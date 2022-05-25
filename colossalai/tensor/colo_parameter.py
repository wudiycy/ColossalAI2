from .colo_tensor import ColoTensor
from .const import TensorType
import torch
from colossalai.tensor import TensorSpec, distspec
from copy import copy


class ColoParameter(ColoTensor):
    r"""A kind of ColoTensor to be considered as a module parameter.

    """

    def __new__(cls,
                data: torch.Tensor,
                requires_grad: bool = True,
                spec: TensorSpec = TensorSpec(distspec.replicate()),
                lazy_init: bool = False) -> 'ColoParameter':
        if data is None:
            data = torch.empty(0)
        return torch.Tensor._make_subclass(cls, data, requires_grad)

    def __init__(self,
                 data: torch.Tensor,
                 requires_grad: bool = True,
                 spec: TensorSpec = TensorSpec(distspec.replicate()),
                 lazy_init: bool = False) -> None:
        self._spec = copy(spec)
        self._type = TensorType.MODEL
        self._graph_node = None
        if lazy_init:
            self.free()

    @staticmethod
    def from_torch_tensor(tensor: torch.Tensor,
                          requires_grad: bool = True,
                          spec: TensorSpec = TensorSpec(distspec.replicate())) -> 'ColoParameter':
        tensor = tensor.as_subclass(ColoParameter)
        tensor.__init__(tensor, requires_grad=requires_grad, spec=spec)
        return tensor

    def __repr__(self):
        if self._is_freed:
            return f'ColoParameter: lazy_init=True, shape={self.shape}, dtype={self.dtype}, device={self.device}'
        return f'ColoParameter: {torch.Tensor.__repr__(self)}'
