import torch
import torch.distributed as dist
import torch.nn as nn
from torch.testing import assert_close

import colossalai
from colossalai.shardformer.layer import LayerNorm1D
from colossalai.testing import rerun_if_address_is_in_use, spawn


def check_layernorm_1d():
    norm = nn.LayerNorm(128, 0.00001).cuda()
    norm1d = LayerNorm1D.from_native_module(norm, process_group=None)

    assert norm1d.weight.shape == torch.Size([128])

    # check computation correctness
    x = torch.rand(4, 128).cuda()
    out = norm(x)
    gather_out = norm1d(x)
    assert_close(out, gather_out)

    # check backward correctness
    out.sum().backward()
    gather_out.sum().backward()

    assert_close(norm.weight.grad, norm1d.weight.grad)


def run_dist(rank, world_size, port):
    colossalai.launch(config={}, rank=rank, world_size=world_size, host='localhost', port=port, backend='nccl')
    check_layernorm_1d()


@rerun_if_address_is_in_use()
def test_layernorm_1d():
    spawn(run_dist, nprocs=2)


if __name__ == '__main__':
    test_layernorm_1d()
