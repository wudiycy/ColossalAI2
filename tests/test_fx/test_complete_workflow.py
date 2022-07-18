import colossalai
import torch
import torch.nn as nn
import pytest
import torch.multiprocessing as mp
import torch.distributed as dist
from colossalai.testing import rerun_if_address_is_in_use
from functools import partial
from colossalai.fx import ColoTracer
from colossalai.utils.model.lazy_init_context import LazyInitContext
from colossalai.fx.passes.shard_1d_pass import transformer_mlp_pass
from colossalai.utils import free_port
from colossalai.tensor import ProcessGroup


class MLP(torch.nn.Module):

    def __init__(self, dim: int):
        super().__init__()
        self.linear1 = torch.nn.Linear(dim, dim)
        self.linear2 = torch.nn.Linear(dim, dim)

    def forward(self, x):
        x = self.linear1(x)
        x = self.linear2(x)
        return x


def run_workflow(world_size):
    # initailization
    with LazyInitContext() as ctx:
        model = MLP(16)

    # tracing
    tracer = ColoTracer()
    graph = tracer.trace(model)
    gm = torch.fx.GraphModule(model, graph, model.__class__.__name__)

    # annotate
    annotated_gm = transformer_mlp_pass(gm, process_group=ProcessGroup())
    annotated_gm.recompile()

    # materialization and sharding
    ctx.lazy_init_parameters(annotated_gm)

    # # check sharding
    assert list(model.linear1.weight.shape) == [16 // world_size, 16]
    assert list(model.linear1.bias.shape) == [16 // world_size]
    assert list(model.linear2.weight.shape) == [16, 16 // world_size]

    # test forward to make sure that IR transform will produce the same results
    # like how ColoTensor would do it normally
    data = torch.rand(4, 16)
    non_fx_out = model(data)
    fx_out = annotated_gm(data)
    assert torch.equal(non_fx_out, fx_out)


def run_dist(rank, world_size, port):
    colossalai.launch(config={}, rank=rank, world_size=world_size, host='localhost', port=port, backend='nccl')
    run_workflow(world_size)


@pytest.mark.dist
@pytest.mark.parametrize('world_size', [1, 2])
@rerun_if_address_is_in_use()
def test_complete_workflow(world_size):
    run_func = partial(run_dist, world_size=world_size, port=free_port())
    mp.spawn(run_func, nprocs=world_size)


if __name__ == '__main__':
    test_complete_workflow(2)
