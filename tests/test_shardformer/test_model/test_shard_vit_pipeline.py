import pytest
import torch

import colossalai
from colossalai.cluster import ProcessGroupMesh
from colossalai.logging import disable_existing_loggers
from colossalai.pipeline.stage_manager import PipelineStageManager
from colossalai.testing import clear_cache_before_run, parameterize, rerun_if_address_is_in_use, spawn
from tests.kit.model_zoo import model_zoo
from tests.test_shardformer.test_model._utils import build_pipeline_model


def check_forward_backward(org_model, sharded_model, data_gen_fn, output_transform_fn, loss_fn):
    # TODO: add tests for forward/backward later
    pass


@parameterize('enable_tensor_parallelism', [False])
@parameterize('enable_fused_normalization', [False])
@parameterize('use_lazy_init', [False])
#TODO: merge this into test_shard_vit
def run_vit_test(enable_fused_normalization, enable_tensor_parallelism, use_lazy_init):
    DP_DIM, PP_DIM = 0, 1
    DP_SIZE, PP_SIZE = 2, 2
    pg_mesh = ProcessGroupMesh(DP_SIZE, PP_SIZE)
    stage_manager = PipelineStageManager(pg_mesh, PP_DIM)

    sub_model_zoo = model_zoo.get_sub_registry('transformers_vit')

    for name, (model_fn, data_gen_fn, _, _, _) in sub_model_zoo.items():

        inputs = data_gen_fn()
        inputs = {k: v.cuda() for k, v in inputs.items()}
        pixel_values = inputs['pixel_values']
        batch_size = len(pixel_values)
        hidden_size = 768
        hidden_state_shape = (batch_size, 197, hidden_size)

        if not stage_manager.is_first_stage():
            # change inputs if not the first stage
            hidden_states = torch.randn(*hidden_state_shape).cuda()
            # inputs['pixel_values'] = None
            inputs['hidden_states'] = hidden_states

        _, sharded_model = build_pipeline_model(model_fn, stage_manager, enable_fused_normalization,
                                                enable_tensor_parallelism, use_lazy_init)
        sharded_model.train()

        output = sharded_model(**inputs)
        if stage_manager.is_last_stage():
            if name != 'transformers_vit':
                assert output.loss is not None
        else:
            assert output.shape == hidden_state_shape, f'hidden_states shape is not correct, output:{output["hidden_states"].shape} is not equal to hidden_state:{hidden_state_shape}'

    torch.cuda.empty_cache()


def check_vit(rank, world_size, port):
    disable_existing_loggers()
    colossalai.launch(config={}, rank=rank, world_size=world_size, host='localhost', port=port, backend='nccl')
    run_vit_test()


@pytest.mark.dist
@rerun_if_address_is_in_use()
@clear_cache_before_run()
def test_vit():
    spawn(check_vit, 4)


if __name__ == "__main__":
    test_vit()
