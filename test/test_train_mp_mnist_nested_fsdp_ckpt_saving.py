import args_parse

MODEL_OPTS = {
    '--reshard_after_forward': {'action': 'store_true'},
    '--flatten_parameters': {'action': 'store_true'},
    '--use_nested_fsdp': {'action': 'store_true'},
    '--ckpt_prefix': {'type': str, 'default': 'Please specify'},
}

FLAGS = args_parse.parse_common_options(
    datadir='/tmp/mnist-data',
    batch_size=128,
    momentum=0.5,
    lr=0.01,
    target_accuracy=98.0,
    num_epochs=18,
    opts=MODEL_OPTS.items(),
)

import os
import shutil
import sys
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torchvision import datasets, transforms
import torch_xla
import torch_xla.debug.metrics as met
import torch_xla.distributed.parallel_loader as pl
import torch_xla.utils.utils as xu
import torch_xla.core.xla_model as xm
import torch_xla.distributed.xla_multiprocessing as xmp
import torch_xla.test.test_utils as test_utils

from fsdp.xla_fully_sharded_data_parallel import XlaFullyShardedDataParallel as FSDP


class FSDPMNIST(nn.Module):

  def __init__(self, flags, device):
    super(FSDPMNIST, self).__init__()
    self.conv1 = nn.Conv2d(1, 10, kernel_size=5)
    self.bn1 = nn.BatchNorm2d(10)
    self.conv2 = nn.Conv2d(10, 20, kernel_size=5)
    self.bn2 = nn.BatchNorm2d(20)
    self.fc1 = nn.Linear(320, 50)
    self.fc2 = nn.Linear(50, 10)

    def wrap(m):
      if not flags.use_nested_fsdp:
        return m
      return FSDP(
        m.to(device),
        reshard_after_forward=flags.reshard_after_forward,
        flatten_parameters=flags.flatten_parameters,
      )

    # wrap a few sub-modules (conv1, conv2, fc1, and fc2 with inner FSDP)
    self.conv1 = wrap(self.conv1)
    self.conv2 = wrap(self.conv2)
    self.fc1 = wrap(self.fc1)
    self.fc2 = wrap(self.fc2)

  def forward(self, x):
    x = F.relu(F.max_pool2d(self.conv1(x), 2))
    x = self.bn1(x)
    x = F.relu(F.max_pool2d(self.conv2(x), 2))
    x = self.bn2(x)
    x = torch.flatten(x, 1)
    x = F.relu(self.fc1(x))
    x = self.fc2(x)
    return F.log_softmax(x, dim=1)


def _train_update(device, x, loss, tracker, writer):
  test_utils.print_training_update(
      device,
      x,
      loss.item(),
      tracker.rate(),
      tracker.global_rate(),
      summary_writer=writer)


def train_mnist(flags, **kwargs):
  torch.manual_seed(1)

  if flags.fake_data:
    train_loader = xu.SampleGenerator(
        data=(torch.zeros(flags.batch_size, 1, 28,
                          28), torch.zeros(flags.batch_size,
                                           dtype=torch.int64)),
        sample_count=60000 // flags.batch_size // xm.xrt_world_size())
    test_loader = xu.SampleGenerator(
        data=(torch.zeros(flags.batch_size, 1, 28,
                          28), torch.zeros(flags.batch_size,
                                           dtype=torch.int64)),
        sample_count=10000 // flags.batch_size // xm.xrt_world_size())
  else:
    train_dataset = datasets.MNIST(
        os.path.join(flags.datadir, str(xm.get_ordinal())),
        train=True,
        download=True,
        transform=transforms.Compose(
            [transforms.ToTensor(),
             transforms.Normalize((0.1307,), (0.3081,))]))
    test_dataset = datasets.MNIST(
        os.path.join(flags.datadir, str(xm.get_ordinal())),
        train=False,
        download=True,
        transform=transforms.Compose(
            [transforms.ToTensor(),
             transforms.Normalize((0.1307,), (0.3081,))]))
    train_sampler = None
    if xm.xrt_world_size() > 1:
      train_sampler = torch.utils.data.distributed.DistributedSampler(
          train_dataset,
          num_replicas=xm.xrt_world_size(),
          rank=xm.get_ordinal(),
          shuffle=True)
    train_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=flags.batch_size,
        sampler=train_sampler,
        drop_last=flags.drop_last,
        shuffle=False if train_sampler else True,
        num_workers=flags.num_workers)
    test_loader = torch.utils.data.DataLoader(
        test_dataset,
        batch_size=flags.batch_size,
        drop_last=flags.drop_last,
        shuffle=False,
        num_workers=flags.num_workers)

  # Scale learning rate to num cores
  lr = flags.lr * xm.xrt_world_size()

  device = xm.xla_device()
  model = FSDPMNIST(flags, device).to(device)
  model = FSDP(
    model,
    reshard_after_forward=flags.reshard_after_forward,
    flatten_parameters=flags.flatten_parameters,
  )

  writer = None
  if xm.is_master_ordinal():
    writer = test_utils.get_summary_writer(flags.logdir)
  optimizer = optim.SGD(model.parameters(), lr=lr, momentum=flags.momentum)
  loss_fn = nn.NLLLoss()

  def train_loop_fn(loader):
    tracker = xm.RateTracker()
    model.train()
    for step, (data, target) in enumerate(loader):
      optimizer.zero_grad()
      output = model(data)
      loss = loss_fn(output, target)
      loss.backward()
      # do not reduce the gradients (since we are using sharded params)
      optimizer.step()
      tracker.add(flags.batch_size)
      if step % flags.log_steps == 0:
        xm.add_step_closure(
            _train_update,
            args=(device, step, loss, tracker, writer),
            run_async=FLAGS.async_closures)

  def test_loop_fn(loader):
    total_samples = 0
    correct = 0
    model.eval()
    for data, target in loader:
      output = model(data)
      pred = output.max(1, keepdim=True)[1]
      correct += pred.eq(target.view_as(pred)).sum()
      total_samples += data.size()[0]

    accuracy = 100.0 * correct.item() / total_samples
    accuracy = xm.mesh_reduce('test_accuracy', accuracy, np.mean)
    return accuracy

  train_device_loader = pl.MpDeviceLoader(train_loader, device)
  test_device_loader = pl.MpDeviceLoader(test_loader, device)
  accuracy, max_accuracy = 0.0, 0.0
  for epoch in range(1, flags.num_epochs + 1):
    xm.master_print('Epoch {} train begin {}'.format(epoch, test_utils.now()))
    train_loop_fn(train_device_loader)
    xm.master_print('Epoch {} train end {}'.format(epoch, test_utils.now()))

    if epoch == flags.num_epochs:
      # test saving the state dict
      ckpt_prefix = flags.ckpt_prefix
      rank = xm.get_ordinal()
      world_size = xm.xrt_world_size()
      ckpt_path = f"{ckpt_prefix}_rank-{rank:08d}-of-{world_size:08d}.pth"
      ckpt = {
        "model": model.state_dict(),
        "shard_metadata": model.get_shard_metadata(),
        "optimizer": optimizer.state_dict()
      }
      xm.save(ckpt, ckpt_path, master_only=False)
      print(f"checkpoint saved to {ckpt_path}\n", end="")
      # test loading the state dict
      ckpt = torch.load(ckpt_path)
      model.load_state_dict(ckpt["model"])
      optimizer.load_state_dict(ckpt["optimizer"])

    accuracy = test_loop_fn(test_device_loader)
    xm.master_print('Epoch {} test end {}, Accuracy={:.2f}'.format(
        epoch, test_utils.now(), accuracy))
    max_accuracy = max(accuracy, max_accuracy)
    test_utils.write_to_summary(
        writer,
        epoch,
        dict_to_write={'Accuracy/test': accuracy},
        write_xla_metrics=True)
    if flags.metrics_debug:
      xm.master_print(met.metrics_report())

  test_utils.close_summary_writer(writer)
  xm.master_print('Max Accuracy: {:.2f}%'.format(max_accuracy))
  return max_accuracy


def _mp_fn(index, flags):
  torch.set_default_tensor_type('torch.FloatTensor')
  accuracy = train_mnist(flags)
  if flags.tidy and os.path.isdir(flags.datadir):
    shutil.rmtree(flags.datadir)
  if accuracy < flags.target_accuracy:
    print('Accuracy {} is below target {}'.format(accuracy,
                                                  flags.target_accuracy))
    sys.exit(21)


if __name__ == '__main__':
  xmp.spawn(_mp_fn, args=(FLAGS,), nprocs=FLAGS.num_cores)
