"""
Utils for model inference
"""
import os
import re
from pathlib import Path
from typing import Optional, Tuple

import torch
from torch import nn

from colossalai.testing import free_port


def init_to_get_rotary(self, base=10000, use_elem=False):
    """
    This function initializes the rotary positional embedding, it is compatible for all models and is called in ShardFormer
    Args:
        self : Model that holds the rotary positional embedding
        base : calculation arg
        use_elem : activated when using chatglm-based models
    """
    self.config.head_dim_ = self.config.hidden_size // self.config.num_attention_heads
    if not hasattr(self.config, "rope_scaling"):
        rope_scaling_factor = 1.0
    else:
        rope_scaling_factor = self.config.rope_scaling.factor if self.config.rope_scaling is not None else 1.0

    if hasattr(self.config, "max_sequence_length"):
        max_seq_len = self.config.max_sequence_length
    elif hasattr(self.config, "max_position_embeddings"):
        max_seq_len = self.config.max_position_embeddings * rope_scaling_factor
    else:
        max_seq_len = 2048 * rope_scaling_factor
    base = float(base)

    # NTK  ref: https://www.reddit.com/r/LocalLLaMA/comments/14lz7j5/ntkaware_scaled_rope_allows_llama_models_to_have/
    ntk_alpha = os.environ.get("INFER_NTK_ALPHA", None)

    if ntk_alpha is not None:
        ntk_alpha = float(ntk_alpha)
        assert ntk_alpha >= 1, "NTK alpha must be greater than or equal to 1"
        if ntk_alpha > 1:
            print(f"Note: NTK enabled, alpha set to {ntk_alpha}")
        max_seq_len *= ntk_alpha
        base = base * (ntk_alpha ** (self.head_dim_ / (self.head_dim_ - 2)))  # Base change formula

    n_elem = self.config.head_dim_
    if use_elem:
        n_elem //= 2

    inv_freq = 1.0 / (base ** (torch.arange(0, n_elem, 2, device="cpu", dtype=torch.float32) / n_elem))
    t = torch.arange(max_seq_len + 1024 * 64, device="cpu", dtype=torch.float32) / rope_scaling_factor
    freqs = torch.outer(t, inv_freq)

    self._cos_cached = torch.cos(freqs).to(self.dtype).cuda()
    self._sin_cached = torch.sin(freqs).to(self.dtype).cuda()


def has_index_file(checkpoint_path: str) -> Tuple[bool, Optional[Path]]:
    """
    Check whether the checkpoint has an index file.

    Args:
        checkpoint_path (str): path to the checkpoint.

    Returns:
        Tuple[bool, Optional[Path]]: a tuple of (has_index_file, index_file_path)
    """
    checkpoint_path = Path(checkpoint_path)
    if checkpoint_path.is_file():
        # check if it is .index.json
        reg = re.compile("(.*?).index((\..*)?).json")
        if reg.fullmatch(checkpoint_path.name) is not None:
            return True, checkpoint_path
        else:
            return False, None
    elif checkpoint_path.is_dir():
        index_files = list(checkpoint_path.glob("*.index.*json"))

        for index_file in index_files:
            if "safetensors" in index_file.__str__():
                return True, index_file.__str__()  # return the safetensors file first

        if len(index_files) == 1:
            return True, index_files[0]
        else:
            assert (
                len(index_files) == 1
            ), f"Expected to find one .index.json file in {checkpoint_path}, but found {len(index_files)}"
            return False, None
    else:
        raise RuntimeError(f"Invalid checkpoint path {checkpoint_path}. Expected a file or a directory.")


def get_model_size(model: nn.Module):
    """Calculates the total size of the model weights (including biases) in bytes.
    Args:
        model: The PyTorch model to analyze.
    Returns:
        The total size of the model weights in bytes.
    """
    total_size = 0
    for key, param in model.named_parameters():
        total_size += param.element_size() * param.numel()
    return total_size / (1024**3)


def find_available_ports(num: int):
    try:
        free_ports = [free_port() for i in range(num)]
    except OSError as e:
        print(f"An OS error occurred: {e}")
        raise RuntimeError("Error finding available ports")
    return free_ports


"""
below just for profiling temporarily, will removed before merge
"""
import time
from contextlib import asynccontextmanager, contextmanager


@contextmanager
def timer(name=""):
    # (@lry89757) will remove later
    start_time = time.time()
    try:
        yield
    finally:
        end_time = time.time()
        elapsed_time = end_time - start_time
        print(f"{name} took {elapsed_time:.6f} seconds")


class Timer:
    # (@lry89757) will remove later
    def __init__(self, name=""):
        print(f"init timer, {name}")
        self.name = name
        self.times = []

    def __enter__(self):
        self.start_time = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        end_time = time.time()
        elapsed_time = end_time - self.start_time
        self.times.append(elapsed_time)
        print(f"{self.name} took {elapsed_time:.6f} seconds")
        self.print_info()

    def print_info(self):
        average_prefill_time = self.times[0]
        print(f"{self.name} prefill average time: {average_prefill_time:.6f} seconds")
        if len(self.times) > 1:
            average_decoding_time = sum(self.times[1:]) / len(self.times[1:])
            print(f"{self.name} decoding average time: {average_decoding_time:.6f} seconds")

    def __del__(self):
        if self.times:
            average_prefill_time = self.times[0]
            print(f"{self.name} prefill average time: {average_prefill_time:.6f} seconds")
            if len(self.times) > 1:
                average_decoding_time = sum(self.times[1:]) / len(self.times[1:])
                print(f"{self.name} decoding average time: {average_decoding_time:.6f} seconds")
        else:
            print(f"{self.name} no timings recorded")


@asynccontextmanager
async def async_timer(name=""):
    # (@lry89757) will remove later
    start_time = time.time()
    try:
        yield
    finally:
        end_time = time.time()
        elapsed_time = end_time - start_time
        print(f"{name} took {elapsed_time:.6f} seconds")
