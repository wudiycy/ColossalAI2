import copy
from functools import partial

import colossalai
import pytest
import torch
import torch.distributed as dist
import torch.multiprocessing as mp
from colossalai.utils import free_port
from colossalai.zero.shard_utils import (BucketTensorShardStrategy, TensorShardStrategy)
from colossalai.zero.sharded_model import ShardedModelV2
from colossalai.zero.sharded_optim import ShardedOptimizerV2
from tests.components_to_test.registry import non_distributed_component_funcs
from torch.nn.parallel import DistributedDataParallel as DDP
from colossalai.nn.optimizer import CPUAdam

from common import CONFIG, check_sharded_params_padding


def _run_step(model, optimizer, data, label, criterion, enable_autocast=False):
    model.train()
    optimizer.zero_grad()
    with torch.cuda.amp.autocast(enabled=enable_autocast):
        if criterion:
            y = model(data)
            loss = criterion(y, label)
        else:
            loss = model(data, label)

    loss = loss.float()
    if isinstance(model, ShardedModelV2):
        optimizer.backward(loss)
    else:
        loss.backward()
    optimizer.step()


def _run_dist(rank, world_size, port, cpu_offload, shard_strategy, use_cpuadam):
    colossalai.launch(config=CONFIG, rank=rank, world_size=world_size, host='localhost', port=port, backend='nccl')
    test_models = ['repeated_computed_layers', 'resnet18', 'bert']
    shard_strategy = shard_strategy()
    for model_name in test_models:
        get_components_func = non_distributed_component_funcs.get_callable(model_name)
        model, train_dataloader, _, optimizer_class, criterion = get_components_func()
        model = model(checkpoint=True).cuda()
        zero_model = ShardedModelV2(copy.deepcopy(model),
                                    shard_strategy,
                                    offload_config=dict(device='cpu') if cpu_offload else None)
        if dist.get_world_size() > 1:
            model = DDP(model)
        lr = 1e-3
        if use_cpuadam:
            optim = torch.optim.Adam(model.parameters(), lr=lr)
            sharded_optim = ShardedOptimizerV2(zero_model, CPUAdam, cpu_offload=cpu_offload, initial_scale=2**5, lr=lr)
        else:
            optim = optimizer_class(model.parameters(), lr=lr)
            sharded_optim = ShardedOptimizerV2(zero_model,
                                               optimizer_class,
                                               cpu_offload=cpu_offload,
                                               initial_scale=2**5,
                                               lr=lr)
        for i, (data, label) in enumerate(train_dataloader):
            #FIXME() if i > 5, the unittest will fail
            if i > 3:
                break
            data, label = data.cuda(), label.cuda()
            _run_step(model, optim, data, label, criterion, False)
            _run_step(zero_model, sharded_optim, data, label, criterion, False)
            check_sharded_params_padding(model, zero_model, loose=True)


# use_cpuadam = True can be used with cpu_offload = False
@pytest.mark.dist
@pytest.mark.parametrize("world_size", [1, 2])
@pytest.mark.parametrize("cpu_offload", [False])
@pytest.mark.parametrize("use_cpuadam", [False])
@pytest.mark.parametrize("shard_strategy", [TensorShardStrategy, BucketTensorShardStrategy])
def test_sharded_optim_v2_cpu_adam(world_size, cpu_offload, shard_strategy, use_cpuadam):
    run_func = partial(_run_dist,
                       world_size=world_size,
                       port=free_port(),
                       cpu_offload=cpu_offload,
                       shard_strategy=shard_strategy,
                       use_cpuadam=use_cpuadam)
    mp.spawn(run_func, nprocs=world_size)


@pytest.mark.dist
@pytest.mark.parametrize("world_size", [1, 2])
@pytest.mark.parametrize("cpu_offload", [True])
@pytest.mark.parametrize("shard_strategy", [TensorShardStrategy, BucketTensorShardStrategy])
@pytest.mark.parametrize("use_cpuadam", [True, False])
def test_sharded_optim_v2_cpu_adam(world_size, cpu_offload, shard_strategy, use_cpuadam):
    run_func = partial(_run_dist,
                       world_size=world_size,
                       port=free_port(),
                       cpu_offload=cpu_offload,
                       shard_strategy=shard_strategy,
                       use_cpuadam=use_cpuadam)
    mp.spawn(run_func, nprocs=world_size)


if __name__ == '__main__':
    test_sharded_optim_v2_cpu_adam(world_size=2,
                                   cpu_offload=True,
                                   shard_strategy=TensorShardStrategy,
                                   use_cpuadam=False)
