from enum import Enum
from typing import Callable, Dict, Optional, Tuple

import torch
import torch.distributed as dist
import torch.nn.functional as F
import triton
import triton.language as tl

from colossalai.kernel.kernel_loader import (
    FlashAttentionForFloatAndCustomMaskLoader,
    FlashAttentionLoader,
    FlashAttentionWithCustomMaskLoader,
    KernelLoader,
)

__all__ = [
    "AttnMaskType",
    "ColoAttention",
]

_flash_attn_forward = _flash_attn_backward = None


class AttnMaskType(Enum):
    CUSTOM = 0
    PADDED = 1
    CAUSAL = 2
    PADDED_CAUSAL = 3


def invert_mask(mask: torch.Tensor) -> torch.Tensor:
    """Invert the mask tensor.

    Args:
        mask (torch.Tensor): Mask tensor. Shape should be [B, 1, Sq, Skv]

    Returns:
        torch.Tensor: Inverted mask tensor.
    """
    inverted_mask = 1.0 - mask
    return inverted_mask.masked_fill(inverted_mask.bool(), torch.finfo(mask.dtype).min)


# adapted from https://github.com/Dao-AILab/flash-attention/blob/main/flash_attn/bert_padding.py
def get_pad_info(padding_mask: torch.Tensor) -> Tuple[int, torch.Tensor, torch.Tensor]:
    """Get padding information from padding mask.

    Args:
        padding_mask (torch.Tensor): Padding mask tensor. Shape should be [B, S]

    Returns:
        Tuple[int, torch.Tensor, torch.Tensor]: Tuple of (max_seq_len, cu_seqlens, indices)
    """
    seqlens_in_batch = padding_mask.sum(dim=-1, dtype=torch.int32)
    indices = torch.nonzero(padding_mask.flatten(), as_tuple=False).flatten()
    max_seqlen_in_batch = seqlens_in_batch.max().item()
    cu_seqlens = F.pad(torch.cumsum(seqlens_in_batch, dim=0, dtype=torch.int32), (1, 0))
    return max_seqlen_in_batch, cu_seqlens, indices


class ColoAttention:
    _kernel_dispatch_map: Optional[Dict[torch.dtype, Dict[Optional[AttnMaskType], Callable]]] = None

    @staticmethod
    def _init_kernels_dispatch():
        if ColoAttention._kernel_dispatch_map is None:
            # fp16/bf16
            half_dispatch_map = {
                None: FlashAttentionLoader(),
                AttnMaskType.CUSTOM: FlashAttentionWithCustomMaskLoader(),
                AttnMaskType.PADDED: FlashAttentionLoader(),
                AttnMaskType.CAUSAL: FlashAttentionLoader(),
                AttnMaskType.PADDED_CAUSAL: FlashAttentionLoader(),
            }
            # fp32
            float_dispatch_map = {
                None: FlashAttentionForFloatAndCustomMaskLoader(),
                AttnMaskType.CUSTOM: FlashAttentionForFloatAndCustomMaskLoader(),
                AttnMaskType.PADDED: FlashAttentionForFloatAndCustomMaskLoader(),
                AttnMaskType.CAUSAL: FlashAttentionForFloatAndCustomMaskLoader(),
                AttnMaskType.PADDED_CAUSAL: FlashAttentionForFloatAndCustomMaskLoader(),
            }
            ColoAttention._kernel_dispatch_map = {
                torch.float16: half_dispatch_map,
                torch.bfloat16: half_dispatch_map,
                torch.float32: float_dispatch_map,
            }

    @staticmethod
    def _dispatch_kernel(dtype: torch.dtype, mask_type: Optional[AttnMaskType]) -> Callable:
        ColoAttention._init_kernels_dispatch()
        if (
            dtype not in ColoAttention._kernel_dispatch_map
            or mask_type not in ColoAttention._kernel_dispatch_map[dtype]
        ):
            raise ValueError(
                "FlashAttention kernel is not available for dtype {} and mask_type {}".format(dtype, mask_type)
            )
        # lazy load
        if isinstance(ColoAttention._kernel_dispatch_map[dtype][mask_type], KernelLoader):
            ColoAttention._kernel_dispatch_map[dtype][mask_type] = ColoAttention._kernel_dispatch_map[dtype][
                mask_type
            ].load()
        return ColoAttention._kernel_dispatch_map[dtype][mask_type]

    @staticmethod
    def prepare_attn_kwargs(
        shape_4d: Tuple[int],
        dtype: torch.dtype,
        device: torch.device,
        q_padding_mask: Optional[torch.Tensor] = None,
        kv_padding_mask: Optional[torch.Tensor] = None,
        is_causal: bool = False,
    ) -> Dict[str, torch.Tensor]:
        """Return a dictionary of keyword arguments for attention function. It supports 4 mask type.
        1. custom mask: no padding mask and is_causal=False, return {}, users should handle attention mask by themselves.
        2. padded mask: recv padding mask and is_causal=False, return {attention_mask, attention_mask_type, cu_seqlens_q, cu_seqlens_kv, max_seqlen_q, max_seqlen_kv, q_indices, kv_indices}.
        3. causal mask: no padding mask and is_causal=True, return {attention_mask, attention_mask_type}.
        4. padded causal mask: recv padding mask and is_causal=True, return {attention_mask, attention_mask_type, cu_seqlens_q, cu_seqlens_kv, max_seqlen_q, max_seqlen_kv, q_indices, kv_indices}.

        Args:
            shape_4d (Tuple[int]): Should be (B, 1, Sq, Skv)
            dtype (torch.dtype): Dtype of attention mask, generally should be ``hidden_states.dtype``
            device (torch.device): Device of attention mask, generally should be ``hidden_states.device``
            q_padding_mask (Optional[torch.Tensor], optional): Padding mask of query. It should be a long tensor or int tensor.
                The shape should be [B, Sq]. ``1`` means valid token, and ``0`` means padding token. Defaults to None.
            kv_padding_mask (Optional[torch.Tensor], optional): Padding mask of key and value. It should be a long tensor or int tensor.
                The shape should be [B, Skv]. ``1`` means valid token, and ``0`` means padding token.
                If it's None and ``q_padding_mask`` is not None, it will be set to ``q_padding_mask``. Defaults to None.
            is_causal (bool, optional): Whether to use causal attention mask. Defaults to False.

        Returns:
            Dict[str, torch.Tensor]: Dictionary of keyword arguments for attention function.
        """
        if q_padding_mask is None and not is_causal:
            return {}
        assert len(shape_4d) == 4 and shape_4d[1] == 1
        b, _, s_q, s_kv = shape_4d
        outputs = {}
        if (q_padding_mask is None or q_padding_mask.bool().all()) and (
            kv_padding_mask is None or kv_padding_mask.bool().all()
        ):
            # no padding
            assert is_causal
            outputs["attention_mask_type"] = AttnMaskType.CAUSAL
            attention_mask = torch.ones(s_q, s_kv, dtype=dtype, device=device)
            if s_q != 1:
                attention_mask = attention_mask.tril(diagonal=0)
            attention_mask = attention_mask.expand(b, s_q, s_kv)
        else:
            max_seqlen_q, cu_seqlens_q, q_indices = get_pad_info(q_padding_mask)
            if kv_padding_mask is None:
                # self attention
                kv_padding_mask = q_padding_mask
                max_seqlen_kv, cu_seqlens_kv, kv_indices = max_seqlen_q, cu_seqlens_q, q_indices
            else:
                max_seqlen_kv, cu_seqlens_kv, kv_indices = get_pad_info(kv_padding_mask)
            assert kv_padding_mask.shape == (
                b,
                s_kv,
            ), f"q_padding_mask shape {kv_padding_mask.shape} should be the same. ({shape_4d})"
            attention_mask = kv_padding_mask[:, None, :].expand(b, s_q, s_kv).to(dtype=dtype, device=device)
            outputs.update(
                {
                    "cu_seqlens_q": cu_seqlens_q,
                    "cu_seqlens_kv": cu_seqlens_kv,
                    "max_seqlen_q": max_seqlen_q,
                    "max_seqlen_kv": max_seqlen_kv,
                    "q_indices": q_indices,
                    "kv_indices": kv_indices,
                }
            )
            if is_causal:
                outputs["attention_mask_type"] = AttnMaskType.PADDED_CAUSAL
                if s_q != 1:
                    attention_mask = attention_mask * attention_mask.new_ones(s_q, s_kv).tril(diagonal=0)
            else:
                outputs["attention_mask_type"] = AttnMaskType.PADDED
        attention_mask = invert_mask(attention_mask).unsqueeze(1)
        outputs["attention_mask"] = attention_mask
        return outputs

    @staticmethod
    def attention(
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        attention_mask_type: AttnMaskType = AttnMaskType.CUSTOM,
        cu_seqlens_q: Optional[torch.Tensor] = None,
        cu_seqlens_kv: Optional[torch.Tensor] = None,
        max_seqlen_q: Optional[int] = None,
        max_seqlen_kv: Optional[int] = None,
        q_indices: Optional[torch.Tensor] = None,
        kv_indices: Optional[torch.Tensor] = None,
        dropout_p: float = 0.0,
        scale: Optional[float] = None,
    ) -> torch.Tensor:
        """Flash Attention function. It supports 4 mask type.
        1. custom mask: recv attention_mask
        2. padded mask: recv attention_mask, attention_mask_type, cu_seqlens_q, cu_seqlens_kv, max_seqlen_q, max_seqlen_kv, indices
        3. causal mask: recv attention_mask, attention_mask_type
        4. padded causal mask: recv attention_mask, attention_mask_type, cu_seqlens_q, cu_seqlens_kv, max_seqlen_q, max_seqlen_kv, indices

        Args:
            q (torch.Tensor): Query tensor. Shape should be [B, Heads, Sq, D]
            k (torch.Tensor): Key tensor. Shape should be [B, Heads, Skv, D]
            v (torch.Tensor): Value tensor. Shape should be [B, Heads, Skv, D]
            attention_mask (Optional[torch.Tensor], optional): Attention mask tensor. Shape should be [B, 1, Sq, Skv]. Defaults to None.
            attention_mask_type (AttnMaskType, optional): Attention mask type. Defaults to AttnMaskType.CUSTOM.
            cu_seqlens_q (Optional[torch.Tensor], optional): The cumulative sequence lengths
                of the sequences in the batch, used to index into q.
                Shape should be [B+1]. Defaults to None.
            cu_seqlens_kv (Optional[torch.Tensor], optional): The cumulative sequence lengths
                of the sequences in the batch, used to index into kv.
                Shape should be [B+1]. Defaults to None.
            max_seqlen_q (Optional[int], optional): Maximum query sequence length in the batch. Defaults to None.
            max_seqlen_kv (Optional[int], optional): Maximum key/value sequence length in the batch. Defaults to None.
            indices (Optional[torch.Tensor], optional): The indices of non-masked tokens from the flattened input sequence.
                Shape should be [NUM_TOKENS]. Defaults to None.
            dropout_p (float, optional): Dropout probability. Defaults to 0.0.
            scale (Optional[float], optional): Scaling factor applied prior to softmax. Defaults to None.

        Returns:
            torch.Tensor: Output tensor. Shape should be [B, Heads, Sq, D]
        """
        # known issue: sdpa does not support attention mask which contains whole row of masked tokens, which leads to nan
        # this case is usaul when padding mask is used and self attention is performed
        # thus, we don't use sdpa when padding mask is used
        # sanity check
        if attention_mask is not None:
            assert torch.is_floating_point(attention_mask), "attention_mask should be a floating point tensor."
            if attention_mask_type in (
                AttnMaskType.CUSTOM,
                AttnMaskType.CAUSAL,
                AttnMaskType.PADDED,
                AttnMaskType.PADDED_CAUSAL,
            ):
                assert (
                    cu_seqlens_q is None
                    and cu_seqlens_kv is None
                    and max_seqlen_q is None
                    and max_seqlen_kv is None
                    and q_indices is None
                    and kv_indices is None
                )
                if attention_mask_type == AttnMaskType.CUSTOM:
                    assert not torch.all(attention_mask != 0, dim=-1).any()
        else:
            # if attention_mask is None, attention_mask_type should be the default value
            assert attention_mask_type == AttnMaskType.CUSTOM
        # kernel dispatch
        mask_type = attention_mask_type if attention_mask is not None else None
        attn_func = ColoAttention._dispatch_kernel(q.dtype, mask_type)
        is_causal = attention_mask is not None and attention_mask_type in (
            AttnMaskType.CAUSAL,
            AttnMaskType.PADDED_CAUSAL,
        )
        return attn_func(
            q,
            k,
            v,
            dropout_p=dropout_p,
            scale=scale,
            attention_mask=attention_mask,
            is_causal=is_causal,
            cu_seqlens_q=cu_seqlens_q,
            cu_seqlens_kv=cu_seqlens_kv,
            max_seqlen_q=max_seqlen_q,
            max_seqlen_kv=max_seqlen_kv,
            q_indices=q_indices,
            kv_indices=kv_indices,
        )


def _load_flash_attn():
    global _flash_attn_forward, _flash_attn_backward
    if _flash_attn_forward is not None and _flash_attn_backward is not None:
        return
    from flash_attn.flash_attn_interface import _flash_attn_varlen_backward as _flash_attn_backward
    from flash_attn.flash_attn_interface import _flash_attn_varlen_forward as _flash_attn_forward


def ring_attn_p2p_comm(sp_rank, send_tensor, recv_tensor, send_src, recv_src, sp_group):
    """No metadata as K, V sizes are fixed"""
    if sp_rank % 2 == 0:
        send_op = dist.P2POp(dist.isend, send_tensor, send_src, group=sp_group)
        recv_op = dist.P2POp(dist.irecv, recv_tensor, recv_src, group=sp_group)
        send_recv_ops = [send_op, recv_op]
    else:
        recv_op = dist.P2POp(dist.irecv, recv_tensor, recv_src, group=sp_group)
        send_op = dist.P2POp(dist.isend, send_tensor, send_src, group=sp_group)
        send_recv_ops = [recv_op, send_op]

    reqs = dist.batch_isend_irecv(send_recv_ops)
    return reqs


@triton.jit
def flash_attn_fwd_out_corr_triton(
    out_ptr, out_per_step_ptr, seq_dim, softmax_lse_ptr, softmax_lse_per_step_ptr, BLOCK_SIZE: tl.constexpr
):
    # Calculate the global id
    pid = tl.program_id(0)

    # Offsets for the current row
    offsets = tl.arange(0, BLOCK_SIZE)

    # Pointers to the current row in out and out_per_step
    row_start = pid * seq_dim
    out_ptrs = out_ptr + row_start + offsets
    out_per_step_ptrs = out_per_step_ptr + row_start + offsets

    # Load softmax_lse and softmax_lse_per_step
    softmax_lse = tl.load(softmax_lse_ptr + pid)
    softmax_lse_per_step = tl.load(softmax_lse_per_step_ptr + pid)

    # Compute the corrected exponentiation
    softmax_lse_corrected_exp = tl.exp(softmax_lse_per_step - softmax_lse)

    out_per_step_vals = tl.load(out_per_step_ptrs)

    # Correct the out_per_step by the exponentiation
    out_corrected = out_per_step_vals * softmax_lse_corrected_exp

    # Load the current out values
    out_vals = tl.load(out_ptrs)

    # Add the corrected output to out
    updated_out_vals = out_vals + out_corrected

    # Store the updated out values
    tl.store(out_ptrs, updated_out_vals)


# Modified from Megatron-LM. TODO: try Triton
def flash_attn_out_correction(out, out_per_step, seq_dim, softmax_lse, softmax_lse_per_step):
    softmax_lse_corrected_exp = torch.exp(softmax_lse_per_step - softmax_lse).movedim(2, seq_dim)
    softmax_lse_corrected_exp = softmax_lse_corrected_exp.unsqueeze(-1)
    out_corrected = out_per_step * softmax_lse_corrected_exp
    out.add_(out_corrected)


def flash_attn_softmax_lse_correction(softmax_lse, softmax_lse_per_step):
    max_scale = torch.max(softmax_lse, softmax_lse_per_step)
    min_scale = torch.min(softmax_lse, softmax_lse_per_step)
    new_scale = max_scale + torch.log(1 + torch.exp(min_scale - max_scale))
    softmax_lse.copy_(new_scale)


class RingAttention(torch.autograd.Function):
    """Implements the Ring Attention from `Ring Attention with Blockwise Transformers for Near-Infinite Context`
    (https://arxiv.org/abs/2310.01889).
    We referenced the context parallel in Megatron-LM, with several critical optimizations
    such as removing the negative optimization of using two streams for attn forward, torch.compile and reusing K, V buffers.
    For load-balancing we adopted the "zigzag" attention scheme from https://github.com/zhuzilin/ring-flash-attention/tree/main
    For portable integration with more models, we don't follow the spirit of "block-wise FNN" in the original paper,
    which requires fusing FFN with the Flash Attention kernel/function (see https://arxiv.org/pdf/2305.19370;
    implemented in Jax and not optimized).

    """

    # TODO: Support arbitary seq length by padding to multiple of cp_size
    @staticmethod
    def forward(
        ctx,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        sp_group: dist.ProcessGroup,
        sp_stream: torch.cuda.Stream,
        attention_mask: Optional[torch.Tensor] = None,
        attention_mask_type: AttnMaskType = AttnMaskType.CUSTOM,
        cu_seqlens_q: Optional[torch.Tensor] = None,
        cu_seqlens_kv: Optional[torch.Tensor] = None,
        max_seqlen_q: Optional[int] = None,
        max_seqlen_kv: Optional[int] = None,
        q_indices: Optional[torch.Tensor] = None,
        kv_indices: Optional[torch.Tensor] = None,
        dropout_p: float = 0.0,
        scale: Optional[float] = None,
    ):
        """
        Args:
            q (torch.Tensor): Query tensor. Shape should be [B, Heads, Sq, D]
            k (torch.Tensor): Key tensor. Shape should be [B, Heads, Skv, D]
            v (torch.Tensor): Value tensor. Shape should be [B, Heads, Skv, D]
            sp_group (Optional[dist.ProcessGroup]): Process group for sequence parallelism
            sp_tream (torch.cuda.Stream): An different stream for output correction.
            attention_mask (Optional[torch.Tensor], optional): Attention mask tensor. Shape should be [B, 1, Sq, Skv]. Defaults to None.
            attention_mask_type (AttnMaskType, optional): Attention mask type. Defaults to AttnMaskType.CUSTOM.
            cu_seqlens_q (Optional[torch.Tensor], optional): The cumulative sequence lengths
                of the sequences in the batch, used to index into q.
                Shape should be [B+1]. Defaults to None.
            cu_seqlens_kv (Optional[torch.Tensor], optional): The cumulative sequence lengths
                of the sequences in the batch, used to index into kv.
                Shape should be [B+1]. Defaults to None.
            max_seqlen_q (Optional[int], optional): Maximum query sequence length in the batch. Defaults to None.
            max_seqlen_kv (Optional[int], optional): Maximum key/value sequence length in the batch. Defaults to None.
            indices (Optional[torch.Tensor], optional): The indices of non-masked tokens from the flattened input sequence.
                Shape should be [NUM_TOKENS]. Defaults to None.
            dropout_p (float, optional): Dropout probability. Defaults to 0.0.
            scale (Optional[float], optional): Scaling factor applied prior to softmax. Defaults to None.

        Returns:
            torch.Tensor: Output tensor. Shape should be [B, Heads, Sq, D]
        """
        if attention_mask is not None:
            assert torch.is_floating_point(attention_mask), "attention_mask should be a floating point tensor."
            assert attention_mask_type in (
                AttnMaskType.PADDED_CAUSAL,
                AttnMaskType.CAUSAL,
            ), "Ring attention doesn't support non-causal attention"
            assert (
                cu_seqlens_q is not None
                and cu_seqlens_kv is not None
                and max_seqlen_q is not None
                and max_seqlen_kv is not None
                and q_indices is not None
                and kv_indices is not None
            )
        try:
            _load_flash_attn()
        except Exception as e:
            raise RuntimeError(
                f"Ring attention requires Flash Attention, but import failed. You can re-install it via 'pip install flash-attn --no-build-isolation'"
            ) from e

        # (B, Sq, H, D) -> (B, H, 2, Sq // 2, D)
        q, k, v = [x.transpose(1, 2).view(*x.shape[:1], 2, x.shape[1] // 2, *x.shape[2:]) for x in (q, k, v)]

        sp_size = dist.get_world_size(sp_group)
        sp_rank = dist.get_rank(sp_group)
        sp_global_ranks = dist.get_process_group_ranks(sp_group)
        send_dst = sp_global_ranks[(sp_rank + 1) % sp_size]
        recv_src = sp_global_ranks[(sp_rank - 1) % sp_size]

        # Pre-allocate double buffer for overlapping and receiving next step's inputs
        q_inputs = [q[:, 0], q[:, 1]]  # (B, 2, Sq // 2, H, D)
        kv_inputs = [torch.stack(k, v)]  # (2, B, 2, Skv // 2, H, D)
        kv_inputs.append(torch.empty_like(kv_inputs[0]))
        del k, v

        # outputs
        out_per_step = [None, None]
        softmax_lse_per_step = [None, None]
        rng_states = [None, None]

        # Overlap output correction with flash attn
        [torch.cuda.current_stream(), sp_stream]
        p2p_reqs = [[], []]
        for i in range(sp_size + 1):
            # Wait for current kv from prev rank
            for req in p2p_reqs[(i + 1) % 2]:
                req.wait()

            if i < sp_size:
                p2p_reqs[i % 2] = ring_attn_p2p_comm(
                    sp_rank,
                    kv_inputs[i % 2],  # send current kv to next rank
                    kv_inputs[(i + 1) % 2],  # recv from prev rank
                    send_dst,
                    recv_src,
                    sp_group,
                )

            if i == 0:
                # Compute with local KV; no mask
                q_input = torch.cat(q_inputs, dim=1).flatten(end_dim=2)  # (B * Sq, H, D)
                kv_input = kv_inputs[i % 2].flatten(
                    start_dim=1, end_dim=3
                )  # (2, B, 2, Skv // 2, H, D) -> (2, B * Skv, H, D)
                (
                    _,
                    _,
                    _,
                    _,
                    out_per_step[i % 2],
                    softmax_lse_per_step[i % 2],
                    _,
                    rng_states[i % 2],
                ) = _flash_attn_forward(
                    q_input,
                    kv_input[0],
                    kv_input[1],
                    cu_seqlens_q,
                    cu_seqlens_kv,
                    max_seqlen_q,
                    max_seqlen_kv,
                    dropout_p,
                    scale,
                    causal=True,
                    return_softmax=True,
                )
            elif i <= sp_rank:
                q_input = torch.cat(q_inputs, dim=1)  # (B, Sq, H, D)
                kv_input = kv_inputs[i % 2][0]  # (2, B, 2, Skv // 2, H, D)
                # Drop the second half of received kv
                kv_input = kv_input[:, :, 0].flatten(
                    start_dim=1, end_dim=3
                )  # (2, B, Skv / 2, H, D) -> (2, B * Skv / 2, H, D)
                (
                    _,
                    _,
                    _,
                    _,
                    out_per_step[i % 2],
                    softmax_lse_per_step[i % 2],
                    _,
                    rng_states[i % 2],
                ) = _flash_attn_forward(
                    q_input,
                    kv_input[0],
                    kv_input[1],
                    cu_seqlens_q,
                    cu_seqlens_kv,
                    max_seqlen_q,
                    max_seqlen_kv,
                    dropout_p,
                    scale,
                    causal=True,
                    return_softmax=True,
                )
