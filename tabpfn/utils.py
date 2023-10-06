import os
import math
import argparse
import random
import datetime
import itertools
import glob
import re

import torch
from torch import nn
from torch.optim.lr_scheduler import LambdaLR
import numpy as np
import pandas as pd
from scipy.signal import convolve, windows

from torch.optim.optimizer import Optimizer


# copied from huggingface
def get_cosine_schedule_with_warmup(optimizer, num_warmup_steps, num_training_steps, num_cycles=0.5, last_epoch=-1):
    """ Create a schedule with a learning rate that decreases following the
    values of the cosine function between 0 and `pi * cycles` after a warmup
    period during which it increases linearly between 0 and 1.
    """

    def lr_lambda(current_step):
        if current_step < num_warmup_steps:
            return float(current_step) / float(max(1, num_warmup_steps))
        progress = float(current_step - num_warmup_steps) / float(max(1, num_training_steps - num_warmup_steps))
        return max(0.0, 0.5 * (1.0 + math.cos(math.pi * float(num_cycles) * 2.0 * progress)))

    return LambdaLR(optimizer, lr_lambda, last_epoch)

# copied from huggingface
def get_restarting_cosine_schedule_with_warmup(optimizer, num_warmup_steps, num_training_steps, steps_per_restart, num_cycles=0.5, last_epoch=-1):
    assert num_training_steps % steps_per_restart == 0

    def inner_lr_lambda(current_step, num_warmup_steps, num_training_steps):
        if current_step < num_warmup_steps:
            return float(current_step) / float(max(1, num_warmup_steps))
        progress = float(current_step - num_warmup_steps) / float(max(1, num_training_steps - num_warmup_steps))
        return max(0.0, 0.5 * (1.0 + math.cos(math.pi * float(num_cycles) * 2.0 * progress)))

    def lr_lambda(current_step):
        inner_step = current_step % steps_per_restart
        return inner_lr_lambda(inner_step,
                               num_warmup_steps if current_step < steps_per_restart else 0,
                               steps_per_restart
                               )


    return LambdaLR(optimizer, lr_lambda, last_epoch)

# copied from huggingface
def get_linear_schedule_with_warmup(optimizer, num_warmup_steps, num_training_steps, last_epoch=-1):
    """
    Create a schedule with a learning rate that decreases linearly from the initial lr set in the optimizer to 0, after
    a warmup period during which it increases linearly from 0 to the initial lr set in the optimizer.

    Args:
        optimizer (:class:`~torch.optim.Optimizer`):
            The optimizer for which to schedule the learning rate.
        num_warmup_steps (:obj:`int`):
            The number of steps for the warmup phase.
        num_training_steps (:obj:`int`):
            The total number of training steps.
        last_epoch (:obj:`int`, `optional`, defaults to -1):
            The index of the last epoch when resuming training.

    Return:
        :obj:`torch.optim.lr_scheduler.LambdaLR` with the appropriate schedule.
    """

    def lr_lambda(current_step: int):
        if current_step < num_warmup_steps:
            return float(current_step) / float(max(1, num_warmup_steps))
        return max(
            0.0, float(num_training_steps - current_step) / float(max(1, num_training_steps - num_warmup_steps))
        )

    return LambdaLR(optimizer, lr_lambda, last_epoch)


def get_openai_lr(transformer_model):
    num_params = sum(p.numel() for p in transformer_model.parameters())
    return 0.003239 - 0.0001395 * math.log(num_params)


def get_weighted_single_eval_pos_sampler(max_len):
    """
    This gives a sampler that can be used for `single_eval_pos` which yields good performance for all positions p,
    where p <= `max_len`. At most `max_len` - 1 examples are shown to the Transformer.
    :return: Sampler that can be fed to `train()` as `single_eval_pos_gen`.
    """
    return lambda: random.choices(range(max_len), [1 / (max_len - i) for i in range(max_len)])[0]


def get_uniform_single_eval_pos_sampler(max_len, min_len=0):
    """
    Just sample any evaluation position with the same weight
    :return: Sampler that can be fed to `train()` as `single_eval_pos_gen`.
    """
    return lambda: random.choices(range(min_len, max_len))[0]


class SeqBN(nn.Module):
    def __init__(self, d_model):
        super().__init__()
        self.bn = nn.BatchNorm1d(d_model)
        self.d_model = d_model

    def forward(self, x):
        assert self.d_model == x.shape[-1]
        flat_x = x.view(-1, self.d_model)
        flat_x = self.bn(flat_x)
        return flat_x.view(*x.shape)


def set_locals_in_self(locals):
    """
    Call this function like `set_locals_in_self(locals())` to set all local variables as object variables.
    Especially useful right at the beginning of `__init__`.
    :param locals: `locals()`
    """
    self = locals['self']
    for var_name, val in locals.items():
        if var_name != 'self': setattr(self, var_name, val)


default_device = 'cuda:0' if torch.cuda.is_available() else 'cpu:0'


# Copied from StackOverflow, but we do an eval on the values additionally
class StoreDictKeyPair(argparse.Action):
    def __init__(self, option_strings, dest, nargs=None, **kwargs):
        self._nargs = nargs
        super(StoreDictKeyPair, self).__init__(option_strings, dest, nargs=nargs, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        my_dict = {}
        for kv in values:
            k, v = kv.split("=")
            try:
                my_dict[k] = eval(v)
            except NameError:
                my_dict[k] = v
        setattr(namespace, self.dest, my_dict)
        print("dict values: {}".format(my_dict))

def get_nan_value(v, set_value_to_nan=0.0):
    if random.random() < set_value_to_nan:
        return v
    else:
        return random.choice([-999, 0, 1, 999])

def to_ranking(data):
    x = (data >= data.unsqueeze(-3))
    x = x.sum(0)
    return x
# TODO: Is there a better way to do this?
#   1. Cmparing to unique elements: When all values are different we still get quadratic blowup
#   2. Argsort(Argsort()) returns ranking, but with duplicate values there is an ordering which is problematic
#   3. Argsort(Argsort(Unique))->Scatter seems a bit complicated, doesn't have quadratic blowup, but how fast?
def to_ranking_low_mem(data):
    x = torch.zeros_like(data)
    for col in range(data.shape[-1]):
        x_ = (data[:, :, col] >= data[:, :, col].unsqueeze(-2))
        x_ = x_.sum(0)
        x[:, :, col] = x_
    return x

def nan_handling_missing_for_unknown_reason_value(set_value_to_nan=0.0):
    return get_nan_value(float('nan'), set_value_to_nan)

def nan_handling_missing_for_no_reason_value(set_value_to_nan=0.0):
    return get_nan_value(float('-inf'), set_value_to_nan)

def nan_handling_missing_for_a_reason_value(set_value_to_nan=0.0):
    return get_nan_value(float('inf'), set_value_to_nan)

def torch_nanmean(x, axis=0, return_nanshare=False):
    num = torch.where(torch.isnan(x), torch.full_like(x, 0), torch.full_like(x, 1)).sum(axis=axis)
    value = torch.where(torch.isnan(x), torch.full_like(x, 0), x).sum(axis=axis)
    if return_nanshare:
        return value / num, 1.-num/x.shape[axis]
    return value / num

def torch_nanstd(x, axis=0):
    num = torch.where(torch.isnan(x), torch.full_like(x, 0), torch.full_like(x, 1)).sum(axis=axis)
    value = torch.where(torch.isnan(x), torch.full_like(x, 0), x).sum(axis=axis)
    mean = value / num
    mean_broadcast = torch.repeat_interleave(mean.unsqueeze(axis), x.shape[axis], dim=axis)
    return torch.sqrt(torch.nansum(torch.square(mean_broadcast - x), axis=axis) / (num - 1))

def normalize_data(data, normalize_positions=-1):
    if normalize_positions > 0:
        mean = torch_nanmean(data[:normalize_positions], axis=0)
        std = torch_nanstd(data[:normalize_positions], axis=0) + .000001
    else:
        mean = torch_nanmean(data, axis=0)
        std = torch_nanstd(data, axis=0) + .000001
    data = (data - mean) / std
    data = torch.clip(data, min=-100, max=100)

    return data

def remove_outliers(X, n_sigma=4, normalize_positions=-1):
    # Expects T, B, H
    assert len(X.shape) == 3, "X must be T,B,H"
    #for b in range(X.shape[1]):
        #for col in range(X.shape[2]):
    data = X if normalize_positions == -1 else X[:normalize_positions]
    data_clean = data[:].clone()
    data_mean, data_std = torch_nanmean(data, axis=0), torch_nanstd(data, axis=0)
    cut_off = data_std * n_sigma
    lower, upper = data_mean - cut_off, data_mean + cut_off

    data_clean[torch.logical_or(data_clean > upper, data_clean < lower)] = np.nan
    data_mean, data_std = torch_nanmean(data_clean, axis=0), torch_nanstd(data_clean, axis=0)
    cut_off = data_std * n_sigma
    lower, upper = data_mean - cut_off, data_mean + cut_off

    X = torch.maximum(-torch.log(1+torch.abs(X)) + lower, X)
    X = torch.minimum(torch.log(1+torch.abs(X)) + upper, X)
            # print(ds[1][data < lower, col], ds[1][data > upper, col], ds[1][~np.isnan(data), col].shape, data_mean, data_std)
    return X

def bool_mask_to_att_mask(mask):
    return mask.float().masked_fill(mask == 0, float('-inf')).masked_fill(mask == 1, float(0.0))

def print_on_master_only(is_master):
    import builtins as __builtin__

    builtin_print = __builtin__.print

    def print(*args, **kwargs):
        force = kwargs.pop("force", False)
        if is_master or force:
            builtin_print(*args, **kwargs)

    __builtin__.print = print


def init_dist(device):
    #print('init dist')
    if 'LOCAL_RANK' in os.environ:
        # launched with torch.distributed.launch
        rank = int(os.environ["LOCAL_RANK"])
        print('torch.distributed.launch and my rank is', rank)
        torch.cuda.set_device(rank)
        os.environ['CUDA_VISIBLE_DEVICES'] = str(rank)
        torch.distributed.init_process_group(backend="nccl", init_method="env://", timeout=datetime.timedelta(seconds=20),
                                             world_size=torch.cuda.device_count(), rank=rank)
        torch.distributed.barrier()
        print_on_master_only(rank == 0)
        print(f"Distributed training on {torch.cuda.device_count()} GPUs, this is rank {rank}, "
              "only I can print, but when using print(..., force=True) it will print on all ranks.")
        return True, rank, f'cuda:{rank}'
    elif 'SLURM_PROCID' in os.environ and torch.cuda.device_count() > 1:
        # this is for multi gpu when starting with submitit
        assert device != 'cpu:0'
        rank = int(os.environ['SLURM_PROCID'])
        os.environ['MASTER_ADDR'] = 'localhost'
        os.environ['MASTER_PORT'] = '12355'
        torch.cuda.set_device(rank)
        os.environ['CUDA_VISIBLE_DEVICES'] = str(rank)
        print('distributed submitit launch and my rank is', rank)
        torch.distributed.init_process_group(backend="nccl", init_method="env://", timeout=datetime.timedelta(seconds=20),
                                             world_size=torch.cuda.device_count(), rank=rank)
        torch.distributed.barrier()
        print_on_master_only(rank == 0)
        print(f"Distributed training on {torch.cuda.device_count()} GPUs, this is rank {rank}, "
              "only I can print, but when using print(..., force=True) it will print on all ranks.")

        return True, rank, f'cuda:{rank}'
    else:
        #print('Not using distributed')
        # will not change any of the behavior of print, but allows putting the force=True in the print calls
        print_on_master_only(True)
        return False, 0, device

# NOP function for python with statements (x = NOP(); with x:)
class NOP():
    def __enter__(self):
        pass
    def __exit__(self, type, value, traceback):
        pass

def check_compatibility(dl):
    if hasattr(dl, 'num_outputs'):
        print('`num_outputs` for the DataLoader is deprecated. It is assumed to be 1 from now on.')
        assert dl.num_outputs != 1, "We assume num_outputs to be 1. Instead of the num_ouputs change your loss." \
                                    "We specify the number of classes in the CE loss."

def product_dict(dic):
    keys = dic.keys()
    vals = dic.values()
    for instance in itertools.product(*vals):
        yield dict(zip(keys, instance))

def normalize_by_used_features_f(x, num_features_used, num_features, normalize_with_sqrt=False):
    if normalize_with_sqrt:
        return x / (num_features_used / num_features)**(1 / 2)
    return x / (num_features_used / num_features)


def compare_dicts(left, right, prefix=None, all=False):
    if not all:
        for d in [left, right]:
            d.pop("losses", None)
            d.pop("learning_rates", None)
            d.pop("wallclock_times", None)
            d.pop("bptt_extra_samples", None)
            d.pop("num_classes", None)
            d.pop("differentiable_hyperparameters", None)
            d.pop("num_features_used", None)

    prefix = prefix or ""
    for k in set(left).union(set(right)):
        if k not in left:
            print(f"{prefix}{k} missing in left")
            continue
        if k not in right:
            print(f"{prefix}{k} missing in right")
            continue
        if isinstance(left[k], dict):
            compare_dicts(left[k], right[k], prefix=f"{prefix}{k}->", all=all)
        else:
            if (torch.is_tensor(left[k]) and (left[k] != right[k]).all()) or (not torch.is_tensor(left[k]) and left[k] != right[k]):
                print(f"{prefix}{k}: left: {left[k]}, right: {right[k]}")


def get_latest_losses(fileglob="models_diff/*.cpkt"):

    losses_dict = {}
    lr_dict = {}
    wallclock_dict = {}
    last_saves = {}
    for name in glob.glob(fileglob):
        if "prior_diff_real" in name:
            continue
        shortname, epoch_string = name.split("/")[1].split("_epoch_")
        epoch_string = epoch_string[:-len(".cpkt")]
        if epoch_string == "on_exit":
            epoch = np.inf
        else:
            epoch = int(re.findall("(\d+)", epoch_string)[0])
        if shortname in last_saves:
            if last_saves[shortname][1] < epoch:
                last_saves[shortname] = (name, epoch)
        else:
            last_saves[shortname] = (name, epoch)

    for shortname, (name, _) in last_saves.items():
        try:
            model_things = torch.load(name, map_location="cpu")
        except Exception as e:
            print(f"Error on {name}: {str(e)}")
            continue
        config = model_things[-1]
        if "losses" in config:
            losses_dict[shortname] = config['losses']
        if "wallclock_time" in config:
            wallclock_dict[shortname] = config['wallclock_time']
        elif "wallclock_times" in config:
            wallclock_dict[shortname] = config['wallclock_times']
        else:
            wallclock_dict[shortname] = np.NaN
        lr_dict[shortname] = config.get("learning_rates", np.NaN)
    return losses_dict, lr_dict, wallclock_dict, last_saves

def make_long_loss_df(losses_dict, lr_dict, wallclock_dict, smoother=None):
    def trim(series, skip):
        if pd.api.types.is_scalar(series):
            return series
        return series[skip:-skip-1]
    
    dfs = []
    for name, losses in losses_dict.items():
        if smoother is not None:
            if len(smoother) > len(losses):
                continue
            smoothed_losses = convolve(losses, smoother, mode="valid")
            skip = (len(losses) - len(smoothed_losses)) // 2
            if skip < 0:
                continue
            this_df = pd.DataFrame({"loss": smoothed_losses,
                                    "learning_rate": trim(lr_dict[name], skip),
                                    "time": trim(wallclock_dict[name], skip),
                                    "epoch": trim(np.arange(len(losses)), skip)})
        else:
            this_df = pd.DataFrame({"loss": losses, "learning_rate": lr_dict[name], "time": wallclock_dict[name], "epoch": np.arange(len(losses))})
        
        this_df['run'] = name
        dfs.append(this_df)
    long_df = pd.concat(dfs)
    long_df['time_hours'] = long_df.time / 3600
    long_df['time_days'] = long_df.time_hours / 24
    return long_df


class ReduceLROnSpike:
    """Reduce learning rate when a metric has bounced up.

    Args:
        optimizer (Optimizer): Wrapped optimizer.
        mode (str): One of `min`, `max`. In `min` mode, lr will
            be reduced when the quantity monitored has stopped
            decreasing; in `max` mode it will be reduced when the
            quantity monitored has stopped increasing. Default: 'min'.
        factor (float): Factor by which the learning rate will be
            reduced. new_lr = lr * factor. Default: 0.1.
        smoothing (int): Number of epochs with over which to smooth recent performance.
            Default: 10.
        min_lr (float or list): A scalar or a list of scalars. A
            lower bound on the learning rate of all param groups
            or each group respectively. Default: 0.
        eps (float): Minimal decay applied to lr. If the difference
            between new and old lr is smaller than eps, the update is
            ignored. Default: 1e-8.
        verbose (bool): If ``True``, prints a message to stdout for
            each update. Default: ``False``.
    """

    def __init__(self, optimizer, mode='min', factor=0.1, smoothing=10,
                 min_lr=0, eps=1e-8, verbose=False):

        if factor >= 1.0:
            raise ValueError('Factor should be < 1.0.')
        self.factor = factor

        # Attach optimizer
        if not isinstance(optimizer, Optimizer):
            raise TypeError(f'{type(optimizer).__name__} is not an Optimizer')
        self.optimizer = optimizer

        if isinstance(min_lr, (list, tuple)):
            if len(min_lr) != len(optimizer.param_groups):
                raise ValueError(f"expected {len(optimizer.param_groups)} min_lrs, got {len(min_lr)}")
            self.min_lrs = list(min_lr)
        else:
            self.min_lrs = [min_lr] * len(optimizer.param_groups)

        self.smoothing = smoothing
        self.verbose = verbose
        self.mode = mode
        self.threshold = threshold
        self.threshold_mode = threshold_mode
        self.eps = eps
        self.last_epoch = 0
        self.recent_losses = []
        self._init_is_better(mode=mode, threshold=threshold,
                             threshold_mode=threshold_mode)
        self._reset()

    def step(self, metrics):
        # convert `metrics` to float, in case it's a zero-dim Tensor
        current = float(metrics)
        epoch = self.last_epoch + 1
        self.last_epoch = epoch
        if len(self.recent_losses) < self.smoothing:
            self.recent_losses.append(current)
            return
        

        if np.abs(np.mean(self.recent_losses) - current) > np.std(self.recent_losses):
            if self.verbose:
                print("That loss looks bad!")
                print("Recent losses:", self.recent_losses)
                print("Current loss:", current)
            self._reduce_lr(epoch)
            self.recent_losses = []
        else:
            self.recent_loses = self.recent_losses[1:] + [current]

        self._last_lr = [group['lr'] for group in self.optimizer.param_groups]

    def _reduce_lr(self, epoch):
        for i, param_group in enumerate(self.optimizer.param_groups):
            old_lr = float(param_group['lr'])
            new_lr = max(old_lr * self.factor, self.min_lrs[i])
            if old_lr - new_lr > self.eps:
                param_group['lr'] = new_lr
                if self.verbose:
                    epoch_str = ("%.2f" if isinstance(epoch, float) else
                                 "%.5d") % epoch
                    print(f'Epoch {epoch_str}: reducing learning rate of group {i} to {new_lr:.4e}.')

    def state_dict(self):
        return {key: value for key, value in self.__dict__.items() if key != 'optimizer'}

    def load_state_dict(self, state_dict):
        self.__dict__.update(state_dict)
