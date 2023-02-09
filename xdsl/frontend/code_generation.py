import ast
import xdsl.dialects.builtin as builtin
import xdsl.dialects.func as func
import xdsl.frontend.symref as symref

from dataclasses import dataclass, field
from typing import Any, Dict, List
from xdsl.frontend.exception import CodeGenerationException, FrontendProgramException
from xdsl.frontend.op_inserter import OpInserter
from xdsl.frontend.op_resolver import OpResolver
from xdsl.frontend.type_conversion import TypeConverter
from xdsl.ir import Attribute, Block, Region


@dataclass
class CodeGeneration:

    @staticmethod
    def run_with_type_converter(type_converter: TypeConverter,
                                stmts: List[ast.stmt],
                                file: str) -> builtin.ModuleOp:
        """Generates xDSL code and returns it encapsulated into a single module."""
        module = builtin.ModuleOp.from_region_or_ops([])
        visitor = CodegGenerationVisitor(type_converter, module, file)
        for stmt in stmts:
            visitor.visit(stmt)
        return module


@dataclass
class CodegGenerationVisitor(ast.NodeVisitor):
    """Visitor that generates xDSL from the Python AST."""

    type_converter: TypeConverter = field(init=False)
    """Used for type conversion during code generation."""

    globals: Dict[str, Any] = field(init=False)
    """
    Imports and other global information from the module, useful for looking
    up classes, etc.
    """

    inserter: OpInserter = field(init=False)
    """Used for inserting newly generated operations to the right block."""

    symbol_table: Dict[str, Attribute] | None = field(default=None)
    """
    Maps local variable names to their xDSL types. A single dictionary is sufficient
    because inner functions and global variables are not allowed (yet).
    """

    file: str = field(default=None)
    """Path of the file containing the program being processed."""

    def __init__(self, type_converter: TypeConverter, module: builtin.ModuleOp,
                 file: str) -> None:
        self.type_converter = type_converter
        self.globals = type_converter.globals
        self.file = file

        assert len(module.body.blocks) == 1
        self.inserter = OpInserter(module.body.blocks[0])

    def visit(self, node: ast.AST) -> None:
        super().visit(node)

    def generic_visit(self, node: ast.AST) -> None:
        raise CodeGenerationException(
            self.file, node.lineno, node.col_offset,
            f"Unsupported Python AST node {str(node)}")

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        # TODO: Implement assignemnt in the next patch.
        pass

    def visit_Assign(self, node: ast.Assign) -> None:
        # TODO: Implement assignemnt in the next patch.
        pass

    def visit_BinOp(self, node: ast.BinOp):
        op_name: str = node.op.__class__.__name__

        # Table with mappings of Python AST operator to Python methods.
        python_AST_operator_to_python_overload = {
            "Add": "__add__",
            "Sub": "__sub__",
            "Mult": "__mul__",
            "Div": "__truediv__",
            "FloorDiv": "__floordiv__",
            "Mod": "__mod__",
            "Pow": "__pow__",
            "LShift": "__lshift__",
            "RShift": "__rshift__",
            "BitOr": "__or__",
            "BitXor": "__xor__",
            "BitAnd": "__and__",
            "MatMult": "__matmul__"
        }

        if op_name not in python_AST_operator_to_python_overload:
            raise CodeGenerationException(
                self.file, node.lineno, node.col_offset,
                f"Unexpected binary operation {op_name}.")

        # Check that the types of the operands are the same.
        # This is a (temporary?) restriction over Python for implementation simplicity.
        # This also means that we do not need to support reflected operations
        # (__radd__, __rsub__, etc.) which only exist for operations between different types.
        self.visit(node.right)
        rhs = self.inserter.get_operand()
        self.visit(node.left)
        lhs = self.inserter.get_operand()
        if lhs.typ != rhs.typ:
            raise CodeGenerationException(
                self.file, node.lineno, node.col_offset,
                f"Expected the same types for binary operation '{op_name}', "
                f"but got {lhs.typ} and {rhs.typ}.")

        # Look-up what is the frontend type we deal with to resolve the binary
        # operation.
        frontend_type = self.type_converter.xdsl_to_frontend_type_map[
            lhs.typ.__class__]

        try:
            overload_name = python_AST_operator_to_python_overload[op_name]
            op = OpResolver.resolve_op_overload(overload_name,
                                                frontend_type)(lhs, rhs)
            self.inserter.insert_op(op)
        except FrontendProgramException:
            raise CodeGenerationException(
                self.file, node.lineno, node.col_offset,
                f"Binary operation '{op_name}' "
                f"is not supported by type '{frontend_type.__name__}' "
                f"which does not overload '{overload_name}'.")

    def visit_Compare(self, node: ast.Compare):
        # Allow a single comparison only.
        if len(node.comparators) != 1 or len(node.ops) != 1:
            raise CodeGenerationException(
                self.file, node.lineno, node.col_offset,
                "Expected a single comparator, but found "
                f"{len(node.comparators)}.")
        comp = node.comparators[0]
        op_name: str = node.ops[0].__class__.__name__

        # Table with mappings of Python AST cmpop to Python method.
        python_AST_cmpop_to_python_overload = {
            "Eq": "__eq__",
            "Gt": "__gt__",
            "GtE": "__ge__",
            "Lt": "__lt__",
            "LtE": "__le__",
            "NotEq": "__ne__",
            "In": "__contains__",
            "NotIn": "__contains__"
        }

        # Table with currently unsupported Python AST cmpops.
        # The "is" and "is not" operators are (currently) not supported,
        # since the frontend does not consider/preserve object identity.
        # Finally, "not in" does not directly correspond to a special method
        # and is instead simply implemented as the negation of __contains__
        # which the current mapping framework cannot handle.
        unsupported_python_AST_cmpop = {"Is", "IsNot", "NotIn"}

        if op_name in unsupported_python_AST_cmpop:
            raise CodeGenerationException(
                self.file, node.lineno, node.col_offset,
                f"Unsupported comparison operation '{op_name}'.")

        # Check that the types of the operands are the same.
        # This is a (temporary?) restriction over Python for implementation simplicity.
        # This also means that we do not need to consider swapping arguments
        # (__eq__ and __ne__ are their own reflection, __lt__ <-> __gt__  and __le__ <-> __ge__).
        self.visit(comp)
        rhs = self.inserter.get_operand()
        self.visit(node.left)
        lhs = self.inserter.get_operand()
        if lhs.typ != rhs.typ:
            raise CodeGenerationException(
                self.file, node.lineno, node.col_offset,
                f"Expected the same types for comparison operator '{op_name}',"
                f" but got {lhs.typ} and {rhs.typ}.")

        # Resolve the comparison operation to an xdsl operation class
        python_op = python_AST_cmpop_to_python_overload[op_name]
        frontend_type = self.type_converter.xdsl_to_frontend_type_map[
            lhs.typ.__class__]

        try:
            op = OpResolver.resolve_op_overload(python_op, frontend_type)
        except FrontendProgramException:
            raise CodeGenerationException(
                self.file, node.lineno, node.col_offset,
                f"Comparison operation '{op_name}' "
                f"is not supported by type '{frontend_type.__name__}' "
                f"which does not overload '{python_op}'.")

        # Create the comparison operation (including any potential negations)
        if op_name == "In":
            # "in" does not take a mnemonic.
            op = op(lhs, rhs)
        else:
            # Table with mappings of Python AST cmpop to xDSL mnemonics.
            python_AST_cmpop_to_mnemonic = {
                "Eq": "eq",
                "Gt": "sgt",
                "GtE": "sge",
                "Lt": "slt",
                "LtE": "sle",
                "NotEq": "ne"
            }
            mnemonic = python_AST_cmpop_to_mnemonic[op_name]
            op = op(lhs, rhs, mnemonic)

        self.inserter.insert_op(op)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:

        # Set the symbol table.
        assert self.symbol_table is None
        self.symbol_table = dict()

        # Then, convert types in the function signature.
        argument_types: List[Attribute] = []
        for i, arg in enumerate(node.args.args):
            if arg.annotation is None:
                raise CodeGenerationException(self.file, arg.lineno,
                                              arg.col_offset, f"")
            xdsl_type = self.type_converter.convert_type_hint(arg.annotation)
            argument_types.append(xdsl_type)

        return_types: List[Attribute] = []
        if node.returns is not None:
            xdsl_type = self.type_converter.convert_type_hint(node.returns)
            return_types.append(xdsl_type)

        # Create a function operation.
        entry_block = Block()
        body_region = Region.from_block_list([entry_block])
        func_op = func.FuncOp.from_region(node.name, argument_types,
                                          return_types, body_region)

        self.inserter.insert_op(func_op)
        self.inserter.set_insertion_point_from_block(entry_block)

        # All arguments are declared using symref.
        for i, arg in enumerate(node.args.args):
            symbol_name = str(arg.arg)
            block_arg = entry_block.insert_arg(argument_types[i], i)
            self.symbol_table[symbol_name] = argument_types[i]
            entry_block.add_op(symref.Declare.get(symbol_name))
            entry_block.add_op(symref.Update.get(symbol_name, block_arg))

        # Parse function body.
        for stmt in node.body:
            self.visit(stmt)

        # When function definition is processed, reset the symbol table and set
        # the insertion point.
        self.symbol_table = None
        parent_op = func_op.parent_op()
        assert parent_op is not None
        self.inserter.set_insertion_point_from_op(parent_op)

    def visit_Name(self, node: ast.Name):
        if node.id not in self.symbol_table:
            raise CodeGenerationException(
                self.file, node.lineno, node.col_offset,
                f"Symbol '{node.id}' is not defined.")

        fetch_op = symref.Fetch.get(node.id, self.symbol_table[node.id])
        self.inserter.insert_op(fetch_op)

    def visit_Pass(self, node: ast.Pass) -> None:
        parent_op = self.inserter.insertion_point.parent_op()

        # We might have to add an explicit return statement in this case. Make sure to
        # check the type signature.
        if parent_op is not None and isinstance(parent_op, func.FuncOp):
            return_types = parent_op.function_type.outputs.data

            if len(return_types) != 0:
                function_name = parent_op.attributes["sym_name"].data
                raise CodeGenerationException(
                    self.file, node.lineno, node.col_offset,
                    f"Expected '{function_name}' to return a type.")
            self.inserter.insert_op(func.Return.get())

    def visit_Return(self, node: ast.Return) -> None:
        # First of all, we should only be able to return if the statement is directly
        # in the function. Cases like:
        #
        # def foo(cond: i1):
        #   if cond:
        #     return 1
        #   else:
        #     return 0
        #
        # are not allowed at the moment.
        parent_op = self.inserter.insertion_point.parent_op()
        if not isinstance(parent_op, func.FuncOp):
            raise CodeGenerationException(
                self.file, node.lineno, node.col_offset,
                "Return statement should be placed only at the end of the "
                "function body.")

        func_name = parent_op.attributes["sym_name"].data
        func_return_types = parent_op.function_type.outputs.data

        if node.value is None:
            # Return nothing, check function signature matches.
            if len(func_return_types) != 0:
                raise CodeGenerationException(
                    self.file, node.lineno, node.col_offset,
                    f"Expected non-zero number of return types in function "
                    f"'{func_name}', but got 0.")
            self.inserter.insert_op(func.Return.get())
        else:
            # Return some type, check function signature matches as well.
            # TODO: Support multiple return values if we allow multiple assignemnts.
            self.visit(node.value)
            operands = [self.inserter.get_operand()]

            if len(func_return_types) == 0:
                raise CodeGenerationException(
                    self.file, node.lineno, node.col_offset,
                    f"Expected no return types in function '{func_name}'.")

            for i in range(len(operands)):
                if func_return_types[i] != operands[i].typ:
                    raise CodeGenerationException(
                        self.file, node.lineno, node.col_offset,
                        f"Type signature and the type of the return value do "
                        f"not match at position {i}: expected {func_return_types[i]},"
                        f" got {operands[i].typ}.")

            self.inserter.insert_op(func.Return.get(*operands))