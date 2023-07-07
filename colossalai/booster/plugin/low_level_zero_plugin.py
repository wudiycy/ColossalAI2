import logging
import os
import warnings
from functools import partial
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Tuple, Union

import torch
import torch.nn as nn
from torch import Tensor
from torch.optim import Optimizer
from torch.optim.lr_scheduler import _LRScheduler as LRScheduler
from torch.utils._pytree import tree_map
from torch.utils.data import DataLoader

from colossalai.checkpoint_io import CheckpointIndexFile, CheckpointIO
from colossalai.checkpoint_io.utils import (
    get_optimizer_base_filenames,
    get_shard_filename,
    save_param_groups,
    save_state_dict,
)
from colossalai.interface import ModelWrapper, OptimizerWrapper
from colossalai.utils import get_current_device
from colossalai.zero import LowLevelZeroOptimizer, zero_model_wrapper, zero_optim_wrapper

from .dp_plugin_base import DPPluginBase
from .torch_ddp_plugin import TorchDDPCheckpointIO

__all__ = ['LowLevelZeroPlugin']


def _convert_floating_point(x, dtype: torch.dtype = torch.float16):
    if isinstance(x, torch.Tensor) and torch.is_floating_point(x):
        return x.to(dtype)
    return x


SUPPORTED_PRECISION = ['fp16', 'bf16', 'fp32']


class LowLevelZeroCheckpointIO(TorchDDPCheckpointIO):

    def save_unsharded_optimizer(self, optimizer: Optimizer, checkpoint: str, gather_dtensor: bool):
        state_dict = optimizer.state_dict()
        if self.coordinator.is_master():
            save_state_dict(state_dict, checkpoint, False)

    def save_sharded_optimizer(self,
                               optimizer: OptimizerWrapper,
                               checkpoint: str,
                               gather_dtensor,
                               prefix=None,
                               size_per_shard: int = 1024):
        """
        Save sharded Zero-optimizer checkpoint under the given checkpointing path.
        The following files will be created under the path:
        - An index file (pytorch_optim.bin.index.json) containing a map between optimizer states and file names
        - A group file (pytorch_optim_group.bin) recording information of param_groups
        - Multiple files (pytorch_optim-000XX.bin) that store state tensors of optimizer in a sharding way
        """
        if os.path.isfile(checkpoint):
            logging.error(f"Provided path ({checkpoint}) should be a directory, not a file")
            return

        Path(checkpoint).mkdir(parents=True, exist_ok=True)

        # state_dict only provide only 'param_groups'
        state_dict = optimizer.optim.state_dict()
        # state shard would be handled by the low-level zero optimizer
        sharded_state = optimizer.state_dict_shard(max_shard_size=size_per_shard)

        # Preparing file paths and index file.
        states_name, save_index_file, param_group_file = get_optimizer_base_filenames(prefix)
        index_file = CheckpointIndexFile(checkpoint)

        # Store the information of param groups to param_group_file.
        index_file.append_meta_data("param_groups", param_group_file)
        group_file_path = os.path.join(checkpoint, param_group_file)
        save_param_groups(state_dict, group_file_path)

        # Save shards of optimizer states.
        total_size = 0
        for idx, shard_pair in enumerate(sharded_state):
            shard, current_size = shard_pair
            shard_file = get_shard_filename(states_name, idx)
            total_size = total_size + current_size
            for param_id in shard.keys():
                index_file.append_weight_map(str(param_id), shard_file)

            checkpoint_file_path = os.path.join(checkpoint, shard_file)
            if self.coordinator.is_master():
                save_state_dict(shard, checkpoint_file_path, use_safetensors=False)

        # Wrap up index file.
        index_file.append_meta_data("total_size", total_size)
        if self.coordinator.is_master():
            index_file.write_index_file(save_index_file)
        logging.info(f"The optimizer is going to be split to checkpoint shards. "
                     f"You can find where each parameters has been saved in the "
                     f"index located at {save_index_file}.")

    def load_sharded_optimizer(self, optimizer: Optimizer, index_file_path: str, prefix: str):
        super().load_sharded_optimizer(optimizer, index_file_path, prefix)
        current_rank_state_dict = optimizer.optim.state_dict()['state']
        for param_idx, state in current_rank_state_dict.items():
            for k, v in state.items():
                if isinstance(v, torch.Tensor) and k != 'step':
                    padding_size = (self.coordinator.world_size -
                                    v.numel() % self.coordinator.world_size) % self.coordinator.world_size
                    with torch.no_grad():
                        v = v.flatten()
                        if padding_size > 0:
                            v = torch.nn.functional.pad(v, [0, padding_size])
                        v_list = v.split(v.numel() // self.coordinator.world_size)
                        current_rank_state_dict[param_idx][k] = v_list[self.coordinator.rank].detach()


class LowLevelZeroModel(ModelWrapper):

    def __init__(self, module: nn.Module, stage: int, precision: str) -> None:
        super().__init__(module)
        self.dtype = None
        if precision == 'fp16':
            self.dtype = torch.float16
        elif precision == 'bf16':
            self.dtype = torch.bfloat16
        module = zero_model_wrapper(module, zero_stage=stage)
        if self.dtype is not None:
            module = module.to(self.dtype)
        module = module.to(get_current_device())
        self.module = module
        self.convert_fn = None
        if self.dtype is not None:
            self.convert_fn = partial(_convert_floating_point, dtype=self.dtype)

    def forward(self, *args, **kwargs):
        if self.convert_fn is not None:
            args = tree_map(self.convert_fn, args)
            kwargs = tree_map(self.convert_fn, kwargs)
        return super().forward(*args, **kwargs)


# class LowLevelZeroOptimizer(OptimizerWrapper):

#     def __init__(self,
#                  module: nn.Module,
#                  optimizer: Optimizer,
#                  zero_optim_config: dict,
#                  optim_kwargs: dict,
#                  verbose: bool = False) -> None:
#         optimizer = zero_optim_wrapper(module,
#                                        optimizer,
#                                        optim_config=zero_optim_config,
#                                        **optim_kwargs,
#                                        verbose=verbose)
#         super().__init__(optimizer)

#     def backward(self, loss: Tensor, *args, **kwargs):
#         self.optim.backward(loss)

#     def clip_grad_by_norm(self,
#                           max_norm: Union[float, int],
#                           norm_type: Union[float, int] = 2,
#                           error_if_nonfinite: bool = False,
#                           *args,
#                           **kwargs) -> Tensor:
#         warnings.warn(f'LowLevelZero controls grad clipping by itself, so you should not use clip_grad_by_norm')

#     def clip_grad_by_value(self, clip_value: float, *args, **kwargs) -> None:
#         raise NotImplementedError('LowLevelZero does not support clip_grad_by_value')


class LowLevelZeroPlugin(DPPluginBase):
    """
    Plugin for low level zero.

    Example:
        >>> from colossalai.booster import Booster
        >>> from colossalai.booster.plugin import LowLevelZeroPlugin
        >>>
        >>> model, train_dataset, optimizer, criterion = ...
        >>> plugin = LowLevelZeroPlugin()

        >>> train_dataloader = plugin.prepare_dataloader(train_dataset, batch_size=8)
        >>> booster = Booster(plugin=plugin)
        >>> model, optimizer, train_dataloader, criterion = booster.boost(model, optimizer, train_dataloader, criterion)

    Args:
        strage (int, optional): ZeRO stage. Defaults to 1.
        precision (str, optional): precision. Support 'fp16', 'bf16' and 'fp32'. Defaults to 'fp16'.
        initial_scale (float, optional): Initial scale used by DynamicGradScaler. Defaults to 2**32.
        min_scale (float, optional): Min scale used by DynamicGradScaler. Defaults to 1.
        growth_factor (float, optional): growth_factor used by DynamicGradScaler. Defaults to 2.
        backoff_factor (float, optional): backoff_factor used by DynamicGradScaler. Defaults to 0.5.
        growth_interval (float, optional): growth_interval used by DynamicGradScaler. Defaults to 1000.
        hysteresis (float, optional): hysteresis used by DynamicGradScaler. Defaults to 2.
        max_scale (int, optional): max_scale used by DynamicGradScaler. Defaults to 2**32.
        max_norm (float, optional): max_norm used for `clip_grad_norm`. You should notice that you shall not do
            clip_grad_norm by yourself when using ZeRO DDP. The ZeRO optimizer will take care of clip_grad_norm.
        norm_type (float, optional): norm_type used for `clip_grad_norm`.
        reduce_bucket_size_in_m (int, optional): grad reduce bucket size in M. Defaults to 12.
        communication_dtype (torch.dtype, optional): communication dtype. If not specified, the dtype of param will be used. Defaults to None.
        overlap_communication (bool, optional): whether to overlap communication and computation. Defaults to True.
        cpu_offload (bool, optional): whether to offload grad, master weight and optimizer state to cpu. Defaults to False.
        verbose (bool, optional): verbose mode. Debug info including grad overflow will be printed. Defaults to False.
    """

    def __init__(
        self,
        stage: int = 1,
        precision: str = 'fp16',
        initial_scale: float = 2**32,
        min_scale: float = 1,
        growth_factor: float = 2,
        backoff_factor: float = 0.5,
        growth_interval: int = 1000,
        hysteresis: int = 2,
        max_scale: float = 2**32,
        max_norm: float = 0.0,
        norm_type: float = 2.0,
        reduce_bucket_size_in_m: int = 12,
        communication_dtype: Optional[torch.dtype] = None,
        overlap_communication: bool = True,
        cpu_offload: bool = False,
        verbose: bool = False,
    ) -> None:
        super().__init__()
        assert stage in (1, 2), f'LowLevelZeroPlugin only supports stage 1/2 training'
        assert precision in SUPPORTED_PRECISION, f'LowLevelZeroPlugin only supports {SUPPORTED_PRECISION} training'

        self.stage = stage
        self.precision = precision
        self.zero_optim_config = dict(reduce_bucket_size=reduce_bucket_size_in_m * 1024 * 1024,
                                      communication_dtype=communication_dtype,
                                      overlap_communication=overlap_communication,
                                      cpu_offload=cpu_offload)
        self.optim_kwargs = dict(initial_scale=initial_scale,
                                 growth_factor=growth_factor,
                                 backoff_factor=backoff_factor,
                                 growth_interval=growth_interval,
                                 hysteresis=hysteresis,
                                 min_scale=min_scale,
                                 max_scale=max_scale,
                                 max_norm=max_norm,
                                 norm_type=norm_type)
        self.verbose = verbose

        # set class name with stage, for better error message
        setattr(self.__class__, "__name__", f"LowLevelZeroPlugin_ZeRO-{stage}")

    def support_no_sync(self) -> bool:
        return self.stage == 1

    def control_precision(self) -> bool:
        return True

    def supported_precisions(self) -> List[str]:
        return SUPPORTED_PRECISION

    def control_device(self) -> bool:
        return True

    def supported_devices(self) -> List[str]:
        return ['cuda']

    def configure(
        self,
        model: nn.Module,
        optimizer: Optional[Optimizer] = None,
        criterion: Optional[Callable] = None,
        dataloader: Optional[DataLoader] = None,
        lr_scheduler: Optional[LRScheduler] = None,
    ) -> Tuple[nn.Module, OptimizerWrapper, Callable, DataLoader, LRScheduler]:

        if not isinstance(model, ModelWrapper):
            model = LowLevelZeroModel(model, self.stage, self.precision)

        if optimizer is not None and \
                not isinstance(optimizer, OptimizerWrapper):
            optimizer = zero_optim_wrapper(model.unwrap(),
                                           optimizer,
                                           optim_config=self.zero_optim_config,
                                           **self.optim_kwargs,
                                           verbose=self.verbose)

        return model, optimizer, criterion, dataloader, lr_scheduler

    def control_checkpoint_io(self) -> bool:
        return True

    def get_checkpoint_io(self) -> CheckpointIO:
        return LowLevelZeroCheckpointIO()

    def no_sync(self, model: nn.Module, optimizer: OptimizerWrapper) -> Iterator[None]:
        assert isinstance(optimizer, LowLevelZeroOptimizer)
        return optimizer.optim.no_sync()
