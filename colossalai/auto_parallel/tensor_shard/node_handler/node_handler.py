from abc import ABC, abstractmethod
from typing import Dict, List, Tuple, Union

import torch
from torch.fx.node import Node

from colossalai.auto_parallel.meta_profiler.shard_metainfo import ShardMetaInfo, meta_register
from colossalai.auto_parallel.tensor_shard.options import ShardOption, SolverPerference
from colossalai.auto_parallel.tensor_shard.sharding_strategy import (
    OperationData,
    OperationDataType,
    ShardingSpec,
    ShardingStrategy,
    StrategiesVector,
    TrainCycleItem,
)
from colossalai.auto_parallel.tensor_shard.utils import check_sharding_spec_validity
from colossalai.device.device_mesh import DeviceMesh
from colossalai.logging import get_dist_logger
from colossalai.tensor.shape_consistency import ShapeConsistencyManager

from .strategy import StrategyGenerator


class NodeHandler(ABC):
    '''
    The NodeHandler is an abstract class used to generate every possible strategies for an operator node.

    Args:
        node (Node): the input node in node argument list.
        device_mesh (DeviceMesh): A logical view of a physical mesh.
        strategies_vector (StrategiesVector): all the strategies generated in this handler will be recorded into the strategies_vector.
    '''

    def __init__(self,
                 node: Node,
                 device_mesh: DeviceMesh,
                 strategies_vector: StrategiesVector,
                 shard_option: ShardOption = ShardOption.STANDARD,
                 solver_perference: SolverPerference = SolverPerference.STANDARD) -> None:
        self.node = node
        self.predecessor_node = list(node._input_nodes.keys())
        self.successor_node = list(node.users.keys())
        self.device_mesh = device_mesh
        self.strategies_vector = strategies_vector
        self.shard_option = shard_option
        self.solver_perference = solver_perference

    def update_resharding_cost(self, strategy: ShardingStrategy) -> None:
        """
        Compute the resharding costs and save the costs in the ShardingStrategy object.
        """
        # TODO: test this function when other handlers are ready
        resharding_costs = {}
        shape_consistency_manager = ShapeConsistencyManager()

        for node in self.predecessor_node:
            node_name = str(node)
            # get the current sharding spec generated by this node handler

            # we will not compute the resharding costs for the node not counted in the strategy.
            # And the node with tuple or list output need to be handled below.
            node_in_strategy = [op_data.name for op_data in strategy.sharding_specs.keys()]
            if str(node) not in node_in_strategy:
                continue

            op_data = strategy.get_op_data_by_name(node_name)
            current_sharding_spec = strategy.sharding_specs[op_data]
            # get the sharding specs for this node generated
            # in its own node handler
            assert hasattr(node, 'strategies_vector'), \
                f'The predecessor node {node_name} has no strategy vector to compute the resharding cost.'
            prev_strategy_vector = node.strategies_vector
            prev_sharding_specs = [
                prev_strategy.get_sharding_spec_by_name(node_name) for prev_strategy in prev_strategy_vector
            ]

            # create data structrure to store costs
            if node not in resharding_costs:
                resharding_costs[node] = []

            def _compute_resharding_cost(
                    prev_sharding_spec: Union[ShardingSpec,
                                              List[ShardingSpec]], current_sharding_spec: Union[ShardingSpec,
                                                                                                List[ShardingSpec]],
                    data: Union[torch.Tensor, List[torch.Tensor], Tuple[torch.Tensor]]) -> TrainCycleItem:
                """
                This is a helper function to compute the resharding cost for a specific strategy of a node.
                """
                if prev_sharding_spec is None:
                    return TrainCycleItem(fwd=0, bwd=0, total=0)
                elif isinstance(prev_sharding_spec, ShardingSpec):
                    if isinstance(data, torch.Tensor):
                        dtype = data.dtype
                        size_per_elem_bytes = torch.tensor([], dtype=dtype).element_size()
                        _, _, consistency_cost = shape_consistency_manager.shape_consistency(
                            prev_sharding_spec, current_sharding_spec)

                        resharding_cost = TrainCycleItem(fwd=consistency_cost["forward"] * size_per_elem_bytes,
                                                         bwd=consistency_cost["backward"] * size_per_elem_bytes,
                                                         total=consistency_cost["total"] * size_per_elem_bytes)
                        return resharding_cost
                    else:
                        # This raise is used to check if we have missed any type of data.
                        # It could be merged into Parameter branch, which means we won't handle
                        # non-tensor arguments.
                        raise ValueError(f'Unsupported data type {type(data)}')
                else:
                    assert isinstance(prev_sharding_spec, (tuple, list)), \
                        f'prev_sharding_spec should be in type of ShardingSpec, List[ShardingSpec], \
                            or Tuple[ShardingSpec], but got {type(prev_sharding_spec)}'

                    fwd_cost = 0
                    bwd_cost = 0
                    total_cost = 0
                    for index, (prev_sharding_spec_item,
                                current_sharding_spec_item) in enumerate(zip(prev_sharding_spec,
                                                                             current_sharding_spec)):
                        item_cost = _compute_resharding_cost(prev_sharding_spec_item, current_sharding_spec_item,
                                                             data[index])
                        fwd_cost += item_cost.fwd
                        bwd_cost += item_cost.bwd
                        total_cost += item_cost.total
                    resharding_cost = TrainCycleItem(fwd=fwd_cost, bwd=bwd_cost, total=total_cost)
                    return resharding_cost

            # for each sharding spec generated by the predecessor's node handler
            # compute the resharding cost to switch to the sharding spec generated
            # by the current node handler
            for prev_sharding_spec in prev_sharding_specs:
                resharding_cost = _compute_resharding_cost(prev_sharding_spec, current_sharding_spec, op_data.data)
                resharding_costs[node].append(resharding_cost)
        strategy.resharding_costs = resharding_costs
        return strategy

    def get_target_function(self) -> callable:
        """
        This function is used to get the target function for the node handler.
        The target function is used to analyze the costs of strategies.
        """
        if self.node.op in ('placeholder', 'get_attr', 'output'):
            return None

        if self.node.op == 'call_module':
            target = self.node.graph.owning_module.get_submodule(self.node.target)
        elif self.node.op == 'call_function':
            target = self.node.target
        elif self.node.op == 'call_method':
            target = getattr(self.node.args[0]._meta_data.__class__, self.node.target)
        else:
            raise ValueError(f'Unsupported node type: {self.node.op}')

        return target

    def register_strategy(self, compute_resharding_cost: bool = True) -> StrategiesVector:
        """
        Register different sharding strategies for the current node.
        """
        strategy_generators = self.get_strategy_generator()
        strategies_info = []
        for generator in strategy_generators:
            strategies = generator.generate()

            for strategy in strategies:
                shard_metainfo = ShardMetaInfo()
                shard_metainfo.compute_cost = strategy.compute_cost
                shard_metainfo.memory_cost = strategy.memory_cost
                shard_metainfo.fwd_in = []
                if isinstance(self.node._meta_data, torch.Tensor):
                    shard_metainfo.fwd_out = [self.node._meta_data]
                else:
                    shard_metainfo.fwd_out = self.node._meta_data
                shard_metainfo.fwd_buffer = []
                strategies_info.append(shard_metainfo)

            # postprocess a strategy
            # postprocess can produce one strategy or multiple strategies
            post_processed_strategies_map = map(self.post_process, strategies)
            post_processed_strategies = []

            for strategy in post_processed_strategies_map:
                if isinstance(strategy, (list, tuple)):
                    post_processed_strategies.extend(strategy)
                else:
                    post_processed_strategies.append(strategy)

            # compute the resharding costs based on the previous node
            # strategies if specified
            if compute_resharding_cost:
                updated_strategies = map(self.update_resharding_cost, post_processed_strategies)
                post_processed_strategies = list(updated_strategies)

            self.strategies_vector.extend(post_processed_strategies)
        setattr(self, "strategies_info", strategies_info)

        # validating the correctness of the sharding strategy
        for strategy in self.strategies_vector:
            for op_data, sharding_spec in strategy.sharding_specs.items():
                if op_data.data is not None and isinstance(op_data.data, torch.Tensor):
                    check_sharding_spec_validity(sharding_spec, op_data.data)

        remove_strategy_list = []
        for strategy in self.strategies_vector:
            shard_axis_list = []
            last_axis = len(self.device_mesh.mesh_shape) - 1
            for op_data, sharding_spec in strategy.sharding_specs.items():
                if op_data.data is not None and isinstance(op_data.data, torch.Tensor):
                    for dim, shard_axes in sharding_spec.dim_partition_dict.items():
                        for shard_axis in shard_axes:
                            if shard_axis not in shard_axis_list:
                                shard_axis_list.append(shard_axis)

            shard_level = len(shard_axis_list)
            using_last_axis = last_axis in shard_axis_list or -1 in shard_axis_list
            if self.shard_option == ShardOption.SHARD and shard_level == 0:
                remove_strategy_list.append(strategy)
            if self.shard_option == ShardOption.FULL_SHARD and shard_level <= 1:
                remove_strategy_list.append(strategy)
            if self.shard_option == ShardOption.SHARD_LAST_AXIS:
                if shard_level != 1 or using_last_axis == False:
                    remove_strategy_list.append(strategy)

        for strategy in remove_strategy_list:
            self.strategies_vector.remove(strategy)

        return self.strategies_vector

    def post_process(self, strategy: ShardingStrategy) -> Union[ShardingStrategy, List[ShardingStrategy]]:
        # tranform the strategy generated
        # e.g. to process the sharding strategy for the transposed weights
        return strategy

    @abstractmethod
    def get_strategy_generator(self) -> List[StrategyGenerator]:
        """
        Define which generators should be used by this NodeHandler object.
        """
        pass

    @abstractmethod
    def get_operation_data_mapping(self) -> Dict[str, OperationData]:
        """
        Returns the mapping between the logical operation data to its physical data.
        A logical operation data is a data associated with an operation, which can be input and output. It is
        defined by the strategy generator, for example, a matrix multiplication operation has two operands "input"
        and "other" and one result "output". For a nn.Linear module, the physical operand for "input" is
        the module input, the physical operand for "other" is the module weight, and the physical result for "output"
        is the module output.
        Note that the operand name is specified by the StrategyGenerator object.

        For example:

            # for a linear layer
            mapping = {
                "input": Operand(name=str(self.node.args[0]), type=OperationDataType.ARG, data=self.node.args[0]._meta_data),
                "other": Operand(name="weight", type=OperationDataType.PARAM, data=self.named_parameters['weight']),
                "bias": Operand(name="bias", type=OperationDataType.PARAM, data=self.named_parameters['bias']),
                "output": Operand(name=str(self.node), type=OperationDataType.OUTPUT, data=self.node._meta_data),
            }
        """
        pass


class MetaInfoNodeHandler(NodeHandler):
    """
    This is a base class to handle the nodes patched in the meta profiler.

    Note: this class will be integrated into the NodeHandler class in the future, after
    all the functions are patched.
    """

    def register_strategy(self, compute_resharding_cost: bool = True) -> StrategiesVector:
        """
        This method is inherited from NodeHandler. It will register the strategies first,
        and rewrite the memory_cost and compute_cost of the strategy using the ShardMetaInfo class.
        """
        super().register_strategy(compute_resharding_cost=compute_resharding_cost)
        target = self.get_target_function()
        # Currently we haven't patched all the torch functions and modules, so if the target
        # is not patched, we will use the default cost model to compute the cost.
        # TODO: patch all torch functions and modules to make it clean
        if meta_register.has(target.__class__) or meta_register.has(target):
            strategies_info = []
            for strategy in self.strategies_vector:
                metainfo = ShardMetaInfo(strategy, target)
                strategy.compute_cost = metainfo.compute_cost
                strategy.memory_cost = metainfo.memory_cost
                strategies_info.append(metainfo)

            # attach metainfos to the handler
            setattr(self, "strategies_info", strategies_info)

        else:
            logger = get_dist_logger()
            logger.warning(f'The target function {target} is not patched yet, ')

        return self.strategies_vector


class ModuleHandler(NodeHandler):

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        # set attributes to access module parameters for convenience
        assert self.node.graph.owning_module is not None, \
            f'The graph is not associated with a module, please make sure it can be used to instantiate a GraphModule object.'
        module = self.node.graph.owning_module.get_submodule(self.node.target)
        named_parameters = list(module.named_parameters(recurse=False))
        named_buffers = list(module.named_buffers(recurse=False))
        # convert named parameters from list to dict
        named_parameters = {k: v for k, v in named_parameters}
        named_buffers = {k: v for k, v in named_buffers}
        self.module = module
        self.named_parameters = named_parameters
        self.named_buffers = named_buffers


class MetaInfoModuleHandler(ModuleHandler):
    """
    This is a base class to handle the module patched in the meta profiler.

    Note: this class will be integrated into the ModuleHandler class in the future, after
    all the modules are patched.
    """

    def register_strategy(self, compute_resharding_cost: bool = True) -> StrategiesVector:
        """
        This method is inherited from NodeHandler. It will register the strategies first,
        and rewrite the memory_cost and compute_cost of the strategy using the ShardMetaInfo class.
        """
        super().register_strategy(compute_resharding_cost=compute_resharding_cost)
        target = self.get_target_function()
        # Currently we haven't patched all the torch functions and modules, so if the target
        # is not patched, we will use the default cost model to compute the cost.
        # TODO: patch all torch functions and modules to make it clean
        if meta_register.has(target.__class__) or meta_register.has(target):
            strategies_info = []
            for strategy in self.strategies_vector:
                metainfo = ShardMetaInfo(strategy, target)
                strategy.compute_cost = metainfo.compute_cost
                strategy.memory_cost = metainfo.memory_cost
                strategies_info.append(metainfo)

            # attach metainfos to the handler
            setattr(self, "strategies_info", strategies_info)

        else:
            logger = get_dist_logger()
            logger.warning(f'The target function {target} is not patched yet')

        return self.strategies_vector
