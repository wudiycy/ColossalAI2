from enum import Enum
from logging import NullHandler
import torch


class TensorState(Enum):
    FREE = 0,
    HOLD = 1,
    HOLD_AFTER_FWD = 2,
    HOLD_AFTER_BWD = 3


class StatefulTensor(object):
    """A Structure stores a Torch Tensor and labeled states.

    PatrickStar: Parallel Training of Pre-trained Models via Chunk-based Memory Management

    https://arxiv.org/abs/2108.05818
    """

    def __init__(self, tensor: torch.Tensor, state: TensorState = TensorState.HOLD) -> None:
        self._state = state
        if state is not TensorState.FREE:
            self._payload = tensor
        else:
            self._payload = None

    def data_ptr(self):
        if self._payload is None:
            return None
        return self._payload.data_ptr()

    @property
    def state(self) -> TensorState:
        return self._state

    def set_null(self) -> None:
        self._state = TensorState.FREE
        self._payload = None

    def is_null(self) -> bool:
        if self._state == TensorState.FREE:
            assert self._payload is None
            return True
        return False

    def trans_state(self, state: TensorState) -> None:
        self._state = state
        if state == TensorState.FREE:
            self._payload = None

    @property
    def payload(self) -> int:
        return self._payload

    def copy_payload(self, tensor) -> int:
        self._payload.view(-1).copy_(tensor.view(-1))

    def reset_payload(self, tensor) -> int:
        del self._payload
        self._payload = tensor

    @property
    def device(self) -> torch.device:
        return self._payload.device

    @property
    def dtype(self) -> torch.dtype:
        assert self._payload.dtype == self._origin_dtype
        return self._origin_dtype

    def to(self, device: torch.device):
        raise RuntimeError("Use colo_model_tensor_move install of call .to() on ShardedTensor")

    def to_(self, device: torch.device):
        raise RuntimeError("Use colo_model_tensor_move install of call .to_() on ShardedTensor")

    @property
    def shape(self):
        return self._payload.shape
