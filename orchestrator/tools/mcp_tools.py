"""MCP tool wrappers — lets Zero Agent talk to any MCP server.

Three tools:
  mcp_connect   — wire up a new MCP server (stdio subprocess or SSE/HTTP)
  mcp_list      — see every tool on every connected server
  mcp_call      — invoke any MCP tool by server + tool name + JSON args

Use mcp_connect once per server; the connection stays alive for the
session. Then call mcp_call as many times as needed.
"""

from __future__ import annotations

import json

from orchestrator import mcp_client
from orchestrator.tools.registry import registry


@registry.register
async def mcp_connect(
    name: str,
    type: str = "stdio",
    command: str = "",
    args: str = "",
    url: str = "",
) -> str:
    """Connect to an MCP server so its tools become available to use with mcp_call.

    For stdio servers (a local subprocess): provide ``command`` (the executable)
    and ``args`` (space-separated arguments, e.g. ``"server.py"``).
    For SSE/HTTP servers: provide ``url`` (e.g. ``"http://localhost:8000/sse"``).

    Popular MCP servers (install with ``uvx`` or ``npx``):
      - GitHub:     type=stdio command=uvx  args="mcp-server-github"
      - Filesystem: type=stdio command=uvx  args="mcp-server-filesystem /some/path"
      - Brave:      type=stdio command=npx  args="-y @modelcontextprotocol/server-brave-search"
      - SQLite:     type=stdio command=uvx  args="mcp-server-sqlite --db-path data/db.sqlite"
      - Puppeteer:  type=stdio command=npx  args="-y @modelcontextprotocol/server-puppeteer"

    Args:
        name:    Short label for this server (used in mcp_call / mcp_list).
        type:    "stdio" (subprocess) or "sse" (HTTP SSE endpoint).
        command: Executable for stdio servers, e.g. "uvx", "npx", "python".
        args:    Space-separated arguments for stdio servers.
        url:     SSE endpoint URL, e.g. "http://localhost:8000/sse".
    """
    kind = type.lower().strip()
    if kind == "sse":
        if not url:
            return "Error: provide 'url' for SSE connections."
        return await mcp_client.connect_sse(name=name, url=url)
    else:
        if not command:
            return "Error: provide 'command' for stdio connections."
        args_list = args.split() if args.strip() else []
        return await mcp_client.connect_stdio(
            name=name, command=command, args=args_list,
        )


@registry.register
async def mcp_list(server: str = "") -> str:
    """List all tools available from connected MCP servers.

    If ``server`` is given, show only that server's tools (with full input
    schema). Otherwise show a compact summary of every connected server.

    Args:
        server: Optional server name to filter. Empty = show all servers.
    """
    connections = mcp_client.list_connections()
    if not connections:
        return (
            "No MCP servers connected yet.\n"
            "Use mcp_connect() to wire one up. Example:\n"
            '  mcp_connect(name="fs", type="stdio", command="uvx", '
            'args="mcp-server-filesystem C:\\\\projects")'
        )

    if server:
        conn = next((c for c in connections if c.name == server), None)
        if conn is None:
            names = ", ".join(c.name for c in connections)
            return f"No server named '{server}'. Connected: {names}"
        lines = [f"## {conn.name} ({conn.kind}, {len(conn.tools)} tools)\n"]
        for t in conn.tools:
            schema_str = ""
            if t.input_schema:
                props = t.input_schema.get("properties", {})
                req = t.input_schema.get("required", [])
                params = []
                for k, v in props.items():
                    mark = "*" if k in req else ""
                    desc = v.get("description", v.get("type", ""))
                    params.append(f"{k}{mark}: {desc}")
                schema_str = "\n    Args: " + "; ".join(params) if params else ""
            lines.append(f"  **{t.name}** — {t.description}{schema_str}")
        return "\n".join(lines)

    # Summary of all servers
    lines = [f"**Connected MCP servers** ({len(connections)}):\n"]
    for conn in connections:
        lines.append(f"### {conn.name}  ({conn.kind}, {len(conn.tools)} tools)")
        for t in conn.tools[:12]:
            lines.append(f"  • `{t.name}` — {t.description}")
        if len(conn.tools) > 12:
            lines.append(f"  … and {len(conn.tools) - 12} more (use mcp_list(server='{conn.name}'))")
        lines.append("")
    lines.append("Call any tool with: mcp_call(server=<name>, tool=<tool_name>, arguments={…})")
    return "\n".join(lines)


@registry.register
async def mcp_call(
    server: str,
    tool: str,
    arguments: str = "{}",
) -> str:
    """Call any tool on a connected MCP server.

    First use mcp_connect() to connect a server, then mcp_list() to see its
    tools, then mcp_call() to invoke one.

    Args:
        server:    Server name (as given to mcp_connect).
        tool:      Tool name (from mcp_list).
        arguments: JSON object of arguments, e.g. '{"path": "C:\\\\projects"}'.
                   Pass "{}" or omit for tools that take no arguments.
    """
    try:
        args_dict = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError as exc:
        return (
            f"Error: 'arguments' must be valid JSON.\n"
            f"Got: {arguments!r}\n"
            f"Parse error: {exc}\n"
            f"Example: arguments='{{\"path\": \"C:\\\\projects\"}}'"
        )
    return await mcp_client.call_tool(
        server_name=server, tool_name=tool, arguments=args_dict,
    )


@registry.register
async def mcp_disconnect(server: str) -> str:
    """Disconnect from an MCP server and free its resources.

    Args:
        server: Server name to disconnect.
    """
    return await mcp_client.disconnect(server)
