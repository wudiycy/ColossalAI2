from functools import partial
from typing import Callable, Dict, List, Union

import torch.nn as nn
from torch import Tensor

from colossalai.shardformer.layer import DropoutForReplicatedInput, DropoutForParallelInput, FusedLayerNorm, Linear1D_Col, Linear1D_Row

from ..modeling.vit import forward_fn
from .base_policy import ModulePolicyDescription, Policy, SubModuleReplacementDescription

__all__ = ['ViTPolicy', 'ViTModelPolicy', 'ViTForImageClassificationPolicy', 'ViTForMaskedImageModelingPolicy']


class ViTPolicy(Policy):

    def config_sanity_check(self):
        pass

    def preprocess(self):
        return self.model

    def module_policy(self) -> Dict[Union[str, nn.Module], ModulePolicyDescription]:
        from transformers.models.vit.modeling_vit import ViTEmbeddings, ViTLayer

        policy = {}

        if self.shard_config.enable_tensor_parallelism:
            policy[ViTEmbeddings] = ModulePolicyDescription(attribute_replacement={},
                                        param_replacement=[],
                                        sub_module_replacement=[
                                            SubModuleReplacementDescription(
                                                suffix="dropout",
                                                target_module=DropoutForReplicatedInput,
                                            )
                                        ])
            
            policy[ViTLayer] = ModulePolicyDescription(
                    attribute_replacement={
                        "attention.attention.num_attention_heads":
                            self.model.config.num_attention_heads//self.shard_config.tensor_parallel_size,
                        "attention.attention.all_head_size":
                            self.model.config.hidden_size//self.shard_config.tensor_parallel_size,
                    },
                    param_replacement=[],
                    sub_module_replacement=[
                        SubModuleReplacementDescription(
                            suffix="attention.attention.query",
                            target_module=Linear1D_Col,
                        ),
                        SubModuleReplacementDescription(
                            suffix="attention.attention.key",
                            target_module=Linear1D_Col,
                        ),
                        SubModuleReplacementDescription(
                            suffix="attention.attention.value",
                            target_module=Linear1D_Col,
                        ),
                        SubModuleReplacementDescription(
                            suffix="attention.attention.dropout",
                            target_module=DropoutForParallelInput,
                        ),
                        SubModuleReplacementDescription(
                            suffix="attention.output.dense",
                            target_module=Linear1D_Row,
                        ),
                        SubModuleReplacementDescription(
                            suffix="attention.output.dropout",
                            target_module=DropoutForReplicatedInput,
                        ),
                        SubModuleReplacementDescription(
                            suffix="intermediate.dense",
                            target_module=Linear1D_Col,
                        ),
                        SubModuleReplacementDescription(
                            suffix="output.dense",
                            target_module=Linear1D_Row,
                        ),
                        SubModuleReplacementDescription(
                            suffix="output.dropout",
                            target_module=DropoutForReplicatedInput,
                        ),
                    ]
                )

        return policy
  
    
    def new_model_class(self):
        return None

    def postprocess(self):
        return self.model

    def get_held_layers(self) -> List[nn.Module]:
        """Get pipeline layers for current stage."""
        assert self.pipeline_stage_manager is not None, "pipeline_stage_manager is None"

        if self.model.__class__.__name__ == 'ViTModel':
            module = self.model
        else:
            module = self.model.vit
        stage_manager = self.pipeline_stage_manager

        held_layers = []
        layers_per_stage = self.distribute_layers(len(module.encoder.layer), stage_manager.num_stages)
        if stage_manager.is_first_stage():
            held_layers.append(module.embeddings)
        start_idx, end_idx = self.get_stage_index(layers_per_stage, stage_manager.stage)
        held_layers.extend(module.encoder.layer[start_idx:end_idx])
        if stage_manager.is_last_stage():
            held_layers.append(module.layernorm)
            held_layers.append(module.pooler)
        return held_layers

    def set_pipeline_forward(self, model_cls: nn.Module, pipeline_forward: Callable, policy: Dict):
        if self.pipeline_stage_manager:
            stage_manager = self.pipeline_stage_manager
            if self.model.__class__.__name__ == 'ViTModel':
                module = self.model
            else:
                module = self.model.vit

            layers_per_stage = Policy.distribute_layers(len(module.encoder.layer), stage_manager.num_stages)
            stage_index = Policy.get_stage_index(layers_per_stage, stage_manager.stage)
            method_replacement = {'forward': pipeline_forward(stage_manager=stage_manager, stage_index=stage_index)}
            self.append_or_create_method_replacement(description=method_replacement,
                                                     policy=policy,
                                                     target_key=model_cls)


# ViTModel
class ViTModelPolicy(ViTPolicy):

    def __init__(self) -> None:
        super().__init__()

    def module_policy(self):
        from transformers.models.vit.modeling_vit import ViTModel

        policy = super().module_policy()

        if self.shard_config.pipeline_stage_manager is not None:
            self.set_pipeline_forward(model_cls=ViTModel, pipeline_forward=forward_fn, policy=policy)
        return policy

    def get_held_layers(self) -> List[nn.Module]:
        return super().get_held_layers()

    def get_shared_params(self) -> List[Dict[int, Tensor]]:
        return super().get_shared_params()


# ViTForImageClassification
class ViTForImageClassificationPolicy(ViTPolicy):

     def module_policy(self):
        from transformers.models.vit.modeling_vit import ViTForImageClassification

        policy = super().module_policy()
        if self.shard_config.enable_tensor_parallelism:
            new_item = {
                ViTForImageClassification:
                ModulePolicyDescription(sub_module_replacement=[
                                        SubModuleReplacementDescription(suffix="classifier",
                                                                            target_module=Linear1D_Col,
                                                                            kwargs=dict(gather_output=True))
                                        ])
            }
            policy.update(new_item)
        return policy


# ViTForMaskedImageModeling
class ViTForMaskedImageModelingPolicy(ViTPolicy):

    def __init__(self) -> None:
        super().__init__()
