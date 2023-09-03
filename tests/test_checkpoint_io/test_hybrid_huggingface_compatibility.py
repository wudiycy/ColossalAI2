import pytest
import torch
import torch.distributed as dist
from torch.optim import Adam
from utils import shared_tempdir

import colossalai
from colossalai.booster import Booster
from colossalai.booster.plugin import HybridParallelPlugin
from colossalai.shardformer.layer.utils import Randomizer
from colossalai.tensor.d_tensor.api import clear_layout_converter
from colossalai.testing import (
    check_state_dict_equal,
    clear_cache_before_run,
    parameterize,
    rerun_if_address_is_in_use,
    spawn,
)
from tests.kit.model_zoo import model_zoo


def exam_from_pretrained(model_fn,
                         data_gen_fn,
                         output_transform_fn,
                         loss_fn,
                         test_config,
                         shard=True,
                         size_per_shard=32):

    def _criterion(outputs, inputs):
        outputs = output_transform_fn(outputs)
        loss = criterion(outputs)
        return loss

    def _preprocess_data(data):
        if booster.plugin.stage_manager is not None:
            for k, v in data.items():
                if torch.is_tensor(v) or 'Tensor' in v.__class__.__name__:
                    new_shape = [1] * v.dim()
                    new_shape[0] = 4
                    data[k] = v.to('cuda').repeat(*new_shape)
            return iter([data])
        else:
            return {k: v.cuda() for k, v in data.items()}

    model = model_fn()
    optimizer = Adam((model.parameters()), lr=0.001)
    criterion = loss_fn
    plugin = HybridParallelPlugin(**test_config)
    booster = Booster(plugin=plugin)

    model, optimizer, criterion, _, _ = booster.boost(model, optimizer, criterion)

    data = data_gen_fn()
    model.train()
    if booster.plugin.stage_manager is not None:
        booster.execute_pipeline(_preprocess_data(data),
                                 model,
                                 _criterion,
                                 optimizer,
                                 return_loss=True,
                                 return_outputs=False)
    else:
        output = model(**_preprocess_data(data))
        loss = criterion(output)
        optimizer.backward(loss)

    optimizer.step()

    with shared_tempdir() as tempdir:

        model_ckpt_path = f"{tempdir}/model"
        booster.save_model(model, model_ckpt_path, shard=shard, size_per_shard=size_per_shard)
        dist.barrier()

        new_model = model.unwrap().__class__.from_pretrained(model_ckpt_path)
        new_optimizer = Adam(new_model.parameters(), lr=1e-3)
        new_model, new_optimizer, criterion, _, _ = booster.boost(new_model, new_optimizer, criterion)

        check_state_dict_equal(model.unwrap().state_dict(), new_model.unwrap().state_dict(), False)

    Randomizer.reset_index()
    torch.cuda.empty_cache()


@clear_cache_before_run()
@parameterize('test_config', [{
    'tp_size': 4,
    'pp_size': 1,
    'precision': 'fp32',
}, {
    'tp_size': 2,
    'pp_size': 2,
    'num_microbatches': 4,
    'precision': 'fp16',
    'initial_scale': 1
}, {
    'tp_size': 2,
    'pp_size': 1,
    'zero_stage': 2,
    'precision': 'fp16',
    'initial_scale': 1
}, {
    'tp_size': 1,
    'pp_size': 2,
    'num_microbatches': 4,
    'zero_stage': 1,
    'precision': 'fp16',
    'initial_scale': 1
}])
def run_test(test_config):
    sub_model_zoo = model_zoo.get_sub_registry('transformers_gpt')
    for name, (model_fn, data_gen_fn, output_transform_fn, loss_fn, _) in sub_model_zoo.items():
        exam_from_pretrained(model_fn, data_gen_fn, output_transform_fn, loss_fn, test_config)
    clear_layout_converter()
    torch.cuda.empty_cache()


def run_dist(rank, world_size, port):
    config = {}
    colossalai.launch(config=config, rank=rank, world_size=world_size, host='localhost', port=port, backend='nccl')
    run_test()


@pytest.mark.dist
@pytest.mark.parametrize('world_size', [4])
@rerun_if_address_is_in_use()
def test_huggingface_compatibility(world_size):
    spawn(run_dist, world_size)
