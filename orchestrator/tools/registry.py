"""Decorator-based tool registry with automatic Pydantic v2 schema generation.

Decorate any plain Python function with ``@registry.register`` and the registry
introspects its signature + type hints + docstring to build:

  * a strict Pydantic v2 model used to *validate* incoming arguments, and
  * an OpenAI/Ollama-compatible JSON ``tools`` schema used to *advertise* the
    tool to the model.

Both sync and async tool functions are supported. Sync tools are executed in a
worker thread so they never block the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from typing import Any, Callable, get_type_hints

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    create_model,
)

# Google-style "Args:" docstring section, e.g.
#     filepath: Absolute path to the file.
_ARG_LINE = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?:\([^)]*\))?\s*:\s*(.+)$")


def _split_docstring(doc: str) -> tuple[str, dict[str, str]]:
    """Return (summary, {param_name: description}) from a Google-style docstring."""
    if not doc:
        return "", {}

    lines = doc.strip().splitlines()
    summary_lines: list[str] = []
    param_docs: dict[str, str] = {}
    in_args = False

    for line in lines:
        stripped = line.strip()
        if stripped.lower() in {"args:", "arguments:", "parameters:"}:
            in_args = True
            continue
        if in_args and stripped.lower() in {"returns:", "raises:", "yields:", "examples:"}:
            in_args = False
            continue
        if in_args:
            match = _ARG_LINE.match(line)
            if match:
                param_docs[match.group(1)] = match.group(2).strip()
        elif not param_docs:
            summary_lines.append(stripped)

    summary = " ".join(s for s in summary_lines if s).strip()
    return summary, param_docs


class Tool:
    """A registered tool: callable + validation model + advertised schema."""

    def __init__(
        self,
        func: Callable[..., Any],
        name: str,
        description: str,
        args_model: type[BaseModel],
    ) -> None:
        self.func = func
        self.name = name
        self.description = description
        self.args_model = args_model

    def schema(self) -> dict[str, Any]:
        """OpenAI/Ollama tool schema for this tool."""
        params = self.args_model.model_json_schema()
        params.pop("title", None)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": params,
            },
        }

    async def invoke(self, arguments: dict[str, Any] | str) -> Any:
        """Validate ``arguments`` then run the tool (off-loop if sync)."""
        if isinstance(arguments, str):
            arguments = json.loads(arguments) if arguments.strip() else {}
        validated = self.args_model.model_validate(arguments)
        kwargs = validated.model_dump()

        if inspect.iscoroutinefunction(self.func):
            return await self.func(**kwargs)
        return await asyncio.to_thread(self.func, **kwargs)


def _build_tool(
    func: Callable[..., Any],
    name: str | None,
    description: str | None,
) -> Tool:
    sig = inspect.signature(func)
    hints = get_type_hints(func)
    summary, param_docs = _split_docstring(inspect.getdoc(func) or "")

    fields: dict[str, Any] = {}
    for pname, param in sig.parameters.items():
        if pname in {"self", "cls"}:
            continue
        if param.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
            continue

        annotation = hints.get(pname, str)
        field = Field(
            default=... if param.default is inspect.Parameter.empty else param.default,
            description=param_docs.get(pname, ""),
        )
        fields[pname] = (annotation, field)

    # extra="forbid" => reject hallucinated arguments. strict types where given.
    args_model = create_model(  # type: ignore[call-overload]
        f"{func.__name__}_Args",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )

    return Tool(
        func=func,
        name=name or func.__name__,
        description=description or summary or func.__name__,
        args_model=args_model,
    )


class ToolRegistry:
    """Holds registered tools and exposes them to the model + the agent."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(
        self,
        func: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Callable[..., Any]:
        """Decorator. Use bare ``@registry.register`` or with kwargs."""

        def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
            tool = _build_tool(f, name, description)
            if tool.name in self._tools:
                raise ValueError(f"Tool '{tool.name}' is already registered.")
            self._tools[tool.name] = tool
            return f

        return decorator(func) if func is not None else decorator

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        """All tool schemas, ready to pass to OllamaClient.chat(tools=...)."""
        return [tool.schema() for tool in self._tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any] | str) -> str:
        """Run a tool by name, returning a string safe to feed back to the model.

        Errors are returned as text (never raised) so the agent can recover and
        try a different approach instead of crashing the loop.
        """
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: unknown tool '{name}'. Available: {', '.join(self.names())}"
        try:
            result = await tool.invoke(arguments)
        except ValidationError as exc:
            return f"Error: invalid arguments for '{name}': {exc}"
        except Exception as exc:  # noqa: BLE001 - surface any tool failure to the model
            return f"Error while running '{name}': {type(exc).__name__}: {exc}"
        return result if isinstance(result, str) else json.dumps(result, default=str)


# Process-wide default registry. Tool modules import this and decorate against it.
registry = ToolRegistry()
