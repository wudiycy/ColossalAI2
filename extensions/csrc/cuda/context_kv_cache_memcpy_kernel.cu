#include <ATen/cuda/CUDAContext.h>
#include <torch/extension.h>

#include "../common/vector_copy_utils.h"
#include "../common/micros.h"

template<typename scalar_t, typename MT, int VecSize>
__global__ void context_kv_cache_memcpy_kernel(
    const scalar_t* __restrict__ key,
    const scalar_t* __restrict__ value,
    scalar_t* __restrict__ key_cache,
    scalar_t* __restrict__ value_cache,
    const int* __restrict__ sequence_lengths,
    const int* __restrict__ cu_seqlens,
    const int* __restrict__ block_tables,
    const int head_num,
    const int head_dim,
    const int block_size,
    const int batch_size,
    const int block_table_stride,
    const int64_t key_stride,
    const int64_t value_stride
)
{
    const int seq_token_id = blockIdx.x;
    const int seq_id = blockIdx.y;
    const int block_id = block_tables[seq_id * block_table_stride + seq_token_id / block_size];

    if ( block_id < 0 || seq_token_id > sequence_lengths[seq_id] - 1) {
        return ;
    }

    const int block_offset = seq_token_id % block_size;
    const int hidden_size = head_num * head_dim;
    const int total_token_id = cu_seqlens[seq_id] + seq_token_id;


    for (int i = threadIdx.x * VecSize; i < hidden_size; i += blockDim.x * VecSize) {
        const int head_id = i / head_dim;
        const int head_offset = i % head_dim;
        const int64_t key_src_id = total_token_id * key_stride + i;
        const int64_t value_src_id = total_token_id * value_stride + i;
        const int64_t target_id = block_id * hidden_size * block_size
                                      + head_id * block_size * head_dim
                                      + block_offset * head_dim + head_offset;

        copy_vector<scalar_t, VecSize>(key_cache + target_id, key + key_src_id);
        copy_vector<scalar_t, VecSize>(value_cache + target_id, value + value_src_id);
    }
}

template<typename scalar_t>
void apply_context_kv_cache_memcpy(
    at::Tensor& key,                 // [num_tokens, head_num, head_dim]
    at::Tensor& value,               // [num_tokens, head_num, head_dim]
    at::Tensor& key_cache,           // [num_blocks, head_num, block_size, head_dim]
    at::Tensor& value_cache,         // [num_blocks, head_num, block_size, head_dim]
    at::Tensor& sequence_lengths,    // [batch_size]
    at::Tensor& cu_seqlens,          // [batch_size + 1]
    at::Tensor& block_tables,        // [batch_size, max_seq_len]
    int max_seq_len_in_batch)
{
    int num_tokens = key.size(0);
    int head_num = key.size(1);
    int head_dim = key.size(2);
    int block_size = key_cache.size(2);
    int batch_size = block_tables.size(0);

    int64_t key_stride = key.stride(0);
    int64_t value_stride = value.stride(0);
    int block_table_stride = block_tables.stride(0);

    int vec_size = get_vec_size<scalar_t>(key);

    if (head_dim % vec_size != 0) {
        // Disable vectorized loading optimization when head_dim is not divisible by VecSize.
        vec_size = 1;
    }

    int thread_nums = head_num * head_dim / vec_size;

    const cudaStream_t stream = at::cuda::getCurrentCUDAStream();

    dim3 grid(max_seq_len_in_batch, batch_size);
    dim3 block(std::min(thread_nums, 512));

    switch (vec_size) {
        case 1:
            context_kv_cache_memcpy_kernel<scalar_t, 1><<<grid, block, 0, stream>>>(
                key.data_ptr<scalar_t>(),
                value.data_ptr<scalar_t>(),
                key_cache.data_ptr<scalar_t>(),
                value_cache.data_ptr<scalar_t>(),
                sequence_lengths.data_ptr<int>(),
                cu_seqlens.data_ptr<int>(),
                block_tables.data_ptr<int>(),
                head_num,
                head_dim,
                block_size,
                batch_size,
                block_table_stride,
                key_stride,
                value_stride
            );
            break;
        case 2:
            context_kv_cache_memcpy_kernel<scalar_t, 2><<<grid, block, 0, stream>>>(
                key.data_ptr<scalar_t>(),
                value.data_ptr<scalar_t>(),
                key_cache.data_ptr<scalar_t>(),
                value_cache.data_ptr<scalar_t>(),
                sequence_lengths.data_ptr<int>(),
                cu_seqlens.data_ptr<int>(),
                block_tables.data_ptr<int>(),
                head_num,
                head_dim,
                block_size,
                batch_size,
                block_table_stride,
                key_stride,
                value_stride
            );
            break;
        case 4:
            context_kv_cache_memcpy_kernel<scalar_t, 4><<<grid, block, 0, stream>>>(
                key.data_ptr<scalar_t>(),
                value.data_ptr<scalar_t>(),
                key_cache.data_ptr<scalar_t>(),
                value_cache.data_ptr<scalar_t>(),
                sequence_lengths.data_ptr<int>(),
                cu_seqlens.data_ptr<int>(),
                block_tables.data_ptr<int>(),
                head_num,
                head_dim,
                block_size,
                batch_size,
                block_table_stride,
                key_stride,
                value_stride
            );
            break;
        default:
            AT_ERROR("Unsupported vectorized size ", vec_size);
            break;
    }

    AT_CUDA_CHECK(cudaGetLastError());

}

void context_kv_cache_memcpy(
    at::Tensor& key,                 // [num_tokens, head_num, head_dim]
    at::Tensor& value,               // [num_tokens, head_num, head_dim]
    at::Tensor& key_cache,           // [num_blocks, head_num, block_size, head_dim]
    at::Tensor& value_cache,         // [num_blocks, head_num, block_size, head_dim]
    at::Tensor& sequence_lengths,    // [batch_size]
    at::Tensor& cu_seqlens,          // [batch_size + 1]
    at::Tensor& block_tables,        // [batch_size, max_seq_len]
    int max_seq_len_in_batch)
{
    DISPATCH_FLOAT_HALF_AND_BFLOAT(
        key.scalar_type(),
        "context_kv_cache_memcpy",
        apply_context_kv_cache_memcpy<scalar_t>(
            key,
            value,
            key_cache,
            value_cache,
            sequence_lengths,
            cu_seqlens,
            block_tables,
            max_seq_len_in_batch
        );)
}
