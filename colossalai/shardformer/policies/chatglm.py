from typing import Dict, Union

import torch.nn as nn

import colossalai.shardformer.layer as col_nn

from .basepolicy import ModulePolicyDescription, Policy, SubModuleReplacementDescription

__all__ = ['ChatGLMModelPolicy', 'ChatGLMForConditionalGenerationPolicy']


class ChatGLMModelPolicy(Policy):

    def config_sanity_check(self):
        pass

    def preprocess(self):
        # Resize embedding
        vocab_size = self.model.config.vocab_size
        world_size = self.shard_config.tensor_parallel_size

        if vocab_size % world_size != 0:
            new_vocab_size = vocab_size + world_size - vocab_size % world_size
            self.model.resize_token_embeddings(new_vocab_size)

        return self.model

    def module_policy(self) -> Dict[Union[str, nn.Module], ModulePolicyDescription]:
        from tests.kit.model_zoo.transformers.chatglm2_6b.modeling_chatglm import ChatGLMModel, GLMBlock

        policy = {}

        if self.shard_config.enable_tensor_parallelism:

            policy[ChatGLMModel] = ModulePolicyDescription(attribute_replacement={},
                                                           sub_module_replacement=[
                                                               SubModuleReplacementDescription(
                                                                   suffix="embedding.word_embeddings",
                                                                   target_module=col_nn.VocabParallelEmbedding1D,
                                                               )
                                                           ])

            policy[GLMBlock] = ModulePolicyDescription(attribute_replacement={
                "self_attention.num_attention_heads_per_partition":
                    self.model.config.num_attention_heads // self.shard_config.tensor_parallel_size,
                "self_attention.projection_size":
                    self.model.config.kv_channels * self.model.config.num_attention_heads //
                    self.shard_config.tensor_parallel_size,
                "self_attention.core_attention.num_attention_heads_per_partition":
                    self.model.config.num_attention_heads // self.shard_config.tensor_parallel_size,
                "self_attention.core_attention.hidden_size_per_partition":
                    self.model.config.kv_channels * self.model.config.num_attention_heads //
                    self.shard_config.tensor_parallel_size,
            },
                                                       param_replacement=[],
                                                       sub_module_replacement=[
                                                           SubModuleReplacementDescription(
                                                               suffix="self_attention.query_key_value",
                                                               target_module=col_nn.Linear1D_Col,
                                                           ),
                                                           SubModuleReplacementDescription(
                                                               suffix="self_attention.dense",
                                                               target_module=col_nn.Linear1D_Row,
                                                           ),
                                                           SubModuleReplacementDescription(
                                                               suffix="self_attention.core_attention.attention_dropout",
                                                               target_module=col_nn.DropoutForParallelInput,
                                                           ),
                                                           SubModuleReplacementDescription(
                                                               suffix="mlp.dense_h_to_4h",
                                                               target_module=col_nn.Linear1D_Col,
                                                           ),
                                                           SubModuleReplacementDescription(
                                                               suffix="mlp.dense_4h_to_h",
                                                               target_module=col_nn.Linear1D_Row,
                                                           )
                                                       ])

        return policy

    def postprocess(self):
        return self.model
