import argparse
import os
import pathlib
import torch.utils.data as data
import builtins
from torch.utils.tensorboard import SummaryWriter
import utils
from loaders.dataset3D import Loader3D
from eval import Eval as Evaluator
from losses import LossFn
from test import test3D, test2D
import sys
import numpy as np
from os import path
import socket
import random
from torch.backends import cudnn
from torch.utils.data import DistributedSampler
import torch
import logging
from train import train3D, train2D
from torch import nn
import torchio as tio
import torch.distributed as dist


def save_weights(epoch, model, optim, score, path):
    state = {
        'epoch': epoch,
        'state_dict': model.state_dict(),
        'optimizer': optim.state_dict(),
        'metric': score
    }
    torch.save(state, path)


def main(experiment_name, args):

    assert torch.cuda.is_available()
    logging.info(f"This model will run on {torch.cuda.get_device_name(torch.cuda.current_device())}")

    ## DETERMINISTIC SET-UP
    seed = config.get('seed', 47)
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    # END OF DETERMINISTIC SET-UP

    loader_config = config.get('data-loader', None)
    train_config = config.get('trainer', None)

    model, dataset_type = utils.load_model(config)
    # DDP setting
    world_size = 1
    rank = 0
    if "WORLD_SIZE" in os.environ:
        logging.info('using DISTRIBUTED data parallel')
        world_size = int(os.environ['WORLD_SIZE'])
        gpu = None
        if args.local_rank != -1:  # for torch.distributed.launch
            rank = args.local_rank
            gpu = args.local_rank
        elif 'SLURM_PROCID' in os.environ:  # for slurm scheduler
            rank = int(os.environ['SLURM_PROCID'])
            gpu = rank % torch.cuda.device_count()
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url, world_size=int(os.environ["WORLD_SIZE"]), rank=rank)
        # For multiprocessing distributed, DistributedDataParallel constructor
        # should always set the single device scope, otherwise,
        # DistributedDataParallel will use all available devices.
        assert gpu is not None
        torch.cuda.set_device(gpu)
        model.cuda(gpu)
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[gpu], find_unused_parameters=True)
    else:
        logging.info('using data parallel')
        model = nn.DataParallel(model).cuda()
    is_distributed = world_size > 1

    train_params = model.parameters()

    optim_config = config.get('optimizer')
    optim_name = optim_config.get('name', None)
    if not optim_name or optim_name == 'Adam':
        optimizer = torch.optim.Adam(params=train_params, lr=optim_config['learning_rate'])
    elif optim_name == 'SGD':
        optimizer = torch.optim.SGD(params=train_params, lr=optim_config['learning_rate'])
    else:
        raise Exception("optimizer not recognized")

    sched_config = config.get('lr_scheduler')
    scheduler_name = sched_config.get('name', None)
    if scheduler_name == 'MultiStepLR':
        scheduler = torch.optim.lr_scheduler.MultiStepLR(
            optimizer,
            milestones=sched_config['milestones'],
            gamma=sched_config.get('factor', 0.1),
        )
    elif scheduler_name == 'Plateau':
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='max', verbose=True, patience=7)
    else:
        scheduler = None

    evaluator = Evaluator(loader_config, project_dir, skip_dump=args.skip_dump)

    loss = LossFn(config.get('loss'), loader_config, weights=None)  # TODO: fix this, weights are disabled now

    start_epoch = 0
    if train_config['checkpoint_path'] is not None:
        try:
            checkpoint = torch.load(train_config['checkpoint_path'])
            model.load_state_dict(checkpoint['state_dict'])
            start_epoch = checkpoint['epoch'] + 1
            optimizer.load_state_dict(checkpoint['optimizer'])
            logging.info(f"Checkpoint loaded successfully at epoch {start_epoch}, score:{checkpoint.get('metric', 'unavailable')})")
        except OSError as e:
            logging.info("No checkpoint exists from '{}'. Skipping...".format(train_config['checkpoint_path']))

    train_loader, test_loader, val_loader, splitter = utils.load_dataset(config, rank, world_size, is_distributed, dataset_type, args.competitor)

    if train_config['do_train']:

        # creating training writer (purge on)
        if rank == 0:
            writer = SummaryWriter(log_dir=os.path.join(config['tb_dir'], experiment_name), purge_step=start_epoch)
        else:
            writer = None

        # warm_up = np.ones(shape=train_config['epochs'])
        # warm_up[0:int(train_config['epochs'] * train_config.get('warm_up_length', 0.35))] = np.linspace(
        #     0, 1, num=int(train_config['epochs'] * train_config.get('warm_up_length', 0.35))
        # )

        best_val = 0
        best_test = 0

        for epoch in range(start_epoch, train_config['epochs']):

            if is_distributed:
                train_loader.sampler.set_epoch(np.random.seed(np.random.randint(0, 10000)))
                dist.barrier()

            if dataset_type == '2D':
                train2D(model, train_loader, loss, optimizer, epoch, writer, evaluator, phase="Train")
            else:
                train3D(model, train_loader, loss, optimizer, epoch, writer, evaluator, phase="Train")

            if rank == 0:
                val_model = model.module
                if dataset_type == '2D':
                    val_iou, val_dice, val_haus = test2D(val_model, val_loader, epoch, writer, evaluator, "Validation", splitter)
                else:
                    val_iou, val_dice, val_haus = test3D(val_model, val_loader, epoch, writer, evaluator, phase="Validation")

                if val_iou < 1e-05 and epoch > 15:
                    logging.info('WARNING: drop in performances detected.')

                if scheduler is not None:
                    if optim_name == 'SGD' and scheduler_name == 'Plateau':
                        scheduler.step(val_iou)
                    else:
                        scheduler.step(epoch)

                save_weights(epoch, model, optimizer, val_iou, os.path.join(project_dir, 'checkpoints', 'last.pth'))

                if val_iou > best_val:
                    best_val = val_iou
                    save_weights(epoch, model, optimizer, best_val, os.path.join(project_dir, 'best.pth'))

                if epoch % 5 == 0 and epoch != 0:
                    if dataset_type == '2D':
                        test_iou, _, _ = test2D(model, test_loader, epoch, writer, evaluator, "Test", splitter)
                    else:
                        test_iou, _, _ = test3D(val_model, test_loader, epoch, writer, evaluator, phase="Test")
                    best_test = best_test if best_test > test_iou else test_iou

        logging.info('BEST TEST METRIC IS {}'.format(best_test))

    if rank == 0:
        val_model = model.module
        if dataset_type == '2D':
            test2D(val_model, test_loader, epoch="Final", writer=None, evaluator=evaluator, phase="Final", splitter=splitter)
        else:
            test3D(val_model, test_loader, epoch="Final", writer=None, evaluator=evaluator, phase="Final")


if __name__ == '__main__':

    PC_NAME = "YOUR LOCAL HOSTNAME"
    RESULTS_DIR = r'Local Project Directory' if socket.gethostname() == PC_NAME else r'Remote Project Directory'
    BASE_YAML_PATH = os.path.join('configs', 'config.yaml') if socket.gethostname() == PC_NAME else os.path.join('configs', 'remote_3D.yaml')

    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument('--base_config', default="config.yaml", help='path to the yaml config file')
    arg_parser.add_argument('--verbose', action='store_true', help="if true sdout is not redirected, default: false")
    arg_parser.add_argument('--skip_dump', action='store_true', help="dump test data, default: false")
    arg_parser.add_argument('--test', action='store_true', help="set up test params, default: false")
    arg_parser.add_argument('--dist-url', default='env://', type=str, help='url used to set up distributed training')
    arg_parser.add_argument('--dist-backend', default='nccl', type=str, help='distributed backend')
    arg_parser.add_argument('--local_rank', default=-1, type=int, help='local rank for distributed training')
    arg_parser.add_argument('--competitor', action='store_true', help='competitor trains on sparse, default: false')
    arg_parser.add_argument('--reload', action='store_true', help='reload experiment?, default: false')

    args = arg_parser.parse_args()
    yaml_path = args.base_config

    if path.exists(yaml_path):
        print(f"loading config file in {yaml_path}")
        config = utils.load_config_yaml(yaml_path)
        experiment_name = config.get('title')
        project_dir = os.path.join(RESULTS_DIR, experiment_name)
    else:
        config = utils.load_config_yaml(BASE_YAML_PATH)  # load base config (remote or local)
        experiment_name = config.get('title', 'test')
        print('this experiment is on debug. no folders are going to be created.')
        project_dir = os.path.join(RESULTS_DIR, 'test')

    log_dir = pathlib.Path(os.path.join(project_dir, 'logs'))
    log_dir.mkdir(parents=True, exist_ok=True)

    ##############################
    #   DISTRIBUTED DATA PARALLEL
    if "WORLD_SIZE" in os.environ:
        rank = 0
        if args.local_rank != -1:  # for torch.distributed.launch
            rank = args.local_rank
        elif 'SLURM_PROCID' in os.environ:  # for slurm scheduler
            rank = int(os.environ['SLURM_PROCID'])

        # suppress printing if not on master gpu, and not debugging on 00
        if rank != 0 and os.environ['SLURM_NODELIST'] != 'aimagelab-srv-00':
            def print_pass(*args, end=None):
                pass
            builtins.print = print_pass

        print(f'cuda visible divices: {torch.cuda.device_count()}')
        print(f'world size: {os.environ["WORLD_SIZE"]}')
        print(f'master address: {os.environ["MASTER_ADDR"]}')
        print(f'master port: {os.environ["MASTER_PORT"]}')
        print(f'dist backend: {args.dist_backend}')
        print(f'dist url: {args.dist_url}')
        print(f'cutting batchsize for distributed from {config["data-loader"]["batch_size"]}', end=" ")
        config["data-loader"]["batch_size"] //= 2
        print(f'to {config["data-loader"]["batch_size"]}')
    # END OF DISTRIBUTED BOOTSTRAP
    #####

    if not args.verbose:
        # redirect streams to project dir
        sys.stdout = open(os.path.join(log_dir, 'std.log'), 'a+')
        sys.stderr = sys.stdout
        utils.set_logger(os.path.join(log_dir, 'logging.log'))
    else:
        # not create folder here, just log to console
        utils.set_logger()

    if args.test:
        config['trainer']['do_train'] = False
        config['trainer']['use_syntetic'] = False
        config['data-loader']['num_workers'] = False
        config['trainer']['checkpoint_path'] = os.path.join(project_dir, 'best.pth')

    if args.reload:
        logging.info("RELOAD! setting checkpoint path to last.pth")
        config['trainer']['checkpoint_path'] = os.path.join(project_dir, 'checkpoints', 'last.pth')

    main(experiment_name, args)
