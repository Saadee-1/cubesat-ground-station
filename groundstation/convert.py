"""Safe equation compiler: turns text like "RAW * 0.01" into a function.

We never call eval()/exec(). The text is parsed into a syntax tree, the tree is
checked against a small whitelist, and then we evaluate the tree ourselves.
"""
import ast
import operator
from typing import Callable

from .errors import ConfigError

# The only binary operators allowed. "**" is left out on purpose: 9**9**9
# would make the program compute a gigantic number and hang.
_BINARY_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
# The only unary operators allowed (+x and -x).
_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_VARIABLE = "RAW"  # the single name an equation may use


def _validate(node: ast.AST) -> None:
    """Raise ConfigError unless every node in the tree is whitelisted."""
    if isinstance(node, ast.Expression):
        _validate(node.body)
    elif isinstance(node, ast.Constant):
        # type() (not isinstance) so that True/False (bool), strings, complex
        # numbers, None, etc. are all rejected; only plain int/float pass.
        if type(node.value) not in (int, float):
            raise ConfigError(f"constant {node.value!r} is not allowed")
    elif isinstance(node, ast.Name):
        if node.id != _VARIABLE:
            raise ConfigError(f"unknown name {node.id!r}; only {_VARIABLE} is allowed")
    elif isinstance(node, ast.BinOp):
        if type(node.op) not in _BINARY_OPS:
            raise ConfigError(f"operator {type(node.op).__name__} is not allowed")
        _validate(node.left)
        _validate(node.right)
    elif isinstance(node, ast.UnaryOp):
        if type(node.op) not in _UNARY_OPS:
            raise ConfigError(f"operator {type(node.op).__name__} is not allowed")
        _validate(node.operand)
    else:
        # Calls, attributes, subscripts, lambdas, if-expressions, ...
        raise ConfigError(f"{type(node).__name__} is not allowed in an equation")


def _evaluate(node: ast.AST, raw: float) -> float:
    """Walk a (already validated) tree and compute its value for this RAW."""
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, raw)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return raw  # validation guarantees the name is RAW
    if isinstance(node, ast.BinOp):
        # May raise ZeroDivisionError; the caller (decoder) handles that.
        return _BINARY_OPS[type(node.op)](_evaluate(node.left, raw), _evaluate(node.right, raw))
    if isinstance(node, ast.UnaryOp):
        return _UNARY_OPS[type(node.op)](_evaluate(node.operand, raw))
    raise ConfigError("unexpected node")  # unreachable after validation


def compile_equation(expr: str) -> Callable[[float], float]:
    """Parse and validate `expr` once; return a function raw -> engineering value."""
    if not isinstance(expr, str):
        raise ConfigError("equation must be a string")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
        _validate(tree)
    except SyntaxError as exc:
        raise ConfigError(f"syntax error in equation {expr!r}: {exc.msg}") from exc
    except (ValueError, RecursionError, MemoryError) as exc:
        # ValueError covers e.g. null bytes (and ConfigError, which subclasses it,
        # is re-raised untouched by the clause below).
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"equation {expr!r} cannot be parsed: {exc}") from exc

    def convert(raw: float) -> float:
        """Apply the compiled equation to one raw value."""
        return _evaluate(tree, raw)

    return convert
