import torch
import torch.distributed as dist
from typing import Optional
from colossalai.zero.sharded_param.tensorful_state import StatefulTensor, TensorState


class ShardedTensor(StatefulTensor):

    def __init__(self, tensor: torch.Tensor, process_group: Optional[dist.ProcessGroup] = None) -> None:
        r"""
        A tensor sharded in multiple processes. Constructed from an existing torch.Tensor instance.
        """
        super().__init__(tensor)
        self.trans_state(TensorState.HOLD)

        self._origin_shape = tensor.shape
        self._origin_numel = tensor.numel()
        self._origin_dtype = tensor.dtype

        self.process_group = process_group
        self.world_size = dist.get_world_size(self.process_group)
        self.local_rank = dist.get_rank(self.process_group)
        self._is_sharded = False

    @property
    def origin_numel(self) -> int:
        return self._origin_numel

    @property
    def origin_shape(self) -> int:
        return self._origin_shape

    @property
    def is_sharded(self):
        return self._is_sharded

    @is_sharded.setter
    def is_sharded(self, flag: bool):
        self._is_sharded = flag
