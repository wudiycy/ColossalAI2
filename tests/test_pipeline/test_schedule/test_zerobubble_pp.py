from copy import deepcopy
from typing import Tuple

import pytest
import torch
import torch.distributed as dist
import torch.nn as nn
from torch.testing import assert_close

import colossalai
from colossalai.cluster import ProcessGroupMesh
from colossalai.pipeline.schedule.v_schedule import ScheduledNode
from colossalai.pipeline.schedule.zero_bubble_pp import ZeroBubbleVPipeScheduler
from colossalai.pipeline.stage_manager import PipelineStageManager
from colossalai.testing import rerun_if_address_is_in_use, spawn


class MlpModel(nn.Module):
    def __init__(self, in_dim, out_dim, num_layers):
        super().__init__()
        self.layers = nn.ModuleList([nn.Linear(in_dim, out_dim, bias=None) for _ in range(num_layers)])

    def forward(self, x):
        for layer in self.layers:
            x = layer(x)
        return x


def get_model_numel(model: torch.nn.Module) -> Tuple[int, int]:
    num_params = 0
    num_params_trainable = 0
    for p in model.parameters():
        num_params += p.numel()
        if p.requires_grad:
            num_params_trainable += p.numel()
    return num_params, num_params_trainable


# Test iter input & multiple microbatch
def test_run_fwd_bwd_iter_input(
    rank: int,
    world_size: int,
    port: int,
):
    # init dist
    colossalai.launch(rank=rank, world_size=world_size, port=port, host="localhost")
    rank = dist.get_rank()
    pp_size = world_size
    pg_mesh = ProcessGroupMesh(pp_size)
    num_microbatch = 4
    # stage_manager
    stage_manager = PipelineStageManager(pg_mesh, pipeline_axis=0, enable_interleave=True, num_model_chunks=pp_size)

    # schedule list
    zbv_schedule = [
        # stage 0
        [
            # microbatch 0
            # chunk 0 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=0, stage=0, minibatch=0),
            ScheduledNode(type="F", chunk=0, stage=0, minibatch=0),
            ScheduledNode(type="SEND_FORWARD", chunk=0, stage=0, minibatch=0),
            # chunk 1 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=1, stage=0, minibatch=0),
            ScheduledNode(type="F", chunk=1, stage=0, minibatch=0),
            ScheduledNode(type="SEND_FORWARD", chunk=1, stage=0, minibatch=0),
            # chunk 1 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=1, stage=0, minibatch=0),
            ScheduledNode(type="B", chunk=1, stage=0, minibatch=0),
            ScheduledNode(type="W", chunk=1, stage=0, minibatch=0),
            ScheduledNode(type="SEND_BACKWARD", chunk=1, stage=0, minibatch=0),
            # chunk 0 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=0, stage=0, minibatch=0),
            ScheduledNode(type="B", chunk=0, stage=0, minibatch=0),
            ScheduledNode(type="W", chunk=0, stage=0, minibatch=0),
            ScheduledNode(type="SEND_BACKWARD", chunk=0, stage=0, minibatch=0),
            # microbatch 1
            # chunk 0 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=0, stage=0, minibatch=1),
            ScheduledNode(type="F", chunk=0, stage=0, minibatch=1),
            ScheduledNode(type="SEND_FORWARD", chunk=0, stage=0, minibatch=1),
            # chunk 1 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=1, stage=0, minibatch=1),
            ScheduledNode(type="F", chunk=1, stage=0, minibatch=1),
            ScheduledNode(type="SEND_FORWARD", chunk=1, stage=0, minibatch=1),
            # chunk 1 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=1, stage=0, minibatch=1),
            ScheduledNode(type="B", chunk=1, stage=0, minibatch=1),
            ScheduledNode(type="W", chunk=1, stage=0, minibatch=1),
            ScheduledNode(type="SEND_BACKWARD", chunk=1, stage=0, minibatch=1),
            # chunk 0 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=0, stage=0, minibatch=1),
            ScheduledNode(type="B", chunk=0, stage=0, minibatch=1),
            ScheduledNode(type="W", chunk=0, stage=0, minibatch=1),
            ScheduledNode(type="SEND_BACKWARD", chunk=0, stage=0, minibatch=1),
            # microbatch 2
            # chunk 0 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=0, stage=0, minibatch=2),
            ScheduledNode(type="F", chunk=0, stage=0, minibatch=2),
            ScheduledNode(type="SEND_FORWARD", chunk=0, stage=0, minibatch=2),
            # chunk 1 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=1, stage=0, minibatch=2),
            ScheduledNode(type="F", chunk=1, stage=0, minibatch=2),
            ScheduledNode(type="SEND_FORWARD", chunk=1, stage=0, minibatch=2),
            # chunk 1 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=1, stage=0, minibatch=2),
            ScheduledNode(type="B", chunk=1, stage=0, minibatch=2),
            ScheduledNode(type="W", chunk=1, stage=0, minibatch=2),
            ScheduledNode(type="SEND_BACKWARD", chunk=1, stage=0, minibatch=2),
            # chunk 0 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=0, stage=0, minibatch=2),
            ScheduledNode(type="B", chunk=0, stage=0, minibatch=2),
            ScheduledNode(type="W", chunk=0, stage=0, minibatch=2),
            ScheduledNode(type="SEND_BACKWARD", chunk=0, stage=0, minibatch=2),
            # microbatch 3
            # chunk 0 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=0, stage=0, minibatch=3),
            ScheduledNode(type="F", chunk=0, stage=0, minibatch=3),
            ScheduledNode(type="SEND_FORWARD", chunk=0, stage=0, minibatch=3),
            # chunk 1 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=1, stage=0, minibatch=3),
            ScheduledNode(type="F", chunk=1, stage=0, minibatch=3),
            ScheduledNode(type="SEND_FORWARD", chunk=1, stage=0, minibatch=3),
            # chunk 1 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=1, stage=0, minibatch=3),
            ScheduledNode(type="B", chunk=1, stage=0, minibatch=3),
            ScheduledNode(type="W", chunk=1, stage=0, minibatch=3),
            ScheduledNode(type="SEND_BACKWARD", chunk=1, stage=0, minibatch=3),
            # chunk 0 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=0, stage=0, minibatch=3),
            ScheduledNode(type="B", chunk=0, stage=0, minibatch=3),
            ScheduledNode(type="W", chunk=0, stage=0, minibatch=3),
            ScheduledNode(type="SEND_BACKWARD", chunk=0, stage=0, minibatch=3),
        ],
        # stage 1
        [
            # microbatch 0
            # chunk 0 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=0, stage=1, minibatch=0),
            ScheduledNode(type="F", chunk=0, stage=1, minibatch=0),
            ScheduledNode(type="SEND_FORWARD", chunk=0, stage=1, minibatch=0),
            # chunk 1 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=1, stage=1, minibatch=0),
            ScheduledNode(type="F", chunk=1, stage=1, minibatch=0),
            ScheduledNode(type="SEND_FORWARD", chunk=1, stage=1, minibatch=0),
            # chunk 1 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=1, stage=1, minibatch=0),
            ScheduledNode(type="B", chunk=1, stage=1, minibatch=0),
            ScheduledNode(type="W", chunk=1, stage=1, minibatch=0),
            ScheduledNode(type="SEND_BACKWARD", chunk=1, stage=1, minibatch=0),
            # chunk 0 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=0, stage=1, minibatch=0),
            ScheduledNode(type="B", chunk=0, stage=1, minibatch=0),
            ScheduledNode(type="W", chunk=0, stage=1, minibatch=0),
            ScheduledNode(type="SEND_BACKWARD", chunk=0, stage=0, minibatch=0),
            # microbatch 1
            # chunk 0 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=0, stage=1, minibatch=1),
            ScheduledNode(type="F", chunk=0, stage=1, minibatch=1),
            ScheduledNode(type="SEND_FORWARD", chunk=0, stage=1, minibatch=1),
            # chunk 1 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=1, stage=1, minibatch=1),
            ScheduledNode(type="F", chunk=1, stage=1, minibatch=1),
            ScheduledNode(type="SEND_FORWARD", chunk=1, stage=1, minibatch=1),
            # chunk 1 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=1, stage=1, minibatch=1),
            ScheduledNode(type="B", chunk=1, stage=1, minibatch=1),
            ScheduledNode(type="W", chunk=1, stage=1, minibatch=1),
            ScheduledNode(type="SEND_BACKWARD", chunk=1, stage=1, minibatch=1),
            # chunk 0 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=0, stage=1, minibatch=1),
            ScheduledNode(type="B", chunk=0, stage=1, minibatch=1),
            ScheduledNode(type="W", chunk=0, stage=1, minibatch=1),
            ScheduledNode(type="SEND_BACKWARD", chunk=0, stage=0, minibatch=1),
            # microbatch 2
            # chunk 0 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=0, stage=1, minibatch=2),
            ScheduledNode(type="F", chunk=0, stage=1, minibatch=2),
            ScheduledNode(type="SEND_FORWARD", chunk=0, stage=1, minibatch=2),
            # chunk 1 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=1, stage=1, minibatch=2),
            ScheduledNode(type="F", chunk=1, stage=1, minibatch=2),
            ScheduledNode(type="SEND_FORWARD", chunk=1, stage=1, minibatch=2),
            # chunk 1 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=1, stage=1, minibatch=2),
            ScheduledNode(type="B", chunk=1, stage=1, minibatch=2),
            ScheduledNode(type="W", chunk=1, stage=1, minibatch=2),
            ScheduledNode(type="SEND_BACKWARD", chunk=1, stage=1, minibatch=2),
            # chunk 0 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=0, stage=1, minibatch=2),
            ScheduledNode(type="B", chunk=0, stage=1, minibatch=2),
            ScheduledNode(type="W", chunk=0, stage=1, minibatch=2),
            ScheduledNode(type="SEND_BACKWARD", chunk=0, stage=0, minibatch=2),
            # microbatch 3
            # chunk 0 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=0, stage=1, minibatch=3),
            ScheduledNode(type="F", chunk=0, stage=1, minibatch=3),
            ScheduledNode(type="SEND_FORWARD", chunk=0, stage=1, minibatch=3),
            # chunk 1 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=1, stage=1, minibatch=3),
            ScheduledNode(type="F", chunk=1, stage=1, minibatch=3),
            ScheduledNode(type="SEND_FORWARD", chunk=1, stage=1, minibatch=3),
            # chunk 1 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=1, stage=1, minibatch=3),
            ScheduledNode(type="B", chunk=1, stage=1, minibatch=3),
            ScheduledNode(type="W", chunk=1, stage=1, minibatch=3),
            ScheduledNode(type="SEND_BACKWARD", chunk=1, stage=1, minibatch=3),
            # chunk 0 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=0, stage=1, minibatch=3),
            ScheduledNode(type="B", chunk=0, stage=1, minibatch=3),
            ScheduledNode(type="W", chunk=0, stage=1, minibatch=3),
            ScheduledNode(type="SEND_BACKWARD", chunk=0, stage=0, minibatch=3),
        ],
        # stage 2
        [
            # microbatch 0
            # chunk 0 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=0, stage=2, minibatch=0),
            ScheduledNode(type="F", chunk=0, stage=2, minibatch=0),
            ScheduledNode(type="SEND_FORWARD", chunk=0, stage=2, minibatch=0),
            # chunk 1 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=1, stage=2, minibatch=0),
            ScheduledNode(type="F", chunk=1, stage=2, minibatch=0),
            ScheduledNode(type="SEND_FORWARD", chunk=1, stage=2, minibatch=0),
            # chunk 1 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=1, stage=2, minibatch=0),
            ScheduledNode(type="B", chunk=1, stage=2, minibatch=0),
            ScheduledNode(type="W", chunk=1, stage=2, minibatch=0),
            ScheduledNode(type="SEND_BACKWARD", chunk=1, stage=2, minibatch=0),
            # chunk 0 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=0, stage=2, minibatch=0),
            ScheduledNode(type="B", chunk=0, stage=2, minibatch=0),
            ScheduledNode(type="W", chunk=0, stage=2, minibatch=0),
            ScheduledNode(type="SEND_BACKWARD", chunk=0, stage=2, minibatch=0),
            # microbatch 1
            # chunk 0 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=0, stage=2, minibatch=1),
            ScheduledNode(type="F", chunk=0, stage=2, minibatch=1),
            ScheduledNode(type="SEND_FORWARD", chunk=0, stage=2, minibatch=1),
            # chunk 1 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=1, stage=2, minibatch=1),
            ScheduledNode(type="F", chunk=1, stage=2, minibatch=1),
            ScheduledNode(type="SEND_FORWARD", chunk=1, stage=2, minibatch=1),
            # chunk 1 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=1, stage=2, minibatch=1),
            ScheduledNode(type="B", chunk=1, stage=2, minibatch=1),
            ScheduledNode(type="W", chunk=1, stage=2, minibatch=1),
            ScheduledNode(type="SEND_BACKWARD", chunk=1, stage=2, minibatch=1),
            # chunk 0 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=0, stage=2, minibatch=1),
            ScheduledNode(type="B", chunk=0, stage=2, minibatch=1),
            ScheduledNode(type="W", chunk=0, stage=2, minibatch=1),
            ScheduledNode(type="SEND_BACKWARD", chunk=0, stage=2, minibatch=1),
            # microbatch 2
            # chunk 0 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=0, stage=2, minibatch=2),
            ScheduledNode(type="F", chunk=0, stage=2, minibatch=2),
            ScheduledNode(type="SEND_FORWARD", chunk=0, stage=2, minibatch=2),
            # chunk 1 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=1, stage=2, minibatch=2),
            ScheduledNode(type="F", chunk=1, stage=2, minibatch=2),
            ScheduledNode(type="SEND_FORWARD", chunk=1, stage=2, minibatch=2),
            # chunk 1 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=1, stage=2, minibatch=2),
            ScheduledNode(type="B", chunk=1, stage=2, minibatch=2),
            ScheduledNode(type="W", chunk=1, stage=2, minibatch=2),
            ScheduledNode(type="SEND_BACKWARD", chunk=1, stage=2, minibatch=2),
            # chunk 0 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=0, stage=2, minibatch=2),
            ScheduledNode(type="B", chunk=0, stage=2, minibatch=2),
            ScheduledNode(type="W", chunk=0, stage=2, minibatch=2),
            ScheduledNode(type="SEND_BACKWARD", chunk=0, stage=2, minibatch=2),
            # microbatch 3
            # chunk 0 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=0, stage=2, minibatch=3),
            ScheduledNode(type="F", chunk=0, stage=2, minibatch=3),
            ScheduledNode(type="SEND_FORWARD", chunk=0, stage=2, minibatch=3),
            # chunk 1 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=1, stage=2, minibatch=3),
            ScheduledNode(type="F", chunk=1, stage=2, minibatch=3),
            ScheduledNode(type="SEND_FORWARD", chunk=1, stage=2, minibatch=3),
            # chunk 1 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=1, stage=2, minibatch=3),
            ScheduledNode(type="B", chunk=1, stage=2, minibatch=3),
            ScheduledNode(type="W", chunk=1, stage=2, minibatch=3),
            ScheduledNode(type="SEND_BACKWARD", chunk=1, stage=2, minibatch=3),
            # chunk 0 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=0, stage=2, minibatch=3),
            ScheduledNode(type="B", chunk=0, stage=2, minibatch=3),
            ScheduledNode(type="W", chunk=0, stage=2, minibatch=3),
            ScheduledNode(type="SEND_BACKWARD", chunk=0, stage=2, minibatch=3),
        ],
        # stage 3
        [
            # microbatch 0
            # chunk 0 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=0, stage=3, minibatch=0),
            ScheduledNode(type="F", chunk=0, stage=3, minibatch=0),
            ScheduledNode(type="SEND_FORWARD", chunk=0, stage=3, minibatch=0),
            # chunk 1 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=1, stage=3, minibatch=0),
            ScheduledNode(type="F", chunk=1, stage=3, minibatch=0),
            ScheduledNode(type="SEND_FORWARD", chunk=1, stage=3, minibatch=0),
            # chunk 1 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=1, stage=3, minibatch=0),
            ScheduledNode(type="B", chunk=1, stage=3, minibatch=0),
            ScheduledNode(type="W", chunk=1, stage=3, minibatch=0),
            ScheduledNode(type="SEND_BACKWARD", chunk=1, stage=3, minibatch=0),
            # chunk 0 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=0, stage=3, minibatch=0),
            ScheduledNode(type="B", chunk=0, stage=3, minibatch=0),
            ScheduledNode(type="W", chunk=0, stage=3, minibatch=0),
            ScheduledNode(type="SEND_BACKWARD", chunk=0, stage=3, minibatch=0),
            # microbatch 1
            # chunk 0 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=0, stage=3, minibatch=1),
            ScheduledNode(type="F", chunk=0, stage=3, minibatch=1),
            ScheduledNode(type="SEND_FORWARD", chunk=0, stage=3, minibatch=1),
            # chunk 1 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=1, stage=3, minibatch=1),
            ScheduledNode(type="F", chunk=1, stage=3, minibatch=1),
            ScheduledNode(type="SEND_FORWARD", chunk=1, stage=3, minibatch=1),
            # chunk 1 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=1, stage=3, minibatch=1),
            ScheduledNode(type="B", chunk=1, stage=3, minibatch=1),
            ScheduledNode(type="W", chunk=1, stage=3, minibatch=1),
            ScheduledNode(type="SEND_BACKWARD", chunk=1, stage=3, minibatch=1),
            # chunk 0 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=0, stage=3, minibatch=1),
            ScheduledNode(type="B", chunk=0, stage=3, minibatch=1),
            ScheduledNode(type="W", chunk=0, stage=3, minibatch=1),
            ScheduledNode(type="SEND_BACKWARD", chunk=0, stage=3, minibatch=1),
            # microbatch 2
            # chunk 0 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=0, stage=3, minibatch=2),
            ScheduledNode(type="F", chunk=0, stage=3, minibatch=2),
            ScheduledNode(type="SEND_FORWARD", chunk=0, stage=3, minibatch=2),
            # chunk 1 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=1, stage=3, minibatch=2),
            ScheduledNode(type="F", chunk=1, stage=3, minibatch=2),
            ScheduledNode(type="SEND_FORWARD", chunk=1, stage=3, minibatch=2),
            # chunk 1 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=1, stage=3, minibatch=2),
            ScheduledNode(type="B", chunk=1, stage=3, minibatch=2),
            ScheduledNode(type="W", chunk=1, stage=3, minibatch=2),
            ScheduledNode(type="SEND_BACKWARD", chunk=1, stage=3, minibatch=2),
            # chunk 0 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=0, stage=3, minibatch=2),
            ScheduledNode(type="B", chunk=0, stage=3, minibatch=2),
            ScheduledNode(type="W", chunk=0, stage=3, minibatch=2),
            ScheduledNode(type="SEND_BACKWARD", chunk=0, stage=3, minibatch=2),
            # microbatch 3
            # chunk 0 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=0, stage=3, minibatch=3),
            ScheduledNode(type="F", chunk=0, stage=3, minibatch=3),
            ScheduledNode(type="SEND_FORWARD", chunk=0, stage=3, minibatch=3),
            # chunk 1 fwd
            ScheduledNode(type="RECV_FORWARD", chunk=1, stage=3, minibatch=3),
            ScheduledNode(type="F", chunk=1, stage=3, minibatch=3),
            ScheduledNode(type="SEND_FORWARD", chunk=1, stage=3, minibatch=3),
            # chunk 1 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=1, stage=3, minibatch=3),
            ScheduledNode(type="B", chunk=1, stage=3, minibatch=3),
            ScheduledNode(type="W", chunk=1, stage=3, minibatch=3),
            ScheduledNode(type="SEND_BACKWARD", chunk=1, stage=3, minibatch=3),
            # chunk 0 bwd
            ScheduledNode(type="RECV_BACKWARD", chunk=0, stage=3, minibatch=3),
            ScheduledNode(type="B", chunk=0, stage=3, minibatch=3),
            ScheduledNode(type="W", chunk=0, stage=3, minibatch=3),
            ScheduledNode(type="SEND_BACKWARD", chunk=0, stage=3, minibatch=3),
        ],
    ]

    scheduler = ZeroBubbleVPipeScheduler(
        schedule=zbv_schedule[rank],  # hint: send whole schedule or local schedule only ?
        stage_manager=stage_manager,
        num_model_chunks=pp_size,
        num_microbatch=num_microbatch,
        overlap_p2p=False,
    )

    # loss func
    def criterion(x, *args, **kwargs):
        return (x * x).mean()

    # init model and input
    batch_size = 4
    num_layers = 8
    in_dim = out_dim = 8
    print(f"Before init Model: {torch.cuda.memory_allocated()/1024**3 :.3f} GB on device {stage_manager.get_rank()};")
    model = MlpModel(in_dim=in_dim, out_dim=out_dim, num_layers=num_layers).to(rank)
    input0 = torch.rand(in_dim, out_dim, requires_grad=True).to(rank)
    data_iter = [torch.rand(batch_size, in_dim, out_dim, requires_grad=True).to(rank)]

    [t.clone() for t in data_iter]
    model_base = deepcopy(model)

    if rank == 0:
        # layer 0 & 7 to chunk 0 on rank0
        local_chunk = torch.nn.ModuleList().to(rank)
        for idx, sub_model in enumerate(model.layers):
            if idx == 0 or idx == 7:
                local_chunk.append(sub_model)
    elif rank == 1:
        # layer 1 & 6 to chunk 1 on rank1
        local_chunk = torch.nn.ModuleList().to(rank)
        for idx, sub_model in enumerate(model.layers):
            if idx == 1 or idx == 6:
                local_chunk.append(sub_model)
    elif rank == 2:
        # layer 2 & 5 to chunk 2 on rank2
        local_chunk = torch.nn.ModuleList().to(rank)
        for idx, sub_model in enumerate(model.layers):
            if idx == 2 or idx == 5:
                local_chunk.append(sub_model)
    else:
        # layer 3 & 4 to chunk 3 on rank3
        local_chunk = torch.nn.Sequential().to(rank)
        for idx, sub_model in enumerate(model.layers):
            if idx == 3 or idx == 4:
                local_chunk.append(sub_model)
    print(
        f"After init Model & input: {torch.cuda.memory_allocated()/1024**3 :.3f} GB on device {stage_manager.get_rank()};"
    )

    torch.cuda.synchronize()
    scheduler.run_forward_backward(
        model_chunk=local_chunk,
        data_iter=iter(data_iter),
        criterion=criterion,
        optimizer=None,
        return_loss=None,
        return_outputs=None,
    )

    ##########################
    # Fwd bwd for base
    ##########################
    # fwd & bwd
    output_base = model_base(data_iter[0])
    loss_base = criterion(output_base)
    loss_base.backward()
    print(f"After base fwd & bwd: {torch.cuda.memory_allocated()/1024**3 :.3f} GB;")

    ##########################
    # assert weight
    ##########################
    if rank == 0:
        # layer 0
        assert_close(local_chunk[0].weight, model_base.layers[0].weight)
        assert_close(local_chunk[0].weight.grad, model_base.layers[0].weight.grad)
        # layer 7
        assert_close(local_chunk[1].weight, model_base.layers[7].weight)
        assert_close(local_chunk[1].weight.grad, model_base.layers[7].weight.grad)
    if rank == 1:
        # layer 1
        assert_close(local_chunk[0].weight, model_base.layers[1].weight)
        assert_close(local_chunk[0].weight.grad, model_base.layers[1].weight.grad)
        # layer 6
        assert_close(local_chunk[1].weight, model_base.layers[6].weight)
        assert_close(local_chunk[1].weight.grad, model_base.layers[6].weight.grad)
    if rank == 2:
        # layer 2
        assert_close(local_chunk[0].weight, model_base.layers[2].weight)
        assert_close(local_chunk[0].weight.grad, model_base.layers[2].weight.grad)
        # layer 5
        assert_close(local_chunk[1].weight, model_base.layers[5].weight)
        assert_close(local_chunk[1].weight.grad, model_base.layers[5].weight.grad)
    if rank == 3:
        # layer 3
        assert_close(local_chunk[0].weight, model_base.layers[3].weight)
        assert_close(local_chunk[0].weight.grad, model_base.layers[3].weight.grad)
        # layer 4
        assert_close(local_chunk[1].weight, model_base.layers[4].weight)
        assert_close(local_chunk[1].weight.grad, model_base.layers[4].weight.grad)


@pytest.mark.dist
# @pytest.mark.parametrize("num_microbatch", [4])
# @pytest.mark.parametrize("batch_size", [4])
# @pytest.mark.parametrize("num_model_chunk", [2])
@rerun_if_address_is_in_use()
def test_pp():
    spawn(
        test_run_fwd_bwd_iter_input,
        nprocs=4,
    )


if __name__ == "__main__":
    test_pp()
