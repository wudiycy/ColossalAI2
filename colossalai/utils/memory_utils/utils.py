import torch
from colossalai.utils import get_current_device

from typing import Tuple, Union, Optional

from collections import namedtuple
import psutil

_GLOBAL_CUDA_MEM_FRACTION = 1.0


# copy from PatrickStar
def _get_cpu_memory_info():
    ps_mem_info = namedtuple("ps_mem_info", ["total", "free", "cached", "buffers", "used"])
    try:
        # psutil reads the memory info from /proc/memory_info,
        # which results in returning the host memory instead of
        # that of container.
        # Here we try to read the container memory with method in:
        # https://stackoverflow.com/a/46213331/5163915
        mems = {}
        with open("/sys/fs/cgroup/memory/memory.meminfo", "rb") as f:
            for line in f:
                fields = line.split()
                mems[fields[0]] = int(fields[1]) * 1024
        total = mems[b"MemTotal:"]
        free = mems[b"MemFree:"]
        cached = mems[b"Cached:"]
        buffers = mems[b"Buffers:"]
        used = total - free - cached - buffers
        if used < 0:
            used = total - free
        mem_info = ps_mem_info(total=total, free=free, cached=cached, buffers=buffers, used=used)
    except FileNotFoundError:
        mems = psutil.virtual_memory()
        mem_info = ps_mem_info(
            total=mems.total,
            free=mems.free,
            cached=mems.cached,
            buffers=mems.buffers,
            used=mems.used,
        )
    return mem_info


def colo_cpu_memory_used(device: Optional[torch.device] = None) -> int:
    """Get the free memory info of a cpu device.

    Args:
       device (Optional[``torch.device``]): a torch device instance or None. Defaults None.

    Returns:
        int: current memory usage, sized by Byte.
    """
    if device:
        assert device.type == 'cpu'
    else:
        device = torch.device('cpu')

    mem_info = _get_cpu_memory_info()
    # FIXME(jiaruifang) only work for 1-CPU multi-GPU
    # CPU memory is sharded with all processes
    # Not support multi-GPU multi-CPU
    # We need a local_world_size here
    ret = mem_info.used
    return ret


def colo_cuda_memory_used(device: Optional[torch.device] = None) -> int:
    """Get the free memory info of device.

    Args:
       device (Optional[``torch.device``]): a torch device instance or None. Defaults None.

    Returns:
        int: current memory usage, sized by Byte.
    """
    if device:
        assert device.type == 'cuda'
    else:
        device = torch.device(f'cuda:{get_current_device()}')

    ret: int = torch.cuda.memory_allocated(device)
    # get the peak memory to report correct data, so reset the counter for the next call
    if hasattr(torch.cuda, "reset_peak_memory_stats"):    # pytorch 1.4+
        torch.cuda.reset_peak_memory_stats(device)
    return ret


def colo_set_process_memory_fraction(ratio: float) -> None:
    """colo_set_process_memory_fraction 

    set how much cuda memory used on the gpu belonging to the current process.

    Args:
        ratio (float): a ratio between 0. ~ 1.
    """
    global _GLOBAL_CUDA_MEM_FRACTION
    _GLOBAL_CUDA_MEM_FRACTION = ratio
    torch.cuda.set_per_process_memory_fraction(_GLOBAL_CUDA_MEM_FRACTION, get_current_device())


def colo_cuda_memory_capacity() -> float:
    """
    Get cuda memory capacity of the current cuda.
    """
    return torch.cuda.get_device_properties(get_current_device()).total_memory * _GLOBAL_CUDA_MEM_FRACTION
