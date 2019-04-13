import argparse
import os
import time
from tqdm import tqdm

import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import torch.backends.cudnn as cudnn
from torch.autograd import Variable

import torchvision
import torchvision.datasets as datasets
import torchvision.transforms as transforms

from randwire import RandWireSmall78
from loss import CEWithLabelSmoothingLoss
from logger import Logger


def main(args):
    iteration = 0

    # Tensorboard loggers
    log_root = './log'
    log_names = ['', 'train_loss', 'test_loss']
    log_dirs = list(map(lambda x: os.path.join(log_root, args.label, x), log_names))
    if not os.path.isdir(log_root):
        os.mkdir(log_root)
    for log_dir in log_dirs:
        if not os.path.isdir(log_dir):
            os.mkdir(log_dir)
    _, train_logger, val_logger = \
            list(map(lambda x: Logger(x), log_dirs))

    # Checkpoint save directory
    checkpoint_root = './checkpoint'
    if not os.path.isdir(checkpoint_root):
        os.mkdir(checkpoint_root)
    checkpoint_dir = os.path.join(checkpoint_root, args.label)
    if not os.path.isdir(checkpoint_dir):
        os.mkdir(checkpoint_dir)

    # Data transform
    print('==> Preparing data ..')
    traindir = os.path.join(args.data_root, 'train')
    valdir = os.path.join(args.data_root, 'val')
    traintf = transforms.Compose([
        transforms.RandomHorizontalFlip(),
        transforms.RandomResizedCrop(224),
        transforms.ToTensor(),
        transforms.Normalize(
            mean=[0.485,0.456,0.406],
            std=[0.229,0.224,0.225]),
    ])
    valtf = transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize(
            mean=[0.485,0.456,0.406],
            std=[0.229,0.224,0.225]),
    ])
    trainset = datasets.ImageFolder(traindir, traintf)
    trainloader = torch.utils.data.DataLoader(trainset, batch_size=args.batch_size,
            shuffle=True, num_workers=args.num_workers, pin_memory=True)
    valset = datasets.ImageFolder(valdir, valtf)
    valloader = torch.utils.data.DataLoader(valset, batch_size=args.batch_size,
            shuffle=False, num_workers=args.num_workers, pin_memory=True)

    # Model and optimizer
    graph_type = 'WS'
    graph_params = {
        'P': 0.75,
        'K': 4,
    }
    model, graphs = RandWireSmall78(model=graph_type, params=graph_params, seeds=None)

    criterion = CEWithLabelSmoothingLoss

    optimizer = optim.SGD(
            model.parameters(),
            lr=args.lr,
            momentum=0.9,
            weight_decay=5e-5)

    checkpoint = None
    # TODO Load from savepoint
    #  if args.resume:
    #      print('==> Resuming from checkpoint..')
    #      checkpoint = torch.load('./checkpoint/ckpt.pth')
    #      model.load_state_dict(checkpoint['model'])
    #      graph = checkpoint['graph']
    #      optimizer.load_state_dict(checkpoint['optim'])
    #      for state in optimizer.state.values():
    #          for k, v in state.items():
    #              if isinstance(v, torch.Tensor):
    #                  state[k] = v.cuda()
    #      for group in optimizer.param_groups:
    #          group['lr'] = args.lr
    #      args.start_epoch = checkpoint['epoch']
    #      iteration = checkpoint['iteration']
    #      print("Last loss: %.3f" % (checkpoint['loss']))
    #      print("Training start from epoch %d iteration %d" % (args.start_epoch, iteration))
    #  else:
    #      pass

    # Enable multi-GPU learning
    model = torch.nn.DataParallel(model, device_ids=range(torch.cuda.device_count()))
    model.cuda()

    # Main run
    for epoch in range(args.start_epoch, args.start_epoch + args.num_epochs):
        train(trainloader, model, graphs, criterion, optimizer, epoch,
                train_logger=train_logger, save_every=args.save_every)
        test(valloader, model, graphs, criterion, epoch, val_logger=vallogger)


# Train
def train(trainloader, model, graphs, criterion, optimizer, epoch, start_iter=0, train_logger=None, save_every=1000):
    print('\nEpoch: %d' % epoch)

    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    #  top5 = AverageMeter()

    model.train()

    end = time.time()
    for batch_idx, (inputs, targets) in enumerate(tqdm(trainloader)):
        # Data loading time
        data_time.update(time.time() - end)

        # Offset correction
        idx = batch_idx + start_iter

        # FORWARD
        input_vars = Variable(inputs.cuda())
        target_vars = Variable(targets.cuda())

        outputs = model(input_vars)
        loss = criterion(outputs, target_vars, eps=0.1)

        # Update log
        prec = accuracy(outputs.data, target_vars, topk=(1,))
        top1.update(prec[0], inputs.size(0))
        losses.update(loss.data, inputs.size(0))

        # Gradient clip
        #  if args.grad_clip and loss > loss_thres:
        #      tqdm.write('batch (%d/%d) | loss: %.3f | BATCH SKIPPED!'
        #          % (idx, len(trainloader), loss.data))
        #      continue

        # BACKWARD
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # Batch evaluation time
        batch_time.update(time.time() - end)

        # Log current performance
        step = idx + len(trainloader) * epoch
        if train_logger is not None:
            info = {
                'loss': loss.data,
                'average loss': losses.avg,
                'top1 precision': np.asscalar(top1.val.cpu().numpy()),
                'top1 average precision': np.asscalar(top1.avg.cpu().numpy()),
            }
            for tag, value in info.items():
                train_logger.scalar_summary(tag, value, step + 1)

        tqdm.write('batch (%d/%d) | loss: %.3f | avg_loss: %.3f | Prec@1: %.3f %% (%.3f %%)' 
                % (batch_idx, len(trainloader), losses.val, losses.avg,
                    np.asscalar(top1.val.cpu().numpy()),
                    np.asscalar(top1.avg.cpu().numpy())))

        # Save at every specified cycles
        if (idx + 1) % save_every == 0:
            save('train' + str(step), model, graphs, optimizer, losses.avg,
                    epoch, idx + 1)

        # Update the base time
        end = time.time()

        # Finish when total iterations match the number of batches
        if start_iter != 0 and (idx + 1) % len(trainloader) == 0:
            break

# Test
def test(valloader, model, graphs, criterion, epoch, val_logger=None):
    print('\nTest')

    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    #  top5 = AverageMeter()

    model.eval()

    end = time.time()
    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(tqdm(valloader)):
            # FORWARD
            input_vars = Variable(inputs.cuda())
            target_vars = Variable(targets.cuda())

            outputs = model(input_vars)
            loss = criterion(target_vars, outputs)

            # Batch evaluation time
            batch_time.update(time.time() - end)

            # Update log
            prec = accuracy(outputs.data, targets, topk=(1,))
            top1.update(prec[0], inputs.size(0))
            losses.update(loss.data, inputs.size(0))

            tqdm.write('batch (%d/%d) | loss: %.3f | avg_loss: %.3f | Prec@1: %.3f %% (%.3f %%)' 
                    % (batch_idx, len(valloader), losses.val, losses.avg,
                        np.asscalar(top1.val.cpu().numpy()),
                        np.asscalar(top1.avg.cpu().numpy())))

            # Update the base time
            end = time.time()

    # Save and log
    save('test' + str(epoch), model, graphs, optimizer, losses.avg, epoch + 1)

    if val_logger is not None:
        val_logger.scalar_summary('loss', losses.avg,
                (epoch + 1) * len(trainloader))
    print('average test loss: %.3f' % (test_loss))


# Save checkpoints
def save(label, model, graphs, optimizer, loss=float('inf'), epoch=0, iteration=0):
    tqdm.write('==> Saving checkpoint')
    state = {
        'model': model.module.state_dict(),
        'graphs': graphs,
        'optim': optimizer.state_dict(),
        'loss': loss,
        'epoch': epoch,
        'iteration': iteration,
    }
    if not os.path.isdir('checkpoint'):
        os.mkdir('checkpoint')
    torch.save(state, './checkpoint/' + args.label + '/ckpt_' + label + '.pth')
    tqdm.write('==> Save done!')


class AverageMeter(object):
    '''Computes and stores the average and current value
    '''
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def accuracy(output, target, topk=(1,)):
    '''Computes the precision@k for the specified values of k
    '''
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0, keepdim=True)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res


if __name__ == '__main__':
    assert torch.cuda.is_available(), 'CUDA is required!'

    parser = argparse.ArgumentParser(description='PyTorch RandWireNet Training')
    parser.add_argument('--label', default='default', type=str,
            help='labels checkpoints and logs saved under')
    parser.add_argument('--model', default='regular109', type=str,
            choices=('small78', 'regular109', 'regular154'),
            help='backbone network to use in fpn')
    parser.add_argument('--num-workers', default=2, type=int,
            help='number of workers in dataloader')
    parser.add_argument('--data-root', default='../common/datasets/ImageNet', type=str,
            help='ImageNet12 folder where train/ and val/ belong')
    parser.add_argument('--lr', default=1e-2, type=float,
            help='learning rate')
    parser.add_argument('--batch-size', default=128, type=int,
            help='size of a minibatch')
    parser.add_argument('--start-epoch', default=0, type=int,
            help='epoch index to start log')
    parser.add_argument('--num-epochs', default=1, type=int,
            help='number of epochs to run')
    parser.add_argument('--save-every', default=1000, type=int,
            help='save cycle during train mode')
    parser.add_argument('--resume', '-r', action='store_true',
            help='resume from checkpoint')
    args = parser.parse_args()

    # Run main routine
    main(args)