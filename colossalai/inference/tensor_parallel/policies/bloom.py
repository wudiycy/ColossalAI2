from functools import partial

import torch
from torch.nn import LayerNorm

import colossalai.shardformer.layer as col_nn
from colossalai.shardformer.modeling.bloom import build_bloom_alibi_tensor_fn
from colossalai.shardformer.policies.base_policy import ModulePolicyDescription, SubModuleReplacementDescription
from colossalai.shardformer.policies.bloom import BloomForCausalLMPolicy

from ..modeling.bloom import BloomInferenceForwards

try:
    from colossalai.kernel.triton.fused_layernorm import layer_norm
    HAS_TRITON_NORM = True
except:
    print("you should install triton from https://github.com/openai/triton")
    HAS_TRITON_NORM = False


def get_triton_layernorm_forward():
    if HAS_TRITON_NORM:

        def _triton_layernorm_forward(self: LayerNorm, hidden_states: torch.Tensor):
            return layer_norm(hidden_states, self.weight.data, self.bias, self.eps)

        return _triton_layernorm_forward
    else:
        return None


class BloomModelInferPolicy(BloomForCausalLMPolicy):

    def __init__(self) -> None:
        super().__init__()

    def module_policy(self):
        from transformers.models.bloom.modeling_bloom import BloomAttention, BloomBlock, BloomForCausalLM, BloomModel
        policy = {}
        if not self.gptq:
            policy = super().module_policy()
        else:
            policy[BloomModel] = ModulePolicyDescription(
                attribute_replacement={
                    "num_heads": self.model.config.n_head // self.shard_config.tensor_parallel_size,
                },
                method_replacement={
                    "build_alibi_tensor": build_bloom_alibi_tensor_fn(self.shard_config.tensor_parallel_process_group)
                },
                sub_module_replacement=[
                    SubModuleReplacementDescription(
                        suffix="word_embeddings",
                        target_module=col_nn.VocabParallelEmbedding1D,
                    )
                ])
        # NOTE set inference mode to shard config
        self.shard_config._infer()

        method_replacement = {
            'forward': BloomInferenceForwards.bloom_for_causal_lm_forward,
            'prepare_inputs_for_generation': BloomInferenceForwards.bloom_for_causal_lm_prepare_inputs_for_generation
        }
        self.append_or_create_method_replacement(description=method_replacement,
                                                 policy=policy,
                                                 target_key=BloomForCausalLM)

        method_replacement = {'forward': BloomInferenceForwards.bloom_model_forward}
        self.append_or_create_method_replacement(description=method_replacement, policy=policy, target_key=BloomModel)

        method_replacement = {'forward': BloomInferenceForwards.bloom_block_forward}
        self.append_or_create_method_replacement(description=method_replacement, policy=policy, target_key=BloomBlock)

        method_replacement = {'forward': BloomInferenceForwards.bloom_attention_forward}
        self.append_or_create_method_replacement(description=method_replacement,
                                                 policy=policy,
                                                 target_key=BloomAttention)

        if HAS_TRITON_NORM:
            infer_method = get_triton_layernorm_forward()
            method_replacement = {'forward': partial(infer_method)}
            self.append_or_create_method_replacement(description=method_replacement,
                                                     policy=policy,
                                                     target_key=LayerNorm)

        return policy
