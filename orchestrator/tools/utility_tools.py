"""Deterministic utility tools — facts the model must never guess.

Two small, dependency-free tools that close common hallucination gaps:

* :func:`get_current_datetime` — the real local date/time. Models routinely
  invent "today's" date (which silently corrupts any schedule/age/deadline
  answer), so they must read it from here instead of from training memory.
* :func:`calculate` — a SAFE arithmetic evaluator (AST-based, no ``eval``).
  Local models are unreliable at multi-step arithmetic; this returns the exact
  number so the model never has to do mental math on figures that must be right
  (prices, totals, probabilities, unit conversions).

Both are pure/deterministic and run instantly, so they are cheap to call often.
"""

from __future__ import annotations

import ast
import operator
from datetime import datetime, timezone

from orchestrator.tools.registry import registry


@registry.register
def get_current_datetime(tz: str = "local") -> str:
    """Return the current date and time. Call this for ANY question about
    "today", "now", the current date/time, the day of week, or how far away a
    date is — never answer those from memory.

    Args:
        tz: "local" for the machine's local time (default), or "utc" for UTC.
    """
    if (tz or "local").strip().lower() == "utc":
        now = datetime.now(timezone.utc)
        zone = "UTC"
    else:
        now = datetime.now().astimezone()
        # Use the numeric UTC offset (ASCII, e.g. "UTC+03:00") rather than the
        # OS-localized tz NAME, which on a Hebrew Windows returns non-ASCII text
        # ("שעון קיץ ירושלים") that then flows back into the model's context and
        # the chat-history persist — an unnecessary encoding risk.
        off = now.strftime("%z")  # e.g. "+0300"
        zone = f"UTC{off[:3]}:{off[3:]}" if len(off) == 5 else "local"
    return (
        f"{now.strftime('%A, %d %B %Y, %H:%M:%S')} ({zone}) "
        f"[ISO 8601: {now.isoformat()}]"
    )


# --- Safe arithmetic evaluator ----------------------------------------------
# Whitelisted operators only. Anything else (names, calls, attribute access,
# comprehensions, etc.) raises, so the tool can never execute arbitrary code.
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# Cap exponents so `9**9**9` can't hang the process building a huge integer.
_MAX_EXPONENT = 1000


def _eval_node(node: ast.AST) -> float | int:
    """Recursively evaluate a whitelisted arithmetic AST node."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(f"unsupported constant: {node.value!r}")
        return node.value
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"unsupported operator: {type(node.op).__name__}")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_EXPONENT:
            raise ValueError(f"exponent too large (max {_MAX_EXPONENT})")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"unsupported unary operator: {type(node.op).__name__}")
        return op(_eval_node(node.operand))
    raise ValueError(f"unsupported expression element: {type(node).__name__}")


@registry.register
def calculate(expression: str) -> str:
    """Evaluate an arithmetic expression EXACTLY and return the result. Use this
    for any calculation whose number must be correct (prices, totals, percentages,
    probabilities, unit math) instead of doing the arithmetic yourself.

    Supports + - * / // % ** and parentheses on numbers only. It cannot call
    functions or access variables.

    Args:
        expression: A math expression, e.g. "(1250 * 1.17) / 3" or "2**10".
    """
    expr = (expression or "").strip()
    if not expr:
        return "Error: empty expression."
    try:
        tree = ast.parse(expr, mode="eval")
        result = _eval_node(tree.body)
    except ZeroDivisionError:
        return "Error: division by zero."
    except (ValueError, SyntaxError, TypeError) as exc:
        return f"Error: cannot evaluate '{expr}': {exc}"
    # Render whole-valued floats without a trailing .0 for readability.
    if isinstance(result, float) and result.is_integer():
        result = int(result)
    return f"{expr} = {result}"
