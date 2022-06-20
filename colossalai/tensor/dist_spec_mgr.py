from colossalai.tensor.distspec import _DistSpec
# from colossalai.nn.layer.utils import divide
from numpy import prod
from contextlib import contextmanager
import torch
import torch.distributed as dist


# TODO(jiaruifang) circle import, move the divide to colossalai.commons.
# colossalai.tensor shall not import any submodule from colossal.nn
def divide(numerator, denominator):
    """Only allow exact division.

    Args:
        numerator (int): Numerator of the division.
        denominator (int): Denominator of the division.

    Returns:
        int: the result of exact division.
    """
    assert denominator != 0, 'denominator can not be zero'
    assert numerator % denominator == 0, \
        '{} is not divisible by {}'.format(numerator, denominator)
    return numerator // denominator


class TransformDistSpec(torch.autograd.Function):

    @staticmethod
    def forward(ctx, tensor, old_dist_spec, dist_spec, forward_trans_func, backward_trans_func):
        ctx.old_dist_spec = old_dist_spec
        ctx.dist_spec = dist_spec
        ctx.backward_trans_func = backward_trans_func
        return forward_trans_func(tensor, old_dist_spec, dist_spec)

    @staticmethod
    def backward(ctx, grad_outputs):
        return ctx.backward_trans_func(grad_outputs, ctx.dist_spec, ctx.old_dist_spec), None, None, None, None


class DistSpecManager:

    _use_autograd_function: bool = True

    @staticmethod
    def _shard_as(tensor: torch.Tensor, old_dist_spec: _DistSpec, dist_spec: _DistSpec) -> torch.Tensor:
        chunk = tensor
        idx = dist_spec.process_group.rank()
        num_parts = prod(dist_spec.num_partitions)
        for i, dim in enumerate(dist_spec.dims):
            num_parts //= dist_spec.num_partitions[i]
            chunk_size = divide(tensor.size(dim), dist_spec.num_partitions[i])
            chunk = chunk.narrow(dim, idx // num_parts * chunk_size, chunk_size)
            idx %= num_parts
        return chunk.clone().detach().contiguous()

    @staticmethod
    def _gather(tensor: torch.Tensor, old_dist_spec: _DistSpec) -> torch.Tensor:
        buffer = [torch.empty_like(tensor) for _ in range(old_dist_spec.process_group.size())]
        dist.all_gather(buffer, tensor, group=old_dist_spec.process_group)
        for i in range(len(old_dist_spec.dims) - 1, -1, -1):
            new_buffer = []
            dim = old_dist_spec.dims[i]
            num_parts = old_dist_spec.num_partitions[i]
            for start in range(0, len(buffer), num_parts):
                new_buffer.append(torch.cat(buffer[start:start + num_parts], dim))
            buffer = new_buffer
        assert len(buffer) == 1
        return buffer[0]

    @staticmethod
    def _all_to_all(tensor: torch.Tensor, old_dist_spec: _DistSpec, dist_spec: _DistSpec) -> torch.Tensor:
        world_size = old_dist_spec.process_group.size()
        if world_size == 1:
            return tensor
        gather_dim = old_dist_spec.dims[0]
        scatter_dim = dist_spec.dims[0]
        shapes = list(tensor.shape)
        shapes[scatter_dim] = shapes[scatter_dim] // world_size

        scatter_list = [t.contiguous() for t in torch.tensor_split(tensor, world_size, scatter_dim)]
        gather_list = [torch.empty(*shapes, dtype=tensor.dtype, device=tensor.device) for _ in range(world_size)]
        dist.all_to_all(gather_list, scatter_list, group=old_dist_spec.process_group)

        return torch.cat(gather_list, dim=gather_dim).contiguous()

    @staticmethod
    def _r2r(tensor: torch.Tensor, old_dist_spec: _DistSpec, dist_spec: _DistSpec) -> torch.Tensor:
        if old_dist_spec.process_group is not None and old_dist_spec.process_group != dist_spec.process_group \
                and dist_spec.process_group is not None:
            raise NotImplementedError
        return tensor

    @staticmethod
    def _r2s(tensor: torch.Tensor, old_dist_spec: _DistSpec, dist_spec: _DistSpec) -> torch.Tensor:
        if old_dist_spec.process_group is not None and old_dist_spec.process_group != dist_spec.process_group:
            raise NotImplementedError
        return DistSpecManager._shard_as(tensor, old_dist_spec, dist_spec)

    @staticmethod
    def _s2r(tensor: torch.Tensor, old_dist_spec: _DistSpec, dist_spec: _DistSpec) -> torch.Tensor:
        if old_dist_spec.process_group != dist_spec.process_group \
                and dist_spec.process_group is not None:
            raise NotImplementedError
        return DistSpecManager._gather(tensor, old_dist_spec)

    @staticmethod
    def _s2s(tensor: torch.Tensor, old_dist_spec: _DistSpec, dist_spec: _DistSpec) -> torch.Tensor:
        if old_dist_spec.process_group != dist_spec.process_group:
            raise NotImplementedError
        if old_dist_spec == dist_spec:
            return tensor
        if len(old_dist_spec.dims) == 1 and len(dist_spec.dims) == 1:
            # use all-to-all to save memory
            return DistSpecManager._all_to_all(tensor, old_dist_spec, dist_spec)
        tensor = DistSpecManager._gather(tensor, old_dist_spec)
        return DistSpecManager._shard_as(tensor, old_dist_spec, dist_spec)

    @staticmethod
    def handle_trans_spec(tensor: torch.Tensor, old_dist_spec: _DistSpec, dist_spec: _DistSpec) -> torch.Tensor:
        forward_trans_handle = getattr(DistSpecManager, f'_{old_dist_spec.placement.value}2{dist_spec.placement.value}')
        if not DistSpecManager._use_autograd_function:
            return forward_trans_handle(tensor, old_dist_spec, dist_spec)
        backward_trans_handle = getattr(DistSpecManager,
                                        f'_{dist_spec.placement.value}2{old_dist_spec.placement.value}')
        return TransformDistSpec.apply(tensor, old_dist_spec, dist_spec, forward_trans_handle, backward_trans_handle)

    @staticmethod
    @contextmanager
    def no_grad():
        try:
            DistSpecManager._use_autograd_function = False
            yield
        finally:
            DistSpecManager._use_autograd_function = True
