from typing import Optional, Union

import torch
import torch.distributed as dist
import torch.nn as nn
from transformers.generation import GenerationConfig
from transformers.utils import logging

from colossalai.cluster import ProcessGroupMesh
from colossalai.pipeline.schedule.generate import GenerateSchedule
from colossalai.pipeline.stage_manager import PipelineStageManager
from colossalai.shardformer import ShardConfig, ShardFormer
from colossalai.shardformer.policies.base_policy import Policy

from ..kv_cache import BatchInferState, MemoryManager
from .microbatch_manager import MicroBatchManager
from .policies import model_policy_map

PP_AXIS, TP_AXIS = 0, 1

_supported_models = [
    "LlamaForCausalLM",
    "BloomForCausalLM",
    "LlamaGPTQForCausalLM",
    "SmoothLlamaForCausalLM",
    "ChatGLMForConditionalGeneration",
]


class InferenceEngine:
    """
    InferenceEngine is a class that handles the pipeline parallel inference.

    Args:
        model (`nn.Module`): the model not in pipeline style, and will be modified with `ShardFormer`.
        tp_size (int): the size of tensor parallelism.
        pp_size (int): the size of pipeline parallelism.
        dtype (str): the data type of the model, should be one of 'fp16', 'fp32', 'bf16'.
        model_policy (`colossalai.shardformer.policies.base_policy.Policy`): the policy to shardformer model. It will be determined by the model type if not provided.
        micro_batch_size (int): the micro batch size. Only useful when `pp_size` > 1.
        micro_batch_buffer_size (int): the buffer size for micro batch. Normally, it should be the same as the number of pipeline stages.
        max_batch_size (int): the maximum batch size.
        max_input_len (int): the maximum input length.
        max_output_len (int): the maximum output length.
        quant (str): the quantization method, should be one of 'smoothquant', 'gptq', None.
        verbose (bool): whether to return the time cost of each step.

    """

    def __init__(
        self,
        model: nn.Module,
        tp_size: int = 1,
        pp_size: int = 1,
        dtype: str = "fp16",
        model_policy: Policy = None,
        micro_batch_size: int = 1,
        micro_batch_buffer_size: int = None,
        max_batch_size: int = 4,
        max_input_len: int = 32,
        max_output_len: int = 32,
        quant: str = None,
        verbose: bool = False,
        # TODO: implement early_stopping, and various gerneration options
        early_stopping: bool = False,
        do_sample: bool = False,
        num_beams: int = 1,
    ) -> None:
        # sanity check
        assert model.__class__.__name__ in _supported_models, f"Model {model.__class__.__name__} is not supported."
        assert (
            tp_size * pp_size == dist.get_world_size()
        ), f"TP size({tp_size}) * PP size({pp_size}) should be equal to the global world size ({dist.get_world_size()})"
        assert dtype in ["fp16", "fp32", "bf16"], "dtype should be one of 'fp16', 'fp32', 'bf16'"
        assert quant in ["smoothquant", "gptq", None], "quant should be one of 'smoothquant', 'gptq'"

        if quant == "gptq":
            from ..quant.gptq import GPTQManager

            self.gptq_manager = GPTQManager(model.quantize_config, max_input_len=max_input_len)
            model = model.model
        elif quant == "smoothquant":
            model = model.model

        self.pp_size = pp_size
        self.tp_size = tp_size
        self.quant = quant
        self.max_input_len = max_input_len
        self.max_batch_size = max_batch_size
        self.max_output_len = max_output_len

        logger = logging.get_logger(__name__)
        if quant == "smoothquant" and dtype != "fp32":
            dtype = "fp32"
            logger.warning_once("Warning: smoothquant only support fp32 and int8 mix precision. set dtype to fp32")

        if dtype == "fp16":
            self.dtype = torch.float16
            model.half()
        elif dtype == "bf16":
            self.dtype = torch.bfloat16
            model.to(torch.bfloat16)
        else:
            self.dtype = torch.float32

        if model_policy is None:
            model_policy = model_policy_map[model.config.model_type]()

        self.cache_manager_list = [
            self._init_manager(model, max_batch_size, max_input_len, max_output_len)
            for _ in range(micro_batch_buffer_size or pp_size)
        ]

        # Init pg mesh
        self.pg_mesh = ProcessGroupMesh(pp_size, tp_size)
        stage_manager = None
        if pp_size > 1:
            stage_manager = PipelineStageManager(self.pg_mesh, PP_AXIS, True)
            mb_manager = MicroBatchManager(
                stage_manager.stage,
                micro_batch_size,
                micro_batch_buffer_size or pp_size,
                max_input_len,
                max_output_len,
                self.cache_manager_list,
            )
            self.schedule = GenerateSchedule(stage_manager, mb_manager, verbose)

        self.tp_group = self.pg_mesh.get_group_along_axis(TP_AXIS) if tp_size > 1 else None

        self.model = self._shardformer(model, model_policy, stage_manager, self.tp_group)
        if quant == "gptq":
            self.gptq_manager.post_init_gptq_buffer(self.model)
        self.verbose = verbose

    def generate(self, input_list: Union[list, dict], generation_config: Optional[GenerationConfig] = None):
        """
        Args:
            input_list (list): a list of input data, each element is a `BatchEncoding` or `dict`.

        Returns:
            out (list): a list of output data, each element is a list of token.
            timestamp (float): the time cost of the inference, only return when verbose is `True`.
        """

        if self.pp_size > 1:
            out, timestamp = self.schedule.generate_step(self.model, iter([input_list]))
            if self.verbose:
                return out, timestamp
            else:
                return out
        else:
            # when pipeline parallelism is not used, we can directly use the model to generate
            # now the size if cache manager list is 1
            batch_infer_state = BatchInferState.init_from_batch(
                input_list, self.max_input_len, self.max_output_len, self.cache_manager_list[0]
            )
            # bind the infer state to the model (not lm model)
            self.model.model.infer_state = batch_infer_state
            if generation_config is not None:
                generation_config.max_new_tokens = self.max_output_len
            else:
                generation_config = GenerationConfig(
                    max_new_tokens=self.max_output_len, pad_token_id=self.model.config.pad_token_id
                )
            out = self.model.generate(**input_list, generation_config=generation_config)
            # free the cache
            self.cache_manager_list[0].free_all()
            return out

    def _shardformer(
        self,
        model: nn.Module,
        model_policy: Policy,
        stage_manager: Optional[PipelineStageManager],
        tp_group: Optional[dist.ProcessGroup],
    ) -> nn.Module:
        shardconfig = ShardConfig(
            tensor_parallel_process_group=tp_group,
            pipeline_stage_manager=stage_manager,
            enable_tensor_parallelism=(self.tp_size > 1),
            enable_fused_normalization=False,
            enable_all_optimization=False,
            enable_flash_attention=False,
            enable_jit_fused=False,
            enable_sequence_parallelism=False,
            extra_kwargs={"quant": self.quant},
        )
        shardformer = ShardFormer(shard_config=shardconfig)
        shard_model, _ = shardformer.optimize(model, model_policy)
        return shard_model.cuda()

    def _init_manager(self, model, max_batch_size: int, max_input_len: int, max_output_len: int) -> MemoryManager:
        max_total_token_num = max_batch_size * (max_input_len + max_output_len)
        if model.config.model_type == "llama":
            head_dim = model.config.hidden_size // model.config.num_attention_heads
            head_num = model.config.num_key_value_heads // self.tp_size
            num_hidden_layers = (
                model.config.num_hidden_layers
                if hasattr(model.config, "num_hidden_layers")
                else model.config.num_layers
            )
            layer_num = num_hidden_layers // self.pp_size
        elif model.config.model_type == "bloom":
            head_dim = model.config.hidden_size // model.config.n_head
            head_num = model.config.n_head // self.tp_size
            num_hidden_layers = model.config.n_layer
            layer_num = num_hidden_layers // self.pp_size
        elif model.config.model_type == "chatglm":
            head_dim = model.config.hidden_size // model.config.num_attention_heads
            if model.config.multi_query_attention:
                head_num = model.config.multi_query_group_num // self.tp_size
            else:
                head_num = model.config.num_attention_heads // self.tp_size
            num_hidden_layers = model.config.num_layers
            layer_num = num_hidden_layers // self.pp_size
        else:
            raise NotImplementedError("Only support llama, bloom and chatglm model.")

        dtype = torch.int8 if self.quant == "smoothquant" else self.dtype
        return MemoryManager(max_total_token_num, dtype, head_num, head_dim, layer_num)
