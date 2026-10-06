"""Functions the model can ask the server to run.

The model chooses the function and the arguments. This module runs only the
two built-in functions. Unknown names and invalid math return an error string
the model can read. They do not raise.
"""

import ast
import json
import math
import operator
from dataclasses import dataclass
from datetime import datetime, timezone

MAX_MODEL_ROUNDS = 4
_MAX_EXPRESSION_LENGTH = 100
_MAX_RESULT_LENGTH = 200
_MAX_NUMBER = 1_000_000_000_000

_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARY = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_DEFINITIONS: tuple[dict[str, str], ...] = (
    {
        "name": "current_time",
        "description": (
            "Return the current UTC date and time. "
            "Use this when the user asks what time or date it is."
        ),
    },
    {
        "name": "calculate",
        "description": (
            "Evaluate one arithmetic expression exactly. "
            "Use this instead of doing the math yourself. "
            "The expression may contain numbers, parentheses, and + - * /."
        ),
    },
)

_PARAMETERS: dict[str, dict[str, object]] = {
    "current_time": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "calculate": {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "Arithmetic expression, for example (12 + 3) * 4.",
            }
        },
        "required": ["expression"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class ToolOutcome:
    name: str
    arguments: dict[str, object]
    result: str
    is_error: bool


def gemini_tools(*, web_search: bool = False) -> list[dict[str, object]]:
    tools = _function_tools()
    if web_search:
        tools.append({"type": "google_search", "search_types": ["web_search"]})
    return tools


def openai_tools(*, web_search: bool = False) -> list[dict[str, object]]:
    tools = [{**tool, "strict": True} for tool in _function_tools()]
    if web_search:
        tools.append({"type": "web_search"})
    return tools


def run_tool(
    name: str,
    arguments: object,
    *,
    now: datetime | None = None,
) -> ToolOutcome:
    """Run one allowed function. The result is short text the model can read."""
    raw = _arguments(arguments)
    public = _public_arguments(raw)
    if name == "current_time":
        moment = now or datetime.now(timezone.utc)
        stamp = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return ToolOutcome(name, public, stamp, False)
    if name == "calculate":
        return _calculate(public)
    return ToolOutcome(name or "unknown", public, "That tool is not available.", True)


def _function_tools() -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "name": item["name"],
            "description": item["description"],
            "parameters": _PARAMETERS[item["name"]],
        }
        for item in _DEFINITIONS
    ]


def _arguments(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _public_arguments(arguments: dict[str, object]) -> dict[str, object]:
    cleaned: dict[str, object] = {}
    for key, value in list(arguments.items())[:8]:
        if not isinstance(key, str) or not key:
            continue
        label = key[:40]
        if isinstance(value, str):
            cleaned[label] = value[:_MAX_EXPRESSION_LENGTH]
        elif isinstance(value, bool):
            cleaned[label] = value
        elif isinstance(value, int):
            cleaned[label] = value
        elif isinstance(value, float) and math.isfinite(value):
            cleaned[label] = value
    return cleaned


def _calculate(arguments: dict[str, object]) -> ToolOutcome:
    expression = arguments.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        return ToolOutcome("calculate", arguments, "The arguments were not valid.", True)
    if len(expression) > _MAX_EXPRESSION_LENGTH:
        return ToolOutcome(
            "calculate",
            arguments,
            "The expression could not be calculated.",
            True,
        )
    try:
        value = _eval_math(ast.parse(expression, mode="eval"))
        rendered = _format_number(value)
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError):
        return ToolOutcome(
            "calculate",
            arguments,
            "The expression could not be calculated.",
            True,
        )
    return ToolOutcome("calculate", arguments, rendered[:_MAX_RESULT_LENGTH], False)


def _eval_math(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_math(node.body)
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return float(_UNARY[type(node.op)](_eval_math(node.operand)))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        return float(_BINARY[type(node.op)](_eval_math(node.left), _eval_math(node.right)))
    raise ValueError


def _format_number(value: float) -> str:
    if not math.isfinite(value) or abs(value) > _MAX_NUMBER:
        raise ValueError
    if value.is_integer():
        return str(int(value))
    return format(value, ".12g")
