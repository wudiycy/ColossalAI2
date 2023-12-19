import torch
import torch.nn.functional as F

from colossalai.kernel.triton import context_attention_unpadded
from colossalai.testing import clear_cache_before_run, parameterize
from colossalai.utils import get_current_device


def torch_attn_ref(q: torch.Tensor, k: torch.Tensor, v: torch.Tensor, seq_len: int, num_heads: int, head_size: int):
    # Consider MHA for now
    # For a single sequence, q,k,v [seq_len, num_heads, head_size]
    assert q.shape[-1] == k.shape[-1] == v.shape[-1] == head_size

    q = q.view(1, seq_len, num_heads, head_size)
    k = k.view(1, seq_len, num_heads, head_size)
    v = v.view(1, seq_len, num_heads, head_size)
    q = q.transpose(1, 2)
    k = k.transpose(1, 2)
    v = v.transpose(1, 2)

    mask = torch.tril(torch.ones(1, 1, seq_len, seq_len), diagonal=0).to(device=get_current_device())
    mask[mask == 0.0] = float("-inf")
    mask = mask.repeat(1, num_heads, 1, 1)

    qk = torch.matmul(q, k.transpose(2, 3))
    attn_scores = qk / (head_size**0.5)
    attn_weights = F.softmax(attn_scores.to(dtype=torch.float32) + mask, dim=-1).to(dtype=q.dtype)
    out = torch.matmul(attn_weights, v).transpose(1, 2).contiguous()
    out = out.reshape(-1, num_heads, head_size)
    return out


def torch_attn_unpad(q: torch.Tensor, k: torch.Tensor, v: torch.Tensor, context_lengths: torch.Tensor):
    # Process sequence one by one and cat them together.
    # Consider MHA for now.
    # q,k,v [num_tokens(sum(context_lengths)), num_heads, head_size]
    assert context_lengths.dim() == 1, "context_lengths should be a 1D tensor"

    _, num_heads, head_size = q.shape
    out_torch = []
    start_idx = 0
    for i in range(len(context_lengths)):
        end_idx = start_idx + context_lengths[i].item()
        torch_attn_ref_out = torch_attn_ref(
            q[start_idx:end_idx], k[start_idx:end_idx], v[start_idx:end_idx], end_idx - start_idx, num_heads, head_size
        )
        out_torch.append(torch_attn_ref_out)
        start_idx = end_idx
    return torch.cat(out_torch, dim=0)


@clear_cache_before_run()
@parameterize("bsz", [4, 7, 9])
@parameterize("block_size", [8, 16])
@parameterize("max_num_blocks_per_seq", [7, 10])
@parameterize("same_context_len", [True, False])
def test_context_attention(bsz, block_size, max_num_blocks_per_seq, same_context_len):
    torch.manual_seed(123)

    dtype = torch.float16
    device = get_current_device()
    num_seqs = bsz
    num_heads = 16
    # Consider MHA for now and thus num_kv_heads == num_heads
    num_kv_heads = num_heads
    head_size = 128
    # block_size = 8
    # max_num_blocks_per_seq = 10
    max_seq_len = max_num_blocks_per_seq * block_size
    num_seqs * max_num_blocks_per_seq

    if same_context_len:
        context_lengths = torch.tensor([max_seq_len for _ in range(num_seqs)], dtype=torch.int32, device=device)
    else:
        context_lengths = torch.randint(low=1, high=max_seq_len, size=(num_seqs,), dtype=torch.int32, device=device)
    print("context_lengths: \n", context_lengths)
    num_tokens = torch.sum(context_lengths).item()

    qkv = torch.randn(size=(num_tokens, num_heads + 2 * num_kv_heads, head_size), dtype=dtype, device=device)
    q, k, v = torch.split(qkv, [num_heads, num_kv_heads, num_kv_heads], dim=-2)

    # Mock allocation on block tables
    block_id = 0
    block_tables = torch.full(size=(num_seqs, max_num_blocks_per_seq), fill_value=-1, dtype=torch.int32)
    for i, seq_len in enumerate(context_lengths.tolist()):
        right_bound = (seq_len + block_size - 1) // block_size  # open bound
        block_tables[i, :right_bound] = torch.arange(block_id, block_id + right_bound, dtype=torch.int32)
        block_id += right_bound
    block_tables = block_tables.to(device=device)

    out_torch = torch_attn_unpad(q, k, v, context_lengths)
    out_triton = context_attention_unpadded(q, k, v, context_lengths, block_tables)

    print("Out shapes:\n", out_torch.shape, out_triton.shape)

    assert torch.allclose(out_torch, out_triton, atol=1e-2, rtol=1e-3)


if __name__ == "__main__":
    test_context_attention()
