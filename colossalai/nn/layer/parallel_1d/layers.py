#!/usr/bin/env python
# -*- encoding: utf-8 -*-

import math
import numbers
from contextlib import nullcontext
from typing import Callable, Tuple

import torch
import torch.nn.functional as F
from colossalai.communication import broadcast
from colossalai.context import ParallelMode, seed
from colossalai.core import global_context as gpc
from colossalai.nn import init as init
from colossalai.registry import LAYERS
from colossalai.utils import get_current_device
from torch import Tensor, dtype
from torch.nn.parameter import Parameter

from ..base_layer import ParallelLayer
from ..utils import divide, set_tensor_parallel_attribute_by_partition
from ._operation import FusedLayerNormAffineFunction1D
from ._utils import (gather_forward_split_backward, get_parallel_input, reduce_grad, reduce_input, set_parallel_input,
                     split_forward_gather_backward)


@LAYERS.register_module
class Linear1D(torch.nn.Module):
    def __init__(self,
                 in_features: int,
                 out_features: int,
                 bias: bool = True,
                 dtype: torch.dtype = None,
                 gather_output: bool = False,
                 skip_bias_add: bool = False,
                 weight_initializer: Callable = init.kaiming_uniform_(a=math.sqrt(5)),
                 bias_initializer: Callable = init.xavier_uniform_(a=1, scale=1)):
        super().__init__()
        parallel_input = get_parallel_input()
        if not parallel_input:
            self.layer = Linear1D_Col(in_features,
                                      out_features,
                                      bias=bias,
                                      dtype=dtype,
                                      gather_output=gather_output,
                                      skip_bias_add=skip_bias_add,
                                      weight_initializer=weight_initializer,
                                      bias_initializer=bias_initializer)
        else:
            self.layer = Linear1D_Row(in_features,
                                      out_features,
                                      bias=bias,
                                      dtype=dtype,
                                      parallel_input=parallel_input,
                                      skip_bias_add=skip_bias_add,
                                      weight_initializer=weight_initializer,
                                      bias_initializer=bias_initializer)

    @property
    def weight(self):
        return self.layer.weight

    @property
    def bias(self):
        return self.layer.bias

    def forward(self, input_: Tensor) -> Tensor:
        return self.layer(input_)


@LAYERS.register_module
class Classifier1D(ParallelLayer):
    """RowLinear with given weight"""

    def __init__(self,
                 in_features: int,
                 num_classes: int,
                 weight: Parameter = None,
                 bias: bool = True,
                 dtype: dtype = None,
                 weight_initializer: Callable = init.kaiming_uniform_(a=math.sqrt(5)),
                 bias_initializer: Callable = init.xavier_uniform_(a=1, scale=1)):
        super().__init__()
        self.in_features = in_features
        self.num_classes = num_classes
        self.parallel_input = get_parallel_input()

        # Divide the weight matrix along the last dimension.
        self.input_size_per_partition = divide(in_features, gpc.tensor_parallel_size)

        # Parameters.
        # Initialize weight.
        factory_kwargs = {'device': get_current_device(), 'dtype': dtype}
        if weight is not None:
            self.weight = weight
            self.has_weight = False
        else:
            self.weight = Parameter(torch.empty(self.num_classes, self.input_size_per_partition, **factory_kwargs))
            self.has_weight = True
        if bias:
            self.bias = Parameter(torch.empty(self.num_classes, **factory_kwargs))
        else:
            self.bias = None
        with seed(ParallelMode.TENSOR):
            self.reset_parameters(weight_initializer, bias_initializer)
        self._set_tensor_parallel_attributes()
        set_parallel_input(False)

    def reset_parameters(self, weight_initializer, bias_initializer) -> None:
        fan_in, fan_out = self.in_features, self.num_classes
        if self.has_weight:
            weight_initializer(self.weight, fan_in=fan_in, fan_out=fan_out)
        if self.bias is not None:
            bias_initializer(self.bias, fan_in=fan_in)
            broadcast(self.bias, gpc.get_ranks_in_group(ParallelMode.PARALLEL_1D)[0], ParallelMode.PARALLEL_1D)

    def _set_tensor_parallel_attributes(self):
        if self.has_weight:
            num_partition = gpc.get_world_size(ParallelMode.TENSOR)
            set_tensor_parallel_attribute_by_partition(self.weight, num_partition)

    def forward(self, input_: Tensor) -> Tensor:
        # Set up backprop all-reduce.
        if self.parallel_input:
            input_ = input_
        else:
            input_ = split_forward_gather_backward(input_, ParallelMode.PARALLEL_1D, dim=-1)

        output_parallel = F.linear(input_, self.weight)
        output = reduce_input(output_parallel, ParallelMode.PARALLEL_1D)
        if self.bias is not None:
            output = output + self.bias
        return output


@LAYERS.register_module
class Linear1D_Col(ParallelLayer):
    """Linear layer with column parallelism.

    The linear layer is defined as :math:`Y = XA + b`. A is parallelized along
    its second dimension as :math:`A = [A_1, ..., A_p]`.

    :param in_features: first dimension of matrix A.
    :type in_features: int
    :param output_size: second dimension of matrix A.
    :type output_size: int
    :param bias: If true, add bias, defaults to True
    :type bias: bool, optional
    :param dtype: The dtype of parameters, defaults to None
    :type dtype: torch.dtype, optional
    :param gather_output: If true, call all-gether on output and make Y avaiable
                    to all GPUs, otherwise, every GPU will have its output
                    which is :math:`Y_i = XA_i`, defaults to False
    :type gather_output: bool, optional
    """

    def __init__(self,
                 in_features: int,
                 out_features: int,
                 bias: bool = True,
                 dtype: torch.dtype = None,
                 gather_output: bool = False,
                 skip_bias_add: bool = False,
                 weight_initializer: Callable = init.kaiming_uniform_(a=math.sqrt(5)),
                 bias_initializer: Callable = init.xavier_uniform_(a=1, scale=1)):
        super().__init__()

        # Keep input parameters
        self.in_features = in_features
        self.out_features = out_features
        self.gather_output = gather_output
        self.skip_bias_add = skip_bias_add

        if skip_bias_add and not bias:
            raise ValueError('cannot skip bias addition if bias is None')

        self.out_features_per_partition = divide(out_features, gpc.tensor_parallel_size)

        # Parameters.
        # Initialize weight.
        factory_kwargs = {'device': get_current_device(), 'dtype': dtype}
        self.weight = Parameter(torch.empty(self.out_features_per_partition, self.in_features, **factory_kwargs))

        if bias:
            self.bias = Parameter(torch.empty(self.out_features_per_partition, **factory_kwargs))
        else:
            self.bias = None
        with seed(ParallelMode.TENSOR):
            self.reset_parameters(weight_initializer, bias_initializer)
        self._set_tensor_parallel_attributes()
        set_parallel_input(True)

    def reset_parameters(self, weight_initializer, bias_initializer) -> None:
        fan_in, fan_out = self.in_features, self.out_features
        weight_initializer(self.weight, fan_in=fan_in, fan_out=fan_out)
        if self.bias is not None:
            bias_initializer(self.bias, fan_in=fan_in)

    def _set_tensor_parallel_attributes(self):
        num_partition = gpc.get_world_size(ParallelMode.TENSOR)
        set_tensor_parallel_attribute_by_partition(self.weight, num_partition)
        if self.bias is not None:
            set_tensor_parallel_attribute_by_partition(self.bias, num_partition)

    def forward(self, input_: Tensor) -> Tuple[Tensor, Tensor]:
        # Set up backprop all-reduce.
        input_parallel = reduce_grad(input_, ParallelMode.PARALLEL_1D)
        # Matrix multiply.

        bias = self.bias if not self.skip_bias_add else None
        output_parallel = F.linear(input_parallel, self.weight, bias)
        if self.gather_output:
            # All-gather across the partitions.
            output = gather_forward_split_backward(output_parallel, ParallelMode.PARALLEL_1D, dim=-1)
        else:
            output = output_parallel
        if self.skip_bias_add:
            return output, self.bias
        else:
            return output


@LAYERS.register_module
class Linear1D_Row(ParallelLayer):
    """ Linear layer with row parallelism 

    :param in_features: size of each input sample
    :type in_features: int
    :param out_features: size of each output sample
    :type out_features: int
    :param bias: If set to ``False``, the layer will not learn an additive bias, defaults to True
    :type bias: bool, optional
    :param dtype: The dtype of parameters, defaults to None
    :type dtype: torch.dtype, optional
    :param parallel_input: If set to ``True``, it's assumed that the input is splitted, defaults to False
    :type parallel_input: bool, optional
    """

    def __init__(self,
                 in_features: int,
                 out_features: int,
                 bias: bool = True,
                 dtype: torch.dtype = None,
                 parallel_input: bool = True,
                 skip_bias_add: bool = False,
                 weight_initializer: Callable = init.kaiming_uniform_(a=math.sqrt(5)),
                 bias_initializer: Callable = init.xavier_uniform_(a=1, scale=1)):
        super().__init__()

        # Keep input parameters
        self.in_features = in_features
        self.out_features = out_features
        self.parallel_input = parallel_input
        self.skip_bias_add = skip_bias_add

        if skip_bias_add and not bias:
            raise ValueError('cannot skip bias addition if bias is None')

        # Divide the weight matrix along the last dimension.
        self.input_size_per_partition = divide(in_features, gpc.tensor_parallel_size)

        # Parameters.
        # Initialize weight.
        factory_kwargs = {'device': get_current_device(), 'dtype': dtype}
        self.weight = Parameter(torch.empty(self.out_features, self.input_size_per_partition, **factory_kwargs))

        if bias:
            self.bias = Parameter(torch.empty(self.out_features, **factory_kwargs))
        else:
            self.bias = None
        with seed(ParallelMode.TENSOR):
            self.reset_parameters(weight_initializer, bias_initializer)
        self._set_tensor_parallel_attributes()
        set_parallel_input(False)

    def reset_parameters(self, weight_initializer, bias_initializer) -> None:
        fan_in, fan_out = self.in_features, self.out_features
        weight_initializer(self.weight, fan_in=fan_in, fan_out=fan_out)
        if self.bias is not None:
            bias_initializer(self.bias, fan_in=fan_in)
            broadcast(self.bias, gpc.get_ranks_in_group(ParallelMode.PARALLEL_1D)[0], ParallelMode.PARALLEL_1D)

    def _set_tensor_parallel_attributes(self):
        num_partition = gpc.get_world_size(ParallelMode.TENSOR)
        set_tensor_parallel_attribute_by_partition(self.weight, num_partition)

    def forward(self, input_: Tensor) -> Tensor:
        # Set up backprop all-reduce.
        if self.parallel_input:
            input_ = input_
        else:
            input_ = split_forward_gather_backward(input_, ParallelMode.PARALLEL_1D, dim=-1)

        output_parallel = F.linear(input_, self.weight)
        output = reduce_input(output_parallel, ParallelMode.PARALLEL_1D)

        if not self.skip_bias_add:
            if self.bias is not None:
                output = output + self.bias
            return output
        else:
            return output, self.bias


@LAYERS.register_module
class MixedFusedLayerNorm1D(torch.nn.Module):
    """ Experimental
    """

    def __init__(self, normalized_shape, eps=1e-5):
        super(MixedFusedLayerNorm1D, self).__init__()

        if isinstance(normalized_shape, numbers.Integral):
            normalized_shape = (normalized_shape, )
        self.normalized_shape = torch.Size(normalized_shape)
        self.eps = eps
        self.weight = Parameter(torch.Tensor(*normalized_shape))
        self.bias = Parameter(torch.Tensor(*normalized_shape))
        self.reset_parameters()

    def reset_parameters(self):
        init.ones_(self.weight)
        init.zeros_(self.bias)

    def forward(self, input):
        return FusedLayerNormAffineFunction1D.apply(input, self.weight, self.bias, self.normalized_shape, self.eps)


@LAYERS.register_module
class Embedding1D(ParallelLayer):
    def __init__(self,
                 num_embeddings: int,
                 embedding_dim: int,
                 padding_idx: int = None,
                 dtype: dtype = None,
                 weight_initializer: Callable = init.normal_(),
                 *args,
                 **kwargs):
        super().__init__()

        self.num_embeddings = num_embeddings
        self.embed_dim = embedding_dim
        embed_dim_per_partition = divide(embedding_dim, gpc.tensor_parallel_size)

        self.padding_idx = padding_idx
        self.embed_args = args
        self.embed_kwargs = kwargs

        self.weight = Parameter(
            torch.empty((num_embeddings, embed_dim_per_partition), device=get_current_device(), dtype=dtype))

        self.reset_parameters(weight_initializer)
        self._set_tensor_parallel_attributes()
        set_parallel_input(False)

    def _set_tensor_parallel_attributes(self):
        set_tensor_parallel_attribute_by_partition(self.weight, gpc.tensor_parallel_size)

    def reset_parameters(self, weight_initializer) -> None:
        with seed(ParallelMode.TENSOR):
            fan_in, fan_out = self.num_embeddings, self.embed_dim
            weight_initializer(self.weight, fan_in=fan_in, fan_out=fan_out)
            self._fill_padding_idx_with_zero()

    def _fill_padding_idx_with_zero(self) -> None:
        if self.padding_idx is not None:
            with torch.no_grad():
                self.weight[self.padding_idx].fill_(0)

    def forward(self, input_: Tensor) -> Tensor:

        output_parallel = F.embedding(input_, self.weight, self.padding_idx, *self.embed_args, **self.embed_kwargs)

        output = gather_forward_split_backward(output_parallel, ParallelMode.PARALLEL_1D, dim=-1)

        return output


@LAYERS.register_module
class Dropout1D(ParallelLayer):
    def __init__(self, p: float = 0.5, inplace: bool = False):
        super().__init__()
        self.parallel_input = get_parallel_input()
        self.p = p
        self.inplace = inplace

    def forward(self, input_: Tensor) -> Tensor:
        cm = nullcontext() if not self.parallel_input else seed(ParallelMode.TENSOR)
        with cm:
            output = F.dropout(input_, self.p, self.training, self.inplace)
        return output
