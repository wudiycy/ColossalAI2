from typing import Dict, List, Union

import torch
import torch.nn.functional as F

from colossalai.auto_parallel.tensor_shard.utils import update_partition_dim
from colossalai.logging import get_dist_logger
from colossalai.tensor.sharding_spec import ShardingNotDivisibleError

from ..sharding_strategy import OperationData, OperationDataType, ShardingStrategy
from .node_handler import ModuleHandler, NodeHandler
from .registry import operator_registry
from .strategy import EmbeddingStrategyGenerator, StrategyGenerator

__all__ = ['EmbeddingModuleHandler', 'EmbeddingFunctionHandler']


def _convert_logical_sharding_to_physical_sharding_spec_for_embedding(strategy: ShardingStrategy, input_name: str,
                                                                      output_name: str) -> List[ShardingStrategy]:
    """
    This function converts the logical sharding spec to the physical sharding spec for both the input and output
    of the embedding operation.

    Args:
        strategy (ShardingStrategy): the logical strategy generated by the strategy generator.
        input_name (str): the name of the OperationData object for the input.
        output_name (str): the name of the OperationData object for the output.
    """
    # the result will be a list of strategies
    sharding_strategies = []

    # get operation data
    input_op_data = strategy.get_op_data_by_name(input_name)
    output_op_data = strategy.get_op_data_by_name(output_name)
    input_sharding_spec = strategy.get_sharding_spec_by_name(input_op_data.name)
    output_sharding_spec = strategy.get_sharding_spec_by_name(output_op_data.name)

    # recover the last logical dimension to physical dimension
    last_logical_output_dims = len(output_op_data.logical_shape) - 1
    last_physical_output_dims = output_op_data.data.dim() - 1

    # get logger for debug message
    logger = get_dist_logger()

    # For the input of the embedding operation, it can be multi-dimensional. The sharding spec is only generated for
    # logical 1D non-matrix dimension, the logical non-matrix dimension can belong to the 0th to Nth dimension of the
    # physical input shape. Thus, we enumerate to get all possible cases.
    if input_sharding_spec.dim_partition_dict:
        # if bool(input_sharding_spec.dim_partition_dict), it means that the
        # the generated sharding strategy does shard the non-matrix dimension,
        # in this case, we need to do enumeration
        num_input_dims = input_op_data.data.dim()
        for i in range(num_input_dims):
            strategy_copy = strategy.clone()
            input_sharding_spec = strategy_copy.get_sharding_spec_by_name(input_op_data.name)
            output_sharding_spec = strategy_copy.get_sharding_spec_by_name(output_op_data.name)
            try:
                # replace the 0th dimension in the logical sharding with ith dimension in the physical sharding
                update_partition_dim(sharding_spec=input_sharding_spec,
                                     dim_mapping={0: i},
                                     physical_shape=input_op_data.data.shape,
                                     inplace=True)

                if last_logical_output_dims in output_sharding_spec.dim_partition_dict:
                    dim_mapping = {0: i, last_logical_output_dims: last_physical_output_dims}
                else:
                    dim_mapping = {0: i}

                update_partition_dim(sharding_spec=output_sharding_spec,
                                     dim_mapping=dim_mapping,
                                     physical_shape=output_op_data.data.shape,
                                     inplace=True)

                strategy_copy.name = f'{strategy.name}_{i}'
                sharding_strategies.append(strategy_copy)

            except ShardingNotDivisibleError as e:
                logger.debug(
                    f'Errored occurred when converting the logical sharding spec to the physical one. Error details: {e}'
                )
    else:
        # the generated sharding strategy does not shard the non-matrix dimension,
        # in this case, we don't need to do enumeration
        # but instead, we still need to convert the logical shape to physical shape
        strategy_copy = strategy.clone()
        input_sharding_spec = strategy_copy.get_sharding_spec_by_name(input_op_data.name)
        output_sharding_spec = strategy_copy.get_sharding_spec_by_name(output_op_data.name)

        # after updating, the logical shape will be replaced by the physical shape
        update_partition_dim(sharding_spec=input_sharding_spec,
                             dim_mapping={},
                             physical_shape=input_op_data.data.shape,
                             inplace=True)

        if last_logical_output_dims in output_sharding_spec.dim_partition_dict:
            dim_mapping = {last_logical_output_dims: last_physical_output_dims}
        else:
            dim_mapping = {}

        update_partition_dim(sharding_spec=output_sharding_spec,
                             dim_mapping=dim_mapping,
                             physical_shape=output_op_data.data.shape,
                             inplace=True)
        sharding_strategies.append(strategy_copy)

    return sharding_strategies


@operator_registry.register(torch.nn.Embedding)
class EmbeddingModuleHandler(ModuleHandler):
    """
    A EmbeddingModuleHandler which deals with the sharding strategies for nn.Embedding module.
    """

    def get_strategy_generator(self) -> List[StrategyGenerator]:
        op_data_mapping = self.get_operation_data_mapping()
        generators = []
        generators.append(EmbeddingStrategyGenerator(op_data_mapping, self.device_mesh))
        return generators

    def get_operation_data_mapping(self) -> Dict[str, OperationData]:
        # In nn.Embedding operation, all the dimensions of input will be treated as the batch dimension,
        # and then the sharding spec will be generated based on the logical 1D tensor.
        # After that, the logical sharding info will be enumerated among all the physical dimensions.
        # Finally, the input will be transformed back to its original shape in self.post_process
        input_meta_data = self.node.args[0]._meta_data
        input_logical_shape = input_meta_data.view(-1).shape
        physical_input_operand = OperationData(name=str(self.node.args[0]),
                                               type=OperationDataType.ARG,
                                               data=input_meta_data,
                                               logical_shape=input_logical_shape)

        physical_other_operand = OperationData(name="weight",
                                               type=OperationDataType.PARAM,
                                               data=self.named_parameters['weight'])

        # Same as input, in nn.Embedding operation, all the dimensions of output will be treated as
        # (batch dimension, embedding dimension), and then the sharding spec will be generated based
        # on the logical 2D tensor.
        # After that, the logical sharding info of batch dimension will be enumerated among all the physical dimensions.
        # Finally, the output will be transformed back to its original shape in self.post_process
        output_meta_data = self.node._meta_data
        output_logical_shape = output_meta_data.view(-1, output_meta_data.shape[-1]).shape
        physical_output = OperationData(name=str(self.node),
                                        type=OperationDataType.OUTPUT,
                                        data=output_meta_data,
                                        logical_shape=output_logical_shape)

        mapping = {"input": physical_input_operand, "other": physical_other_operand, "output": physical_output}

        return mapping

    def post_process(self, strategy: ShardingStrategy) -> Union[ShardingStrategy, List[ShardingStrategy]]:
        """
        Convert the sharding spec from the logical shape to the physical shape.
        """
        # create multiple sharding strategies for the inputs
        # as input can be multi-dimensional and the partition dim is only 2D,
        # we need to map the partition at logical dim 0 to one of the first few dimensions of the input and output
        strategies = _convert_logical_sharding_to_physical_sharding_spec_for_embedding(strategy=strategy,
                                                                                       input_name=str(
                                                                                           self.node.args[0]),
                                                                                       output_name=str(self.node))
        return strategies


@operator_registry.register(F.embedding)
class EmbeddingFunctionHandler(NodeHandler):
    """
    A EmbeddingFunctionHandler which deals with the sharding strategies for F.embedding.
    """

    def get_strategy_generator(self) -> List[StrategyGenerator]:
        op_data_mapping = self.get_operation_data_mapping()
        generators = []
        generators.append(EmbeddingStrategyGenerator(op_data_mapping, self.device_mesh))
        return generators

    def get_operation_data_mapping(self) -> Dict[str, OperationData]:
        # In F.embedding operation, all the dimensions of input will be treated as the batch dimension,
        # and then the sharding spec will be generated based on the logical 1D tensor.
        # After that, the logical sharding info will be enumerated among all the physical dimensions.
        # Finally, the input will be transformed back to its original shape in self.post_process
        input_meta_data = self.node.args[0]._meta_data
        input_logical_shape = input_meta_data.view(-1).shape
        physical_input_operand = OperationData(name=str(self.node.args[0]),
                                               type=OperationDataType.ARG,
                                               data=self.node.args[0]._meta_data,
                                               logical_shape=input_logical_shape)

        # check if the other operand is a parameter
        if isinstance(self.node.args[1]._meta_data, torch.nn.parameter.Parameter):
            data_type = OperationDataType.PARAM
        else:
            data_type = OperationDataType.ARG

        physical_other_operand = OperationData(name=str(self.node.args[1]),
                                               type=data_type,
                                               data=self.node.args[1]._meta_data)

        # Same as input, in F.embedding operation, all the dimensions of output will be treated as
        # (batch dimension, embedding dimension), and then the sharding spec will be generated based
        # on the logical 2D tensor.
        # After that, the logical sharding info of batch dimension will be enumerated among all the physical dimensions.
        # Finally, the output will be transformed back to its original shape in self.post_process
        output_meta_data = self.node._meta_data
        output_logical_shape = output_meta_data.view(-1, output_meta_data.shape[-1]).shape
        physical_output = OperationData(
            name=str(self.node),
            type=OperationDataType.OUTPUT,
            data=self.node._meta_data,
            logical_shape=output_logical_shape,
        )

        mapping = {"input": physical_input_operand, "other": physical_other_operand, "output": physical_output}

        return mapping

    def post_process(self, strategy: ShardingStrategy):
        """
        Convert the sharding spec from the logical shape to the physical shape.
        """
        # create multiple sharding strategies for the inputs
        # as input can be multi-dimensional and the partition dim is only 2D,
        # we need to map the partition at logical dim 0 to one of the first few dimensions of the input and output
        strategies = _convert_logical_sharding_to_physical_sharding_spec_for_embedding(strategy=strategy,
                                                                                       input_name=str(
                                                                                           self.node.args[0]),
                                                                                       output_name=str(self.node))
        return strategies
