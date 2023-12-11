#!/usr/bin/env python

from typing import Any, Callable, Dict, List, Tuple, Union

import torch
import torch.distributed as dist

from .base_accelerator import BaseAccelerator

IS_NPU_AVAILABLE = False
try:
    import torch_npu  # noqa

    IS_NPU_AVAILABLE = True
except ImportError:
    pass


__all__ = ["NpuAccelerator"]


class NpuAccelerator(BaseAccelerator):
    """
    Accelerator class for Huawei NPU devices.
    """

    def __init__(self):
        super().__init__(name="npu", communication_backend="hccl", is_synchronous=False)

    # =======================
    # device APIs
    # =======================
    def get_current_device(self) -> torch.device:
        """
        Return the current device.
        """
        return torch.device(f"npu:{torch.npu.current_device()}")

    def current_device(self) -> int:
        """
        Return the current device index.
        """
        return torch.npu.current_device()

    def set_device(self, device: Union[torch.device, int]) -> None:
        """
        Bind the current process to a device.
        """
        if device is None:
            if not dist.is_initialized():
                raise RuntimeError("Cannot get current device when distributed is not initialized.")
            device = dist.get_rank() % self.device_count()
        torch.npu.set_device(device)

    def get_device_name(self, device: Union[torch.device, int]) -> str:
        """
        Return the name of the device.
        """
        return torch.npu.get_device_name(device)

    def synchronize(self, device: Union[torch.device, int] = None):
        """
        Synchronize the current process.
        """
        torch.npu.synchronize(device)

    def is_available(self):
        """
        Check if the accelerator is available.
        """
        return torch.npu.is_available()

    def device_count(self):
        """
        Return the number of devices on the machine.
        """
        return torch.npu.device_count()

    def get_device_capability(self, device=None) -> Tuple[int, int]:
        """
        Gets the npu capability of a device.
        """
        return torch.npu.get_device_capability(device)

    def get_device_name(self, device=None) -> str:
        """
        Gets the name of a device.
        """
        return torch.npu.get_device_name(device)

    def get_device_properties(self, device):
        """
        Gets the properties of a device.
        """
        return torch.npu.get_device_properties(device)

    def utilization(self, device=None) -> int:
        """
        Returns the percent of time over the past sample period during which one or more kernels was executing on the GPU as given by nvidia-smi
        """
        return torch.npu.utilization(device)

    # =======================
    # random number generator APIs
    # =======================
    def get_rng_state(self, device="npu") -> torch.Tensor:
        """
        Returns the random number generator state of the specified GPU as a ByteTensor.
        """
        return torch.npu.get_rng_state(device)

    def get_rng_state_all(self) -> List[torch.Tensor]:
        """
        Returns a list of ByteTensor representing the random number states of all devices.
        """
        return torch.npu.get_rng_state_all()

    def set_rng_state(self, new_state: torch.ByteTensor, device: str = "npu") -> None:
        """
        Sets the random number generator state of the specified GPU.
        """
        torch.npu.set_rng_state(new_state, device)

    def set_rng_state_all(self, new_states: List[torch.ByteTensor]) -> None:
        """
        Sets the random number generator state of all devices.
        """
        torch.npu.set_rng_state_all(new_states)

    def manual_seed(self, seed: int) -> None:
        """
        Sets the seed for generating random numbers for the current GPU.
        """
        torch.npu.manual_seed(seed)

    def manual_seed_all(self, seed: int) -> None:
        """
        Set the random seed for the all processes.
        """
        torch.npu.manual_seed_all(seed)

    def seed(self) -> None:
        """
        Sets the seed for generating random numbers to a random number for the current GPU.
        """
        torch.npu.seed()

    def seed_all(self) -> None:
        """
        Sets the seed for generating random numbers to a random number on all GPUs.
        """
        torch.npu.seed_all()

    def initial_seed(self) -> int:
        """
        Returns the current random seed of the current GPU.
        """
        return torch.npu.initial_seed()

    # =======================
    # memory management APIs
    # =======================

    def empty_cache(self) -> None:
        """
        Releases all unoccupied cached memory currently held by the caching allocator so that those can be used in other GPU application and visible in nvidia-smi.
        """
        torch.npu.empty_cache()

    def memory_stats(self, device=None) -> Dict[str, Any]:
        """
        Returns a dictionary of npu memory allocator statistics for a given device.
        """
        return torch.npu.memory_stats(device=device)

    def memory_summary(self, device=None, abbreviated=False) -> str:
        """
        Returns a human-readable printout of the current memory allocator statistics for a given device.
        """
        return torch.npu.memory_summary(device=device, abbreviated=abbreviated)

    def memory_snapshot(self):
        """
        Returns a snapshot of the npu memory allocator state across all devices.
        """
        return torch.npu.memory_snapshot()

    def memory_allocated(self, device=None) -> int:
        """
        Returns the current GPU memory occupied by tensors in bytes for a given device.
        """
        return torch.npu.memory_allocated(device=device)

    def max_memory_allocated(self, device=None) -> int:
        """
        Returns the maximum GPU memory occupied by tensors in bytes for a given device.
        """
        return torch.npu.max_memory_allocated(device=device)

    def reset_max_memory_allocated(self, device=None) -> None:
        """
        Resets the starting point in tracking maximum GPU memory occupied by tensors for a given device.
        """
        torch.npu.reset_max_memory_allocated(device=device)

    def reset_max_memory_cached(self, device=None) -> None:
        """
        Resets the starting point in tracking maximum GPU memory managed by the caching allocator for a given device.
        """
        torch.npu.reset_max_memory_cached(device=device)

    def memory_reserved(self, device=None) -> int:
        """
        Returns the current GPU memory managed by the caching allocator in bytes for a given device.
        """
        return torch.npu.memory_reserved(device=device)

    def max_memory_reserved(self, device=None) -> int:
        """
        Returns the maximum GPU memory managed by the caching allocator in bytes for a given device.
        """
        return torch.npu.max_memory_reserved(device=device)

    def set_per_process_memory_fraction(self, fraction: float, device=None) -> None:
        """
        Set memory fraction for a process.
        """
        torch.npu.set_per_process_memory_fraction(fraction, device=device)

    def reset_peak_memory_stats(self, device=None) -> None:
        """
        Resets the "peak" stats tracked by the npu memory allocator.
        """
        torch.npu.reset_peak_memory_stats(device=device)

    # =======================
    # streams and events APIs
    # =======================

    def Stream(self, device=None, priority=0, **kwargs):
        """
        A npu stream is a linear sequence of execution that belongs to a specific device, independent from other streams. See npu-semantics for details.
        """
        return torch.npu.Stream(device, priority, **kwargs)

    def Event(self, enable_timing: bool = False, blocking: bool = False, interprocess: bool = False):
        """
        npu events are synchronization markers that can be used to monitor the device's progress, to accurately measure timing, and to synchronize npu streams.
        """
        return torch.npu.Event(enable_timing, blocking, interprocess)

    def current_stream(self, device=None):
        """
        Returns the currently selected Stream for a given device.
        """
        return torch.npu.current_stream(device)

    def default_stream(self, device=None):
        """
        Returns the default Stream for a given device.
        """
        return torch.npu.default_stream(device)

    def set_stream(self, stream_):
        """
        Sets the current stream.This is a wrapper API to set the stream.
        """
        torch.npu.set_stream(stream_)

    def stream(self, stream_):
        """
        Wrapper around the Context-manager StreamContext that selects a given stream.
        """
        return torch.npu.stream(stream_)

    # =======================
    # amp APIs
    # =======================
    def autocast(
        self, enabled: bool = True, dtype: torch.dtype = torch.float16, cache_enabled: bool = True
    ) -> Callable:
        """
        Return autocast function
        """
        return torch.npu.amp.autocast(enabled=enabled, dtype=dtype, cache_enabled=cache_enabled)
