import torch
import torch.distributed as dist
from colossalai.tensor import ColoTensor, ColoTensorSpec
from colossalai.tensor.distspec import _DistSpec


def gather_tensor(colo_tensor: ColoTensor) -> None:
    """Make colo_tensor replicated when the rank is 0
    """
    if not colo_tensor.is_replicate():
        pg = colo_tensor.get_process_group()
        # for the group which contains rank 0
        if pg.tp_rank_list()[0] == 0:
            old_dist_spec = colo_tensor.dist_spec
            colo_tensor.to_replicate_()
            if dist.get_rank() != 0:
                colo_tensor.set_dist_spec(old_dist_spec)

        # synchronize all processes for unexpected problems
        dist.barrier()

    if dist.get_rank() == 0:
        setattr(colo_tensor, 'save_ready', True)  # set saving signitrue


def scatter_tensor(colo_tensor: ColoTensor, dist_spec: _DistSpec) -> None:
    """Reversal operation of `gather_tensor`.
    """
    if dist_spec.placement == 'r':
        dist.broadcast(colo_tensor.data, 0)
    else:
        global_size = colo_tensor.size_global()

        if dist.get_rank() == 0:
            entire_data = colo_tensor.data
        else:
            entire_data = torch.empty(global_size, device=colo_tensor.device)
        dist.broadcast(entire_data, 0)

        if dist.get_rank() == 0:
            colo_tensor.set_dist_spec(dist_spec)
        else:
            rep_tensor = ColoTensor(entire_data, ColoTensorSpec(
                pg=colo_tensor.get_process_group(),
                compute_attr=colo_tensor.compute_spec))
            rep_tensor.set_dist_spec(dist_spec)
            with torch.no_grad():
                colo_tensor.data.copy_(rep_tensor.data)
        # synchronize all processes for unexpected problems
        dist.barrier()
