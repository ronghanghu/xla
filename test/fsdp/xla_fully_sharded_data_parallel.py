# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

import contextlib
import copy
from enum import Enum, auto
import functools
import logging
from math import inf
import time
import traceback
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Generator,
    List,
    Optional,
    Set,
    Tuple,
    Union,
    cast,
)

import torch
from torch.autograd import Variable
import torch.distributed as dist
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parameter import Parameter
from torch.nn.utils.rnn import PackedSequence
import torch_xla.core.xla_model as xm

from .xla_flatten_params_wrapper import FlattenParamsWrapper

if TYPE_CHECKING:
    from collections import OrderedDict  # noqa: F401


class TrainingState(Enum):
    """
    Simple enum to indicate what state FSDP is in. Used for asserting
    to make sure APIs are called in the correct state.

    ..note::

        BACKWARD_PRE and BACKWARD_POST states are used to ensure we
        receives backward hooks in the correct order. It is used to catch
        unexpected order of hooks being called (likely due to our
        hook registration logic or autograd engine logic changes).
    """

    IDLE = auto()
    FORWARD = auto()
    BACKWARD_PRE = auto()
    BACKWARD_POST = auto()


class FullyShardedDataParallel(nn.Module):
    """
    A wrapper for sharding Module parameters across data parallel workers. This
    is inspired by `Xu et al.`_ as well as the ZeRO Stage 3 from DeepSpeed_.
    FullyShardedDataParallel is commonly shorten to FSDP.

    .. _`Xu et al.`: https://arxiv.org/abs/2004.13336
    .. _DeepSpeed: https://www.deepspeed.ai/

    Pseudo-code usage::

        import torch
        from fairscale.nn.data_parallel import FullyShardedDataParallel as FSDP

        torch.cuda.set_device(device_id)
        sharded_module = FSDP(my_module)
        optim = torch.optim.Adam(sharded_module.parameters(), lr=0.0001)
        x = sharded_module(x, y=3, z=torch.Tensor([1]))
        loss = x.sum()
        loss.backward()
        optim.step()

    It is also possible to shard individual layers separately and have an outer
    wrapper handle any leftover parameters. This can be helpful to further
    reduce GPU memory usage, reduce system memory usage when initializing large
    models and to improve training speed by overlapping the all-gather step
    across the forward pass. For example::

        import torch
        from fairscale.nn.wrap import wrap, enable_wrap, auto_wrap
        from fairscale.nn.data_parallel import FullyShardedDataParallel as FSDP
        from fairscale.utils.testing import dist_init, teardown, rmf

        result = dist_init(0, 1, "/tmp/t1", "/tmp/t2")
        assert result
        fsdp_params = dict(wrapper_cls=FSDP, mixed_precision=True, flatten_parameters=True)
        with enable_wrap(**fsdp_params):
            l1 = wrap(torch.nn.Linear(5, 5))
            assert isinstance(l1, FSDP)
            # Wraps layer in FSDP by default if within context
            # Separately Wraps children modules with more than 1e8 params
            large_tfmr = torch.nn.Transformer(d_model=2048, num_encoder_layers=12,
                                              num_decoder_layers=12)
            l2 = auto_wrap(large_tfmr)
            assert isinstance(l2.encoder, FSDP)
            assert isinstance(l2.decoder, FSDP)
            print(l2)  # You can print the model to examine FSDP wrapping.
        teardown()
        rmf("/tmp/t1")
        rmf("/tmp/t2")

    .. warning::

        The optimizer must be initialized *after* the module has been wrapped,
        since FSDP will shard parameters in-place and this will break any
        previously initialized optimizers.

    .. warning::

        If you wrap every parameter inside a nested FSDP and leaving the outer
        FSDP empty without any parameter, checkpointing activation may trigger
        an assert on the backward pass. The solution is to leave some parameters
        to the outer FSDP.

    .. warning::

        If activation checkpointing is used with FSDP, it is strongly encouraged
        to use ``checkpoint_wrapper`` function from FairScale instead of the
        ``checkpoint`` function from PyTorch.

    Args:
        module (nn.Module):
            module to be wrapped with FSDP.
        reshard_after_forward (bool, Optional):
            if ``True``, reshard parameters after the forward pass. This saves
            memory but slows training. This is only relevant when resharding
            individual layers.
        flatten_parameters (bool, Optional):
            if ``True``, flatten parameters into a single contiguous tensor,
            which improves training speed.
        verbose (bool):
            Set this to ``True`` to turn on verbose output for model's string representation.
            Default: False
    """

    def __init__(
        self,
        module: nn.Module,
        reshard_after_forward: bool = True,
        flatten_parameters: bool = True,
        verbose: bool = False,
    ):
        init_start = time.time()
        super().__init__()
        self.rank = xm.get_ordinal()
        self.world_size = xm.xrt_world_size()
        self.reshard_after_forward = self._orig_reshard_after_forward = reshard_after_forward
        self.flatten_parameters = flatten_parameters
        self.compute_dtype = torch.float32
        self.buffer_dtype = self.compute_dtype
        self.uncollected_opt_state: Dict[int, Dict] = {}
        self.verbose = verbose

        self.gradient_predivide_factor: float = self._get_gradient_predivide_factor(self.world_size)
        self.gradient_postdivide_factor: float = self.world_size / self.gradient_predivide_factor

        self.numel_padded_per_param: List[int] = []
        self._tstart = time.time()

        # TODO (ronghang): build XLA version of SyncBN and automatically enable it
        # enable_pytorch_sync_bn(module)

        # Only handle params which are not already sharded. This enables
        # sharding individual layers of a Module, with an outer wrapper to
        # shard any leftover parameters.
        param_names = []
        params = []
        for param_name, param in module.named_parameters():
            if not hasattr(param, "_is_sharded"):
                param_names.append(param_name)
                params.append(param)

        # For now, it is either all flatten or none flatten.
        if self.flatten_parameters:
            to_be_flatten_params: List[List[Parameter]] = [params]
            non_flatten_params = []
        else:
            # In XLA FSDP, we wrap all parameters with FlattenParamsWrapper
            # even if `flatten_parameters` is False. In this case each param
            # gets its own flatten group (so the flattening has no practical
            # effect on the param size or numbers, but allows us to get around
            # the slicing issue in https://github.com/pytorch/xla/issues/3330)
            to_be_flatten_params: List[List[Parameter]] = [[p] for p in params]
            non_flatten_params = []
        del param_names

        self._fsdp_wrapped_module: nn.Module = FlattenParamsWrapper(
            module, param_list=to_be_flatten_params
        )
        del module  # free original module in case it helps garbage collection

        # Now, in this FSDP wrapper class, we keep a list of to-be-flatten and not-to-be-flatten
        # params for doing sharding, gradient hooks, etc. Note, the ordering of the
        # list matters: flatten params are always in the front.
        #
        # The self._num_flatten_params and self._param_name_groups are computed
        # and kept here to support summon_full_params and shard-to-full weight
        # consolidation.
        params_to_shard = cast(List[Parameter], self._fsdp_wrapped_module.flat_params) + non_flatten_params
        # self._num_flatten_params = len(self._fsdp_wrapped_module.flat_params)  # not supported in XLA FSDP
        # self._param_name_groups = param_name_groups  # not supported in XLA FSDP

        # Shard module parameters in place
        self._shard_parameters_(params_to_shard)

        # Make sure all parameters are sharded.
        for n, p in self.named_parameters():
            assert hasattr(p, "_is_sharded"), f"found unsharded parameter: {n} ; {p.size()}"

        self._reset_lazy_init()

        # Flag to indicate if we require gradient reduction in the backward
        # pass. This will be False when inside the no_sync context manager.
        self._require_backward_grad_sync: bool = True

        # Enum to indicate if we're in the forward/backward pass, idle, etc.
        self.training_state = TrainingState.IDLE

        # Flag to indicate if the full params are gathered.
        self.has_full_params: bool = False

        init_end = time.time()

        logging.debug(
            f"FSDP.__init__(done): total_init_time: {(init_end - init_start): .4f} "
            f"num_params (sharded): {(sum(p.numel() for p in self.sharded_params))}"
        )

        # Flag to guard against preparing gradients multiple times per iteration.
        # This is reset at the end of the backward pass.
        self._pre_backward_hook_has_run = False

    def _get_gradient_predivide_factor(self, world_size: int) -> float:
        factor: int = 1
        while world_size % factor == 0 and world_size / factor > factor:
            factor *= 2
        return float(factor)

    def set_gradient_divide_factors(self, pre: float, post: float, recursive: bool) -> None:
        """Allowing user to override the pre and post divide factors.

        Args:
            pre (float): divide factor before the reduction.
            post (float): divide factor after the reduction.
            recursive (bool): recursively set it for all child FSDP instances or not.
        """
        self.assert_state(TrainingState.IDLE)
        if recursive:
            for module in self.modules():
                if isinstance(module, FullyShardedDataParallel) and module != self:
                    module.set_gradient_divide_factors(pre, post, False)
        self.gradient_predivide_factor = pre
        self.gradient_postdivide_factor = post

    @property
    def module(self) -> FlattenParamsWrapper:
        """make model.module accessible, just like DDP."""
        assert isinstance(self._fsdp_wrapped_module, FlattenParamsWrapper)
        return self._fsdp_wrapped_module

    @property
    def params_with_grad(self) -> List[Parameter]:
        """[p for p in self.parameters() if p.grad is not None]"""
        return [p for p in self.parameters() if p.grad is not None]

    @torch.no_grad()
    def clip_grad_norm_(
        self,
        max_norm: Union[float, int],
        norm_type: Union[float, int] = 2.0,
        # filter_params_fn: Callable[[Any], Any] = None,
    ) -> torch.Tensor:
        """
        Clip all gradients at this point in time. The norm is computed over all
        gradients together, as if they were concatenated into a single vector.
        Gradients are modified in-place.

        Args:
            max_norm (float or int): max norm of the gradients
            norm_type (float or int): type of the used p-norm. Can be ``'inf'``
                for infinity norm.

        Returns:
            Total norm of the parameters (viewed as a single vector).

        .. note:: This is analogous to `torch.nn.utils.clip_grad_norm_` but
            handles the partitioning and multiple devices per rank under the
            hood. The default torch util is not applicable here, because each
            rank only has a partial view of all the grads in the model, so
            calling it in the OSS context would lead to different scaling being
            applied per subset of model parameters.

        .. warning:: This needs to be called on all ranks, since synchronization
            primitives will be used.
        """
        assert self._is_root, "clip_grad_norm should only be called on the root (parent) instance"
        self.assert_state(TrainingState.IDLE)

        max_norm = float(max_norm)
        norm_type = float(norm_type)
        params_with_grad = self.params_with_grad
        # Computes the max norm for this shard's gradients and sync's across workers
        local_norm = _calc_grad_norm(params_with_grad, norm_type)
        if norm_type == inf:
            total_norm = xm.all_reduce(xm.REDUCE_MAX, local_norm)
        else:
            total_norm = xm.all_reduce(xm.REDUCE_SUM, local_norm ** norm_type)
            total_norm = total_norm ** (1.0 / norm_type)

        # Now multiply each grad by (max_norm/total_norm), same as torch 1.7 https://tinyurl.com/3wtxhhqq)
        clip_coef = torch.clip(max_norm / (total_norm + 1e-6), 0.0, 1.0)
        for p in params_with_grad:
            p.grad.detach().mul_(clip_coef.to(p.grad.device))

        return total_norm

    @torch.no_grad()
    def _shard_parameters_(self, params_to_shard) -> None:
        """
        At initialization we wrap a module with full parameters and shard the
        parameters in-place. Sharding is implemented by viewing each parameter
        as a 1D Tensor and retaining only a single slice, where the slice size
        is determined by the number of data parallel workers.

        Wrapping modules with many small parameters (or with a very large data
        parallel world size) will result in many small parameter shards and slow
        performance. In this case it's better to set *``flatten_parameters``* to
        ``True``, so that all of the small parameters in the module are combined
        into a single contiguous Tensor and sharded once.

        After this initial sharding is complete, the user can initialize a
        ``torch.optim.Optimizer`` in the usual way, i.e.::

        .. code-block:: python

            optim = torch.optim.Adam(sharded_module.parameters(), lr=0.0001)

        The optimizer will see only a single slice of parameters and will thus
        allocate less memory for optimizer state, avoiding redundancy across
        data parallel workers.
        """
        # Here we implement it in a different manner from the fairscale FSDP
        # We delete the original module parameters and create the sharded ones
        # TODO (ronghanghu) maybe move it to lazy-init to handle the case of
        # wrapping the model with FSDP first and moving it to XLA device later
        params_to_shard_set = set(params_to_shard)
        assert len(params_to_shard_set) == len(params_to_shard), \
            "params_to_shard should not have dups"
        full_param_infos = []
        shared_full_param_memo = {}
        shared_full_param_infos = []
        full_params = []
        for module_name, m in self.named_modules():
            for n, p in m.named_parameters(recurse=False):
                assert p.dtype == torch.float32, "only fp32 parameters are supported"
                if p in params_to_shard_set:
                    if p in shared_full_param_memo:
                        mname, shared_m, shared_n = shared_full_param_memo[p]
                        shared_full_param_infos.append((module_name, mname, m, n, shared_m, shared_n))
                    else:
                        shared_full_param_memo[p] = (module_name, m, n)
                        full_param_infos.append((module_name, m, n))
                        full_params.append(p)
        assert len(full_params) == len(params_to_shard_set), \
            f"there are parameters in params_to_shard not belonging to this module: " \
            f"{len(full_params)} vs {len(params_to_shard_set)}"
        del shared_full_param_memo
        self.full_params = full_params
        self.full_param_infos = full_param_infos
        self.shared_full_param_infos = shared_full_param_infos

        # deregister the full parameters (so that they won't appear in
        # `parameters()` of the modules)
        for p, (_, m, n) in zip(self.full_params, self.full_param_infos):
            assert n in m._parameters
            m._parameters.pop(n)
        for (_, _, m, n, shared_m, shared_n) in self.shared_full_param_infos:
            assert n in m._parameters
            m._parameters.pop(n)

        # allocate and register new sharded parameters
        self.numel_padded_per_param = []
        self.sharded_params = []
        for p, (module_name, _, n) in zip(self.full_params, self.full_param_infos):
            assert not hasattr(p, "_is_sharded")

            shard_data, num_padded = self._get_shard(p.data)
            p_shard = nn.Parameter(shard_data, requires_grad=p.requires_grad)
            p_shard._orig_size = p.data.size()
            p_shard._is_sharded = True
            p_shard_name = f"fp32shard.{module_name}.{n}".replace(".", "__")
            self.register_parameter(p_shard_name, p_shard)
            self.numel_padded_per_param.append(num_padded)
            self.sharded_params.append(p_shard)
            p._sharded_param = p_shard  # add a handle to the sharded parameter
            # free the original full parameter
            p.data = p.data.new_zeros(1)
            p._has_full_param = False

        assert len(self.numel_padded_per_param) == len(self.full_params)
        assert len(self.sharded_params) == len(self.full_params)

    def _get_shard(self, tensor: torch.Tensor) -> Tuple[torch.Tensor, int]:
        """Return the local shard of a full tensor."""
        # Shard using torch.chunk to match all-gather/reduce-scatter.
        chunks = list(torch.flatten(tensor).chunk(self.world_size))
        while len(chunks) < self.world_size:
            chunks.append(chunks[0].new_empty(0))

        # Determine number of padding elements.
        num_to_pad = chunks[0].numel() - chunks[self.rank].numel()
        assert num_to_pad >= 0, num_to_pad

        shard = chunks[self.rank].clone()
        if num_to_pad > 0:
            shard = F.pad(shard, [0, num_to_pad])
        return shard, num_to_pad

    def extra_repr(self) -> str:
        repr = (
            f"world_size={self.world_size}, "
            f"flatten_parameters={self.flatten_parameters}, "
            f"mixed_precision={self.mixed_precision}, "
        )
        if self.verbose:
            repr = (
                f"rank={self.rank}, " + repr + f"reshard_after_forward={self.reshard_after_forward}, "
                f"compute_dtype={self.compute_dtype}, "
                f"buffer_dtype={self.buffer_dtype}, "
                f"fp32_reduce_scatter={self.fp32_reduce_scatter}, "
                f"compute_device={self.compute_device}"
                f"move_params_to_cpu={self.move_params_to_cpu}, "
                f"move_grads_to_cpu={self.move_grads_to_cpu}, "
                f"bucket_cap_mb={self.bucket_cap_mb}, "
                f"clear_autocast_cache={self.clear_autocast_cache}"
                f"force_input_to_fp32={self.force_input_to_fp32}"
            )
        return repr

    def __getattr__(self, name: str) -> Any:
        """Forward missing attributes to wrapped module."""
        try:
            return super().__getattr__(name)  # defer to nn.Module's logic
        except AttributeError:
            return getattr(self.module, name)

    def __getstate__(self) -> Dict[str, str]:
        """Serialize the state of the current FSDP instance.

        Some properties are not serializable (e.g., process groups, streams), so
        we remove them and try to reconstruct them in :func:`__setstate__`.
        """
        state = copy.copy(self.__dict__)
        state["is_sharded"] = [p._is_sharded for p in self.params]
        state["orig_sizes"] = [p._orig_size for p in self.params]
        self._reset_lazy_init()
        return state

    def __setstate__(self, state: Dict[str, Any]) -> None:
        """Intercept state setting and perform needed changes on params."""
        super().__setstate__(state)

        def fixup(p: Parameter, is_sharded: bool, size: torch.Size) -> Parameter:
            assert isinstance(p, Parameter)
            p.data = p.data.clone()  # move tensors out of shared memory
            p._is_sharded = is_sharded
            p._orig_size = size
            return p

        self.params = [
            fixup(p, is_sharded, size) for p, is_sharded, size in zip(self.params, self.is_sharded, self.orig_sizes)
        ]
        del self.is_sharded
        del self.orig_sizes
        self._reset_lazy_init()

    def __getitem__(self, key: int) -> Any:
        """Forward indexing calls in case the module is a nn.Sequential."""
        return self.module.__getitem__(key)

    @contextlib.contextmanager
    def no_sync(self) -> Generator:
        """
        A context manager to disable gradient synchronizations across FSDP
        processes. Within this context, gradients will be accumulated on module
        variables, which will later be synchronized in the first
        forward-backward pass after exiting the context.

        .. note:: This likely results in higher memory usage because FSDP will
            accumulate the full model gradients (instead of gradient shards)
            until the eventual sync.

        .. note:: Gradient accumulation can be done without this context,
            avoiding the extra GPU memory overhead, but with the extra
            networking overhead.
        """
        self._lazy_init()
        assert self._is_root, "no_sync on inner FSDP is not supported"
        self.assert_state(TrainingState.IDLE)
        # This instance may wrap other FSDP instances and we
        # need to set all of them to accumulate gradients.
        old_flags = []
        for m in self.modules():  # includes self
            if isinstance(m, FullyShardedDataParallel):
                old_flags.append((m, m._require_backward_grad_sync))
                m._require_backward_grad_sync = False
        try:
            yield
        finally:
            for m, old_flag in old_flags:
                assert m._require_backward_grad_sync is False
                m._require_backward_grad_sync = old_flag

    def _reset_lazy_init(self) -> None:
        """Reset instance so :func:`_lazy_init` will run on the next forward."""
        self._is_root: Optional[bool] = None
        self._output_pre_backward_hook_registered: Optional[List] = None
        self.reshard_after_forward = self._orig_reshard_after_forward

    def _lazy_init(self) -> None:
        """Initialization steps that should happen lazily, typically right
        before the first forward pass.
        """
        # Initialize _is_root and setup streams. These steps would ideally
        # happen in __init__, but _is_root can only be determined after the
        # entire model hierarchy is setup, thus we run it lazily.
        if self._is_root is None:
            self._set_is_root()
            self._setup_output_hook_list()

        if self._is_root:
            # Don't free the full params for the outer-most (root) instance,
            # since those params will be needed immediately after for the
            # backward pass.
            self.reshard_after_forward = False

    def _set_is_root(self) -> None:
        """If ``True``, implies that no other :class:`FullyShardedDataParallel`
        instance wraps this one. Called once by :func:`_lazy_init`.
        Also sets self.children_share_process_group = True if all child
        instances share the same process group. If some child instances use a
        different process group, self.clip_grad_norm_ will raise an error.
        """
        if self._is_root is not None:
            return
        # No FSDP instance wraps this, else _is_root would be set to False.
        self._is_root = True
        # If final backward callback is never been queued, state should be IDLE.
        # If final backward callback is queued, the callback should be finished
        # and the state was reset to be IDLE.
        # This should be asserted at the beginning of forward pass in the root instance only.
        # For children instances, if they are checkpointed, state will not be reset to
        # IDLE after each inner forward/backward.
        self.assert_state(TrainingState.IDLE)
        # As the root, we now set all children instances to False and
        # give them a closure to try to queue a wait_for_post_backward.
        for n, m in self.named_modules():
            # `n != ""` excludes self.
            if n != "" and isinstance(m, FullyShardedDataParallel):
                # We relax the assert for non-root instance, when the nested inialized module is wrapped
                # again in FSDP later, for example after training to run inference.
                assert m._is_root is None or not m._is_root
                if m._is_root is None:
                    m._is_root = False

    def _setup_output_hook_list(self) -> None:
        """set up a list to avoid registering pre-backward hooks
        incorrectly.
        """
        assert self._is_root, "This should only be called on the root"
        self._output_pre_backward_hook_registered = []
        for n, m in self.named_modules():
            if n != "" and isinstance(m, FullyShardedDataParallel):
                m._output_pre_backward_hook_registered = self._output_pre_backward_hook_registered

    def forward(self, *args: Any, **kwargs: Any) -> torch.Tensor:
        self._lazy_init()

        # Start of a forward pass.
        self.training_state = TrainingState.FORWARD

        # All-gather full parameters.
        self._rebuild_full_params()

        # Register backward hooks to reshard params and reduce-scatter grads.
        # These need to be re-registered every forward pass.
        self._register_post_backward_hooks()

        outputs = self.module(*args, **kwargs)
        if self.reshard_after_forward:
            self._free_full_params()

        # Register pre-backward hooks to all-gather the params for the backward
        # pass (if output's grad was needed). This won't register anything if
        # we are in eval mode.
        # Some model does forward pass multiple times, we need to register the
        # pre-backward hook on every output since the last output's hook has to
        # fire first to setup for backward. However, we use ``self._pre_backward_hook_has_run``
        # to prevent repeated overhead from multiple hook callbacks.
        outputs = self._register_pre_backward_hooks(outputs)

        # Done with a forward pass.
        self.training_state = TrainingState.IDLE

        return outputs

    def _register_pre_backward_hooks(self, outputs: Any) -> Any:
        """Register pre-backward hook to run before the wrapped module's
        backward. Hooks should be attached to all outputs from the forward.

        Returns:
            outputs: new outputs with hooks registered if they requires gradient.
        """
        if not torch.is_grad_enabled():
            return outputs  # don't register hooks if grad isn't enabled

        if self._is_root:
            # This actually means that only root instance has
            # _post_backward_callback_queued defined. Accidentally accessing this field
            # will assert on all other instances, giving us a nice bug checker.
            self._post_backward_callback_queued = False

        def _pre_backward_hook(*unused: Any) -> None:
            # try to queue final backward callback only once for root, so
            # that final backward callback is attached to the outer most
            # backward graph task and called after all the backward
            # calls are completed.
            if self._is_root:
                self._queue_wait_for_post_backward()

            # All-gather full parameters or switching to the full params.
            # Note, ``self._rebuild_full_params`` is idempotent. So in case it is called
            # unnecessarily, it doesn't incur much overhead.
            if self.reshard_after_forward:
                self._rebuild_full_params()

            # Only run the following once per iteration (i.e. in case
            # it is multiple outputs or multiple forward passes).
            if not self._pre_backward_hook_has_run:
                self._pre_backward_hook_has_run = True
                # Start of a backward pass for the first time in an iteration.
                self.assert_state([TrainingState.IDLE, TrainingState.BACKWARD_PRE])
                # Check p.grad to make sure that it is in the right shape, device, etc.
                for p in self.full_params:
                    if p.grad is not None:
                        assert p.grad.device == p.data.device
                        assert p.grad.size() == p._orig_size

            # Transition to BACKWARD_PRE state if currently IDLE. We can transition from BACKWARD_POST
            # to IDLE when FSDP is within activation checkpointing and called multiple times, due to the
            # extra forward pass for re-computation.
            if self.training_state == TrainingState.IDLE:
                self.training_state = TrainingState.BACKWARD_PRE
            self.assert_state([TrainingState.BACKWARD_PRE, TrainingState.BACKWARD_POST])

        _registered = 0

        def _register_hook(t: torch.Tensor) -> torch.Tensor:
            # We don't register the pre_backward hook on the same tensor that has been
            # returned from an inner FSDP, unless it is the first one. This does
            # not cover all problematic cases though. A tensor not from an inner
            # FSDP can cause problems too:
            # ```
            #   x = layer1(input)
            #   state = [x]  # better change to x.detach(), not fixed by the following if-condition
            #   x = inner_fsdp_module_layer2(x)
            #   state.append(x)  # better change to x.detach(), but fixed by the following if-condition
            #   x = layer3(x)
            #   return x, state
            # ```
            # The tensors in `state`, if not detached, can be registered with
            # backward hooks (in addition to the `x` on the last line). In that case,
            # pre-backward hook can fire multiple times in the order that causes
            # the outer FSDP to crash.
            #
            # The best practice is for modules to be wrapped by FSDP to return 1 and only
            # 1 tensor to be used for backward. All other tensors returned should be
            # detached.
            nonlocal _registered
            assert self._output_pre_backward_hook_registered is not None
            if t.requires_grad and (_registered == 0 or id(t) not in self._output_pre_backward_hook_registered):
                t.register_hook(_pre_backward_hook)
                self._output_pre_backward_hook_registered.append(id(t))
                _registered += 1
            return t

        # Attach hooks to Tensor outputs.
        outputs = apply_to_tensors(_register_hook, outputs)

        return outputs

    def _register_post_backward_hooks(self) -> None:
        """
        Register backward hooks to reshard params and reduce-scatter grads.

        This is called during forward pass. The goal is to attach a hook
        on each of the parameter's gradient generating function (``grad_acc``
        below) so that the hook is called *after* all gradients for that
        param are computed.

        Goals:

        1. We want the hook to fire once and only once *after* all gradients
        are accumulated for a param.
        2. If it fires more than once, we end up incorrectly shard the grad
        multiple times. (could lead to dimension too small)
        3. If it fires once but too early or doesn't fire, we leave gradients
        unsharded. (could lead to dimension too large)

        Due to multiple-pass forward, this function can be called on
        the same parameter multiple times in a single forward pass. If we register
        the hook multiple time, we end up getting called multiple times. We
        could try to get a new hook every time and delete the previous one
        registered. However, due to *unknown reason* (I have debugged it for
        a long time!), in mixed precision mode, we get two different ``grad_acc``
        objects below during different calls of this function (in the same
        forward pass). If we keep the last one, the hook end up firing too
        early. In full precision mode, we luckily get the *same* ``grad_acc``
        object, so deleting and re-registering still ensured the hook fire
        once after all gradients are generated.

        Empirically, keep the first hook register per forward pass seems to
        work the best. We do need to remove the hook at the end of the
        backward pass. Otherwise, the next forward pass will not register
        a new hook, which is needed for a new forward pass.
        """
        if not torch.is_grad_enabled():
            return  # don't register grad hooks if grad isn't enabled
        for p in self.full_params:
            if p.requires_grad:
                if hasattr(p, "_shard_bwd_hook"):
                    continue
                # Register a hook on the first call, empirically, autograd
                # fires it at the end for this param, which makes sense.
                p_tmp = p.expand_as(p)  # Get a grad_fn on p_tmp.
                assert p_tmp.grad_fn is not None
                grad_acc = p_tmp.grad_fn.next_functions[0][0]  # Gets its GradAccumulation object.
                handle = grad_acc.register_hook(functools.partial(self._post_backward_hook, p))
                p._shard_bwd_hook = (grad_acc, handle)

    @torch.no_grad()
    def _post_backward_hook(self, param: Parameter, *unused: Any) -> None:
        """
        At the start of :func:`_post_backward_hook`, ``param.grad`` contains the
        full gradient for the local batch. The reduce-scatter op will replace
        ``param.grad`` with a single shard of the summed gradient across all
        GPUs. This shard will align with the current GPU rank. For example::

            before reduce_scatter:
                param.grad (GPU #0): [1, 2, 3, 4]
                param.grad (GPU #1): [5, 6, 7, 8]

            after reduce_scatter:
                param.grad (GPU #0): [6, 8]    # 1+5, 2+6
                param.grad (GPU #1): [10, 12]  # 3+7, 4+8

        The local GPU's ``optim.step`` is responsible for updating a single
        shard of params, also corresponding to the current GPU's rank. This
        alignment is created by :func:`_shard_parameters_`, which ensures that
        the local optimizer only sees the relevant parameter shard.
        """
        # First hook callback will see PRE state. If we have multiple params,
        # then subsequent hook callbacks will see POST state.
        self.assert_state([TrainingState.BACKWARD_PRE, TrainingState.BACKWARD_POST])
        self.training_state = TrainingState.BACKWARD_POST
        if param.grad is None:
            return

        assert param.grad is not None, param.shape
        if param.grad.requires_grad:
            raise RuntimeError("FSDP only works with gradients that don't require gradients")

        if self._require_backward_grad_sync or self.reshard_after_forward:
            # Free full params. As a special case, we don't free the full params
            # when in a ``no_sync`` context (as inversely indicated by
            # ``self._require_backward_grad_sync``), since the params will not
            # get updated before the next forward. This saves networking
            # bandwidth but uses more GPU memory.
            self._free_full_params([param])

        # Switch to FP32 shard after backward.
        # self._use_fp32_param_shard([param])  # not needed in XLA FSDP

        if not self._require_backward_grad_sync:
            return

        if self.gradient_predivide_factor > 1:
            # Average grad by world_size for consistency with PyTorch DDP.
            param.grad.data.div_(self.gradient_predivide_factor)

        assert hasattr(param, "_has_full_param")
        # Save the unsharded grad for reduction. We will asynchronously accumulate the reduced gradient into
        # sharded param's .grad. If this FSDP module was called multiple times it's possible that multiple
        # gradient reductions will happen in an undefined order. But addition commutes, so this order doesn't
        # matter, neglecting rounding.
        grad = param.grad.data
        # Clear grad on the tensor, so any repeated gradient computations do not interfere with this reduction.
        param.grad = None
        grad_flat = _flatten_and_pad_to_chunks(grad, self.world_size)
        grad_reduce_scattered = xm.reduce_scatter(
            xm.REDUCE_SUM, grad_flat, scale=1.0, scatter_dim=0, shard_count=self.world_size
        )
        self._post_reduction_hook(param, grad_reduce_scattered)

    def _post_reduction_hook(self, param: Parameter, reduced_grad: torch.Tensor) -> None:
        """Hook to call on each param after the reduce-scatter."""
        self.assert_state(TrainingState.BACKWARD_POST)
        if self.gradient_postdivide_factor > 1:
            # Average grad by world_size for consistency with PyTorch DDP.
            reduced_grad.data.div_(self.gradient_postdivide_factor)

        assert hasattr(param, "_sharded_param")
        p_shard = param._sharded_param
        # Accumulate into the gradient shard.
        if p_shard.grad is None:
            p_shard.grad = reduced_grad.data
        else:
            assert p_shard.grad.shape == reduced_grad.shape
            assert p_shard.grad.device == reduced_grad.device
            p_shard.grad.data += reduced_grad.data

    def _queue_wait_for_post_backward(self) -> None:
        """Try to queue a `wait_for_post_backward` callback.

        Only called on root and only queue one callback at the beginning of
        outer most backward.
        """
        assert self._is_root
        if not self._post_backward_callback_queued:
            self.assert_state([TrainingState.IDLE])
            self._post_backward_callback_queued = True
            Variable._execution_engine.queue_callback(self._wait_for_post_backward)

    @torch.no_grad()
    def _wait_for_post_backward(self) -> None:
        """Wait for post-backward to finish. Only called on root instance."""
        assert self._is_root
        # Check if the root module has params and if any of them has
        # the `requires_grad` field set. If `requires_grad=False` for
        # all the params, the post_backward hook will not fire and the
        # state will remain in `TrainingState.BACKWARD_PRE`.
        if any([p.requires_grad for p in self.full_params]):
            self.assert_state(TrainingState.BACKWARD_POST)
        else:
            self.assert_state(TrainingState.BACKWARD_PRE)

        # A backward pass is done, clean up below.
        def _finalize_parameters(fsdp_module: FullyShardedDataParallel) -> None:
            """Helper used below on all fsdp modules."""
            for p in fsdp_module.full_params:
                if not p.requires_grad:
                    continue
                if hasattr(p, "_shard_bwd_hook"):
                    assert len(p._shard_bwd_hook) == 2, len(p._shard_bwd_hook)
                    p._shard_bwd_hook[1].remove()
                    delattr(p, "_shard_bwd_hook")

        # Update root and nested FSDP's hooks and flags.
        for m in self.modules():  # includes self
            if isinstance(m, FullyShardedDataParallel):
                _finalize_parameters(m)
                m._pre_backward_hook_has_run = False
                if any(p.requires_grad for p in m.parameters()):
                    # Check if the module has params and if any of them has
                    # the `requires_grad` field set. If `requires_grad=False` for
                    # all the params, the post_backward hook will not fire and the
                    # state will remain in `TrainingState.BACKWARD_PRE`.
                    if any([p.requires_grad for p in m.full_params]):
                        m.assert_state(TrainingState.BACKWARD_POST)
                    else:
                        m.assert_state(TrainingState.BACKWARD_PRE)
                else:
                    # When `m` and its children has no params or has params but
                    # none with `requires_grad==True`, there are two cases:
                    # 1. output tensors are `requires_grad==True`. In this case,
                    # pre-backward hook is still registered, so it is in BACKWARD_PRE state.
                    # 2. output tensors are `requires_grad==False`. In this case,
                    # pre-backward hook is not registered, so it is in IDLE state.
                    m.assert_state([TrainingState.BACKWARD_PRE, TrainingState.IDLE])
                m.training_state = TrainingState.IDLE

                if m._is_root:
                    # reset this flag for cases like "one forward pass + multiple backward passes"
                    self._post_backward_callback_queued = False
                    # clear this list for next iteration
                    assert self._output_pre_backward_hook_registered is not None
                    self._output_pre_backward_hook_registered.clear()

    @torch.no_grad()
    def _rebuild_full_params(self) -> None:
        """
        Gather all shards of params.

        Note, this is idempotent if full params are already gathered. Callers
        assume the idempotency. So please keep it that way.
        """
        if self.has_full_params:
            return
        for p, p_shard in zip(self.full_params, self.sharded_params):
            if not p._has_full_param:
                # gather full parameter from shards
                p_padded = xm.all_gather(p_shard).flatten().detach()
                p.data = p_padded[: p_shard._orig_size.numel()].view(p_shard._orig_size)
                p._has_full_param = True
        self.has_full_params = True

    @torch.no_grad()
    def _free_full_params(self, params: Optional[List[Parameter]] = None) -> None:
        """Free up storage for full parameters."""
        if params is None:
            params = self.full_params
        self.has_full_params = False
        for p in params:
            if p._has_full_param:
                # free the original full parameter
                p.data = p.data.new_zeros(1)
                p._has_full_param = False

    def assert_state(self, state: Union[TrainingState, List[TrainingState]]) -> None:
        """Assert we are in the given state."""
        # Since assert can be turned off and this error checking
        # is really important, we use explicit error checking
        # and raise a ValueError if needed.
        if isinstance(state, TrainingState):
            state = [state]
        if self.training_state not in state:
            msg = f"expected to be in states {state} but current state " f"is {self.training_state}"
            # In case we are failing in the context of autograd hook, asserting
            # may not generate useful msg. So, let's print it to be sure.
            if self.rank == 0:
                print(f"Asserting FSDP instance is: {self}")
                print(f"ERROR: {msg}")
                traceback.print_stack()
            raise ValueError(msg)

    def _print_r0(self, msg: str, restart: bool = False) -> None:
        """Debugging utility to print memory usage stats nicely on rank 0"""
        if restart:
            self._tstart = time.time()
        if self.rank == 0:
            memory_info = xm.get_memory_info(xm.xla_device())
            gb_free = memory_info["kb_free"] / 1024 / 1024
            gb_total = memory_info["kb_total"] / 1024 / 1024
            logging.info(
                f"{msg} free={gb_free: .4f} GB, total={gb_total: .4f} GB, t={time.time()-self._tstart: .1f}"
            )


def _flatten_and_pad_to_chunks(tensor: torch.Tensor, world_size: int) -> torch.Tensor:
    """Flatten and pad a tensor to a given world size (for reduce-scatter)."""
    if tensor.numel() % world_size != 0:
        pad_size = world_size - tensor.numel() % world_size
        tensor = F.pad(tensor.flatten(), [0, pad_size])

    return tensor


def apply_to_tensors(fn: Callable, container: Union[torch.Tensor, Dict, List, Tuple, Set]) -> Any:
    """Recursively apply to all tensor in different kinds of container types."""

    def _apply(x: Union[torch.Tensor, Dict, List, Tuple, Set]) -> Any:
        if torch.is_tensor(x):
            return fn(x)
        elif isinstance(x, OrderedDict):
            od = x.__class__()
            for key, value in x.items():
                od[key] = _apply(value)
            return od
        elif isinstance(x, PackedSequence):
            _apply(x.data)
            return x
        elif isinstance(x, dict):
            return {key: _apply(value) for key, value in x.items()}
        elif isinstance(x, list):
            return [_apply(x) for x in x]
        elif isinstance(x, tuple):
            return tuple(_apply(x) for x in x)
        elif isinstance(x, set):
            return {_apply(x) for x in x}
        else:
            return x

    return _apply(container)


def _calc_grad_norm(parameters: List[torch.nn.Parameter], p: float) -> torch.Tensor:
    r"""Calculate gradient norm of an iterable of parameters.
    Returns:
        Total norm of the parameters (viewed as a single vector).
    """
    if len(parameters) == 0:
        return torch.tensor(0.0)

    if p == inf:
        local_norm = max(par.grad.detach().abs().max() for par in parameters)
    else:
        local_norm = torch.norm(
            torch.stack([torch.norm(par.grad.detach(), p) for par in parameters]), p
        )
    return local_norm
