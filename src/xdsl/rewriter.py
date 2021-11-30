import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Callable, Dict

from xdsl.dialects.builtin import ModuleOp
from xdsl.ir import Operation, SSAValue, Region, Block


@dataclass(init=False)
class RewriteAction:
    """
    Action that a single rewrite may execute.
    A rewrite always delete the matched operation, and replace it with new operations.
    The matched operation results are replaced with new ones.
    """

    new_ops: List[Operation]
    """New operations that replace the one matched."""

    new_results: List[SSAValue]
    """SSA values that replace the matched operation results."""
    def __init__(self, new_ops: List[Operation],
                 new_results: Optional[List[SSAValue]]):
        self.new_ops = new_ops
        if new_results is not None:
            self.new_results = new_results
            return

        if len(new_ops) == 0:
            self.new_results = []
        else:
            self.new_results = new_ops[-1].results


class RewritePattern(ABC):
    """
    A side-effect free rewrite pattern matching on a DAG.
    """
    @abstractmethod
    def match_and_rewrite(
            self, op: Operation,
            new_operands: List[SSAValue]) -> Optional[RewriteAction]:
        """
        Match an operation, and optionally returns a rewrite to be performed.
        `op` is the operation to match, and `new_operands` are the potential new values of the operands.
        This function returns `None` if the pattern did not matched, and a rewrite action otherwise.
        """
        ...


@dataclass(eq=False, repr=False)
class AnonymousRewritePattern(RewritePattern):
    """
    A rewrite pattern encoded by an anonymous function.
    """
    func: Callable[[Operation, List[SSAValue]], Optional[RewriteAction]]

    def match_and_rewrite(
            self, op: Operation,
            new_operands: List[SSAValue]) -> Optional[RewriteAction]:
        return self.func(op, new_operands)


def op_type_rewrite_pattern(func):
    """
    This function is intended to be used as a decorator on a RewritePatter method.
    It uses type hints to match on a specific operation type before calling the decorated function.
    """
    # Get the operation argument and check that it is a subclass of Operation
    params = [param for param in inspect.signature(func).parameters.values()]
    if len(params) == 3:
        has_self = True
        expected_type = params[1].annotation
    else:
        has_self = False
        expected_type = params[0].annotation
    if not issubclass(expected_type, Operation):
        raise Exception("op_type_rewrite_pattern expects the first non-self "
                        "operand type hint to be an Operation subclass")

    if not has_self:

        def op_type_rewrite_pattern_wrapper(
                op: Operation,
                operands: List[SSAValue]) -> Optional[RewriteAction]:
            if not isinstance(op, expected_type):
                return None
            return func(op, operands)

        return op_type_rewrite_pattern_wrapper

    def op_type_rewrite_pattern_wrapper(
            self, op: Operation,
            operands: List[SSAValue]) -> Optional[RewriteAction]:
        if not isinstance(op, expected_type):
            return None
        return func(self, op, operands)

    return op_type_rewrite_pattern_wrapper


@dataclass(repr=False, eq=False)
class OperandUpdater:
    """
    Provides functionality to bookkeep changed results and to access and update them.
    """

    result_mapping: Dict[SSAValue, SSAValue] = field(default_factory=dict())

    def bookkeep_results(self, old_op: Operation,
                         action: RewriteAction) -> None:
        """Bookkeep the changes made by a rewrite action matching on `old_op`."""
        if len(old_op.results) == 0:
            return

        assert len(old_op.results) == len(action.new_results)

        for (old_res, new_res) in zip(old_op.results, action.new_results):
            self.result_mapping[old_res] = new_res

    def get_new_value(self, value: SSAValue) -> SSAValue:
        """Get the updated value, if it exists, or returns the same one."""
        return self.result_mapping.get(value, value)

    def get_new_operands(self, op: Operation) -> [SSAValue]:
        """Get the new operation updated operands"""
        return [self.get_new_value(operand) for operand in op.operands]

    def update_operands(self, op: Operation) -> None:
        """Update an operation operands with the new operands."""
        op.operands = self.get_new_operands(op)


@dataclass(eq=False, repr=False)
class PatternRewriteWalker:
    """
    Walks the IR in the block and instruction order.
    Can walk either first the regions, or first the owner operation.
    """

    pattern: RewritePattern
    """Pattern to apply during the walk."""

    walk_regions_first: bool = field(default=False)
    """Choose if the walker should first walk the operation regions first, or the operation itself."""

    _updater: OperandUpdater = field(init=False,
                                     default_factory=OperandUpdater)
    """Takes care of bookkeeping the changes made during the walk."""
    def rewrite_module(self, op: ModuleOp) -> ModuleOp:
        """Rewrite an entire module operation."""
        new_ops = self.rewrite_op(op)
        if len(new_ops) == 1:
            res_op = new_ops[1]
            if isinstance(res_op, ModuleOp):
                return res_op
        raise Exception(
            "Rewrite pattern did not rewrite a module into another module.")

    def rewrite_op(self, op: Operation) -> List[Operation]:
        """Rewrite an operation, along with its regions."""
        # First, we walk the regions if needed
        if self.walk_regions_first:
            self.rewrite_op_regions(op)

        # We then match for a pattern in the current operation
        action = self.pattern.match_and_rewrite(
            op, self._updater.get_new_operands(op))

        # If we produce new operations, we rewrite them recursively until convergence
        if action is not None:
            self._updater.bookkeep_results(op, action)
            new_ops = []
            for new_op in action.new_ops:
                new_ops.extend(self.rewrite_op(new_op))
            return new_ops

        # Otherwise, we update their operands, and walk recursively their regions if needed
        self._updater.update_operands(op)
        if not self.walk_regions_first:
            self.rewrite_op_regions(op)
        return [op]

    def rewrite_op_regions(self, op: Operation):
        """Rewrite the regions of an operation, and update the operation with the new regions."""
        new_regions = []
        for region in op.regions:
            new_region = Region()
            for block in region.blocks:
                new_block = Block()
                for sub_op in block.ops:
                    new_block.add_ops(self.rewrite_op(sub_op))
                new_region.add_block(new_block)
            new_regions.append(new_region)
        op.regions = []
        for region in new_regions:
            op.add_region(region)