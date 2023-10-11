from typing import Any, Callable, Optional, Tuple

import torch
import torch.distributed as dist
import torch.nn as nn
from torch import Tensor
from torch.cuda.amp import custom_bwd, custom_fwd
from torch.distributed import ProcessGroup

from colossalai.moe.manager import MOE_MANAGER

MOE_KERNEL = None


def load_moe():
    global MOE_KERNEL
    from colossalai.kernel.op_builder import MOEBuilder

    MOE_KERNEL = MOEBuilder().load()


class TPOverlap(torch.autograd.Function):

    @staticmethod
    def forward(
        ctx: Any,
        experts: nn.Module,
        dispatch_data: Tensor,
        group: ProcessGroup,
    ) -> Tensor:

        NUM_CHUNK = 1
        NUM_STAGES = 4
        ctx.save_for_backward(experts, dispatch_data)

        assert dispatch_data.shape[0] % NUM_CHUNK == 0, \
            "arbitrary chunk num is not supported yet, please use chunk num that can divide num_experts"
        chunk_size = dispatch_data.shape[0] // NUM_CHUNK
        chunk_data = torch.split(dispatch_data, chunk_size, dim=0)
        output = torch.empty_like(dispatch_data)

        def get_chunk_slice(idx: int, chunk_size: int) -> Tuple[slice]:
            return (slice(idx * chunk_size, (idx + 1) * chunk_size), )

        expert_in, in_handle, input_indices = None, None, None
        partial_expert_out, data_indices = None, None
        expert_out, out_handle, output_indices = None, None, None

        for i in range(NUM_CHUNK + NUM_STAGES - 1):
            if out_handle is not None:
                out_handle.wait()
                output[output_indices] = expert_out
                expert_out, out_handle, output_indices = None, None, None

            # reduce scatter last output
            if partial_expert_out is not None:
                output_indices = data_indices
                expert_out, out_handle = ReduceScatter.apply(partial_expert_out, group, True)
                partial_expert_out = None

            # compute
            if in_handle is not None:
                in_handle.wait()
                data_indices = input_indices
                partial_expert_out = experts(expert_in, input_indices)
                expert_in, in_handle, input_indices = None, None, None

            # all gather next input
            if 0 <= i < NUM_CHUNK:
                input_indices = get_chunk_slice(i, chunk_size)
                expert_in, in_handle = AllGather.apply(chunk_data[i].contiguous(), group, True)

        return output

    @staticmethod
    def backward(ctx: Any, grad_outputs: Tensor) -> Tuple[Tensor, None, None]:
        raise NotImplementedError()


class AllGather(torch.autograd.Function):

    @staticmethod
    def forward(
        ctx: Any,
        inputs: Tensor,
        group: Optional[ProcessGroup] = None,
        overlap: bool = False,
    ) -> Tuple[Tensor, Optional[Callable]]:
        """
        Returns:
            outputs: Tensor
            handle: Optional[Callable], if overlap is True
        """

        if ctx is not None:
            ctx.comm_grp = group
            ctx.overlap = overlap

        comm_size = dist.get_world_size(group)
        if comm_size == 1:
            return inputs.unsqueeze(0), None

        buffer_shape = (comm_size,) + inputs.shape
        outputs = torch.empty(buffer_shape, dtype=inputs.dtype, device=inputs.device)
        buffer_list = list(torch.chunk(outputs, comm_size, dim=0))
        if not overlap:
            dist.all_gather(buffer_list, inputs, group=group)
            return outputs, None
        else:
            handle = dist.all_gather(buffer_list, inputs, group=group, async_op=True)
            if ctx is None and overlap:
                global WORLD_HANDLE_ALLGATHER
                WORLD_HANDLE_ALLGATHER = handle
            return outputs, handle

    @staticmethod
    def backward(ctx: Any, *grad_outputs) -> Tuple[Tensor, None, None]:
        return (
            ReduceScatter.forward(None, grad_outputs[0], ctx.comm_grp, ctx.overlap)[0],
            None,
            None,
        )


class ReduceScatter(torch.autograd.Function):

    @staticmethod
    def forward(
        ctx: Any,
        inputs: Tensor,
        group: Optional[ProcessGroup] = None,
        overlap: bool = False,
    ) -> Tuple[Tensor, Optional[Callable]]:
        """
        Returns:
            outputs: Tensor
            handle: Optional[Callable], if overlap is True
        """

        if ctx is not None:
            ctx.comm_grp = group
            ctx.overlap = overlap

        comm_size = dist.get_world_size(group)
        if comm_size == 1:
            return inputs.squeeze(0), None

        if not inputs.is_contiguous():
            inputs = inputs.contiguous()

        output_shape = inputs.shape[1:]
        outputs = torch.empty(output_shape, dtype=inputs.dtype, device=inputs.device)
        buffer_list = list(torch.chunk(inputs, comm_size, dim=0))
        if not overlap:
            dist.reduce_scatter(outputs, buffer_list, group=group)
            return outputs, None
        else:
            handle = dist.reduce_scatter(outputs, buffer_list, group=group, async_op=True)
            if ctx is None and overlap:
                global WORLD_HANDLE_REDUCESCATTER
                WORLD_HANDLE_REDUCESCATTER = handle
            return outputs, handle

    @staticmethod
    def backward(ctx: Any, *grad_outputs) -> Tuple[Tensor, None, None]:
        return (
            AllGather.forward(None, grad_outputs[0], ctx.comm_grp, ctx.overlap)[0],
            None,
            None,
        )


class AllToAll(torch.autograd.Function):
    """Dispatches input tensor [e, c, h] to all experts by all_to_all_single
    operation in torch.distributed.
    """

    @staticmethod
    def forward(ctx: Any, inputs: Tensor, group: Optional[ProcessGroup] = None) -> Tensor:
        if ctx is not None:
            ctx.comm_grp = group
        if not inputs.is_contiguous():
            inputs = inputs.contiguous()
        if dist.get_world_size(group) == 1:
            return inputs
        output = torch.empty_like(inputs)
        dist.all_to_all_single(output, inputs, group=group)
        return output

    @staticmethod
    def backward(ctx: Any, *grad_outputs: Tensor) -> Tuple[Tensor, None]:
        return AllToAll.forward(None, *grad_outputs, ctx.comm_grp), None


class MoeDispatch(torch.autograd.Function):

    @staticmethod
    @custom_fwd
    def forward(ctx, tokens, mask, dest_idx, ec):
        s = tokens.size(0)
        h = tokens.size(1)
        dtype = tokens.dtype

        if MOE_KERNEL is None:
            load_moe()
        if tokens.dtype != torch.float32:
            tokens = tokens.to(torch.float32)
        expert_input = MOE_KERNEL.dispatch_forward(s, ec, h, tokens, mask, dest_idx)
        if expert_input.dtype != dtype:
            expert_input = expert_input.to(dtype)
        ctx.save_for_backward(mask, dest_idx)
        ctx.s = s
        ctx.h = h
        ctx.ec = ec
        ctx.dtype = dtype

        return expert_input

    @staticmethod
    @custom_bwd
    def backward(ctx, output_grad):
        mask, dest_idx = ctx.saved_tensors
        if output_grad.dtype != torch.float32:
            output_grad = output_grad.to(torch.float32)
        d_tokens = MOE_KERNEL.dispatch_backward(ctx.s, ctx.ec, ctx.h, output_grad, mask, dest_idx)
        if d_tokens.dtype != ctx.dtype:
            d_tokens = d_tokens.to(ctx.dtype)
        return d_tokens, None, None, None


class MoeCombine(torch.autograd.Function):

    @staticmethod
    @custom_fwd
    def forward(ctx, expert_tokens, logits, mask, dest_idx, ec):
        assert logits.dtype == torch.float32

        s = logits.size(0)
        e = logits.size(1)
        c = ec // e
        h = expert_tokens.size(-1)
        dtype = expert_tokens.dtype

        if expert_tokens.dtype != torch.float32:
            expert_tokens = expert_tokens.to(torch.float32)
        if MOE_KERNEL is None:
            load_moe()
        output = MOE_KERNEL.combine_forward(s, e, c, h, expert_tokens, logits, mask, dest_idx)
        if output.dtype != dtype:
            output = output.to(dtype)

        ctx.save_for_backward(expert_tokens, logits, mask, dest_idx)
        ctx.s = s
        ctx.e = e
        ctx.c = c
        ctx.h = h
        ctx.dtype = dtype

        return output

    @staticmethod
    @custom_bwd
    def backward(ctx, tokens_grad):
        expert_tokens, logits, mask, dest_idx = ctx.saved_tensors
        if tokens_grad.dtype != torch.float32:
            tokens_grad = tokens_grad.to(torch.float32)

        d_expert, d_logits = MOE_KERNEL.combine_backward(ctx.s, ctx.e, ctx.c, ctx.h, tokens_grad, expert_tokens, logits,
                                                         mask, dest_idx)
        if d_expert.dtype != ctx.dtype:
            d_expert = d_expert.to(ctx.dtype)

        return d_expert, d_logits, None, None, None


def moe_cumsum(inputs: Tensor):
    dim0 = inputs.size(0)
    flag = (dim0 <= 1024) or (dim0 <= 2048 and dim0 % 2 == 0) or (dim0 % 4 == 0)
    if flag and MOE_MANAGER.use_kernel_optim:
        if MOE_KERNEL is None:
            load_moe()
        return MOE_KERNEL.cumsum_sub_one(inputs)
    else:
        return torch.cumsum(inputs, dim=0) - 1


class MoeInGradScaler(torch.autograd.Function):
    """
    Scale the gradient back by the number of experts
    because the batch size increases in the moe stage
    """

    @staticmethod
    def forward(ctx: Any, inputs: Tensor, ep_size: int) -> Tensor:
        if ctx is not None:
            ctx.ep_size = ep_size
        return inputs

    @staticmethod
    def backward(ctx: Any, *grad_outputs: Tensor) -> Tuple[Tensor, None]:
        assert len(grad_outputs) == 1
        grad = grad_outputs[0]
        if ctx.ep_size != 1:
            grad = grad * ctx.ep_size
        return grad, None


class MoeOutGradScaler(torch.autograd.Function):
    """
    Scale the gradient by the number of experts
    because the batch size increases in the moe stage
    """

    @staticmethod
    def forward(ctx: Any, inputs: Tensor, ep_size: int) -> Tensor:
        ctx.ep_size = ep_size
        return inputs

    @staticmethod
    def backward(ctx: Any, *grad_outputs: Tensor) -> Tuple[Tensor, None]:
        assert len(grad_outputs) == 1
        grad = grad_outputs[0]
        if ctx.ep_size != 1:
            grad = grad / ctx.ep_size
        return grad, None
