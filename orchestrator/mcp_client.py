"""MCP (Model Context Protocol) client manager for Zero Agent.

Connects to any MCP server (stdio subprocess or SSE/HTTP endpoint) and
exposes its tools to Zero's agent loop. 100% local — the MCP servers
themselves run on this machine; no cloud required.

Each connection runs as a persistent background asyncio task so the
underlying transport (subprocess/SSE) stays alive across tool calls.
Connections are cached by name; calling connect() twice with the same
name is a no-op (returns the cached entry).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("zero_agent.mcp_client")

# ---------------------------------------------------------------------------
# Lazy MCP import — graceful degradation if the user hasn't installed `mcp`
# ---------------------------------------------------------------------------
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    try:
        from mcp.client.sse import sse_client
    except ImportError:
        sse_client = None  # type: ignore[assignment]
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False
    ClientSession = None  # type: ignore[assignment, misc]
    StdioServerParameters = None  # type: ignore[assignment, misc]
    stdio_client = None  # type: ignore[assignment]
    sse_client = None  # type: ignore[assignment]


@dataclass
class MCPTool:
    """One tool exposed by an MCP server."""
    server: str
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        return f"  [{self.server}] {self.name} — {self.description}"


@dataclass
class _Connection:
    name: str
    kind: str                          # "stdio" | "sse"
    tools: list[MCPTool]
    _session: Any                      # ClientSession (typed as Any to avoid import issues)
    _task: asyncio.Task | None = None


# Process-wide registry: name → connection
_connections: dict[str, _Connection] = {}
_ready_events: dict[str, asyncio.Event] = {}


def _mcp_unavailable() -> str:
    return "MCP is not available: `pip install mcp` to enable."


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

async def connect_stdio(
    name: str,
    command: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> str:
    """Start an MCP stdio server subprocess and connect to it.

    Args:
        name: Friendly label for this server (used in subsequent calls).
        command: Executable to run (e.g. "python", "node", "uvx").
        args: Arguments for the command (e.g. ["server.py"]).
        env: Extra env vars forwarded to the subprocess.
        timeout: Seconds to wait for the server to be ready.

    Returns a human-readable status string.
    """
    if not _MCP_AVAILABLE:
        return _mcp_unavailable()
    if name in _connections:
        c = _connections[name]
        return f"Already connected to '{name}' (stdio, {len(c.tools)} tools)."

    ready = asyncio.Event()
    _ready_events[name] = ready
    error_holder: list[str] = []

    async def _run() -> None:
        params = StdioServerParameters(
            command=command,
            args=args or [],
            env=env,
        )
        try:
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    raw = await session.list_tools()
                    tools = [
                        MCPTool(
                            server=name,
                            name=t.name,
                            description=t.description or "",
                            input_schema=t.inputSchema if hasattr(t, "inputSchema") else {},
                        )
                        for t in (raw.tools if hasattr(raw, "tools") else [])
                    ]
                    _connections[name] = _Connection(
                        name=name, kind="stdio", tools=tools, _session=session,
                    )
                    ready.set()
                    logger.info("mcp: connected stdio '%s' (%d tools)", name, len(tools))
                    # Hold the connection open until this task is cancelled.
                    await asyncio.get_event_loop().create_future()
        except asyncio.CancelledError:
            logger.info("mcp: stdio '%s' disconnected", name)
        except Exception as exc:  # noqa: BLE001
            error_holder.append(str(exc))
            logger.warning("mcp: stdio '%s' failed: %s", name, exc)
            ready.set()  # unblock the waiter so it sees the error

    task = asyncio.create_task(_run(), name=f"mcp_stdio_{name}")
    if name in _connections:
        _connections[name]._task = task

    try:
        await asyncio.wait_for(ready.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        task.cancel()
        return f"Error: MCP stdio server '{name}' did not respond within {timeout}s."

    if error_holder:
        return f"Error connecting to '{name}': {error_holder[0]}"

    conn = _connections.get(name)
    if conn is None:
        return f"Error: connection to '{name}' failed silently."
    conn._task = task
    return (
        f"Connected to MCP server '{name}' (stdio: {command} {' '.join(args or [])}).\n"
        f"{len(conn.tools)} tools available: "
        + ", ".join(t.name for t in conn.tools[:10])
        + ("…" if len(conn.tools) > 10 else "")
    )


async def connect_sse(
    name: str,
    url: str,
    timeout: float = 15.0,
) -> str:
    """Connect to an MCP server over SSE/HTTP.

    Args:
        name: Friendly label.
        url: SSE endpoint URL, e.g. "http://localhost:8000/sse".
        timeout: Seconds to wait for the server to be ready.
    """
    if not _MCP_AVAILABLE:
        return _mcp_unavailable()
    if sse_client is None:
        return "SSE transport is not available in this mcp version."
    if name in _connections:
        c = _connections[name]
        return f"Already connected to '{name}' (SSE, {len(c.tools)} tools)."

    ready = asyncio.Event()
    _ready_events[name] = ready
    error_holder: list[str] = []

    async def _run() -> None:
        try:
            async with sse_client(url) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    raw = await session.list_tools()
                    tools = [
                        MCPTool(
                            server=name,
                            name=t.name,
                            description=t.description or "",
                            input_schema=t.inputSchema if hasattr(t, "inputSchema") else {},
                        )
                        for t in (raw.tools if hasattr(raw, "tools") else [])
                    ]
                    _connections[name] = _Connection(
                        name=name, kind="sse", tools=tools, _session=session,
                    )
                    ready.set()
                    logger.info("mcp: connected SSE '%s' (%d tools)", name, len(tools))
                    await asyncio.get_event_loop().create_future()
        except asyncio.CancelledError:
            logger.info("mcp: SSE '%s' disconnected", name)
        except Exception as exc:  # noqa: BLE001
            error_holder.append(str(exc))
            logger.warning("mcp: SSE '%s' failed: %s", name, exc)
            ready.set()

    task = asyncio.create_task(_run(), name=f"mcp_sse_{name}")

    try:
        await asyncio.wait_for(ready.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        task.cancel()
        return f"Error: MCP SSE server '{name}' did not respond within {timeout}s."

    if error_holder:
        return f"Error connecting to '{name}': {error_holder[0]}"

    conn = _connections.get(name)
    if conn is None:
        return f"Error: connection to '{name}' failed silently."
    conn._task = task
    return (
        f"Connected to MCP server '{name}' (SSE: {url}).\n"
        f"{len(conn.tools)} tools available: "
        + ", ".join(t.name for t in conn.tools[:10])
        + ("…" if len(conn.tools) > 10 else "")
    )


async def disconnect(name: str) -> str:
    """Cancel and remove an MCP connection."""
    conn = _connections.pop(name, None)
    if conn is None:
        return f"No connection named '{name}'."
    if conn._task and not conn._task.done():
        conn._task.cancel()
        try:
            await conn._task
        except (asyncio.CancelledError, Exception):
            pass
    _ready_events.pop(name, None)
    return f"Disconnected from '{name}'."


def list_connections() -> list[_Connection]:
    return list(_connections.values())


def get_all_tools() -> list[MCPTool]:
    """All tools from all connected servers, flat list."""
    tools: list[MCPTool] = []
    for conn in _connections.values():
        tools.extend(conn.tools)
    return tools


async def call_tool(
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any] | str,
) -> str:
    """Call a tool on a connected MCP server and return the result as text."""
    if not _MCP_AVAILABLE:
        return _mcp_unavailable()

    conn = _connections.get(server_name)
    if conn is None:
        names = ", ".join(_connections) or "none"
        return f"Error: no MCP server named '{server_name}'. Connected: {names}"

    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return f"Error: arguments must be valid JSON: {exc}"

    try:
        result = await conn._session.call_tool(tool_name, arguments)
        # result.content is a list of TextContent / ImageContent / etc.
        parts: list[str] = []
        for item in result.content if hasattr(result, "content") else []:
            if hasattr(item, "text"):
                parts.append(item.text)
            else:
                parts.append(str(item))
        return "\n".join(parts) if parts else "(empty result)"
    except Exception as exc:  # noqa: BLE001
        return f"Error calling '{tool_name}' on '{server_name}': {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Auto-connect from config
# ---------------------------------------------------------------------------

async def autoconnect_from_config() -> list[str]:
    """Connect to MCP servers listed in config.MCP_SERVERS at startup.

    Each entry in MCP_SERVERS is a dict:
      {"name": "github", "type": "stdio", "command": "uvx", "args": ["mcp-server-git"]}
      {"name": "myapi",  "type": "sse",   "url": "http://localhost:8000/sse"}
    """
    from orchestrator import config
    servers = getattr(config, "MCP_SERVERS", [])
    if not servers:
        return []
    results: list[str] = []
    for srv in servers:
        try:
            name = srv.get("name", "unnamed")
            kind = srv.get("type", "stdio").lower()
            if kind == "stdio":
                r = await connect_stdio(
                    name=name,
                    command=srv.get("command", "python"),
                    args=srv.get("args", []),
                    env=srv.get("env"),
                )
            elif kind == "sse":
                r = await connect_sse(name=name, url=srv.get("url", ""))
            else:
                r = f"Unknown MCP server type '{kind}' for '{name}'."
            results.append(r)
        except Exception as exc:  # noqa: BLE001
            results.append(f"Error auto-connecting '{srv.get('name')}': {exc}")
    return results
