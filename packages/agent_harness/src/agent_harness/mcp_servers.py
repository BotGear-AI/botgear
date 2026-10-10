"""MCP servers a workflow may use: declared in WORKFLOW.md, gated by policy.

    mcp:
      robot:                                  # the agent sees mcp__robot__<tool>
        url: "{input.endpoint}"               # placeholders like checks; or a literal
        headers:
          X-API-Key: "{secret.ROBOT_KEY}"     # secrets by reference only, never inline
        tools: [list_capabilities, get_state] # exact tool names, or ["*"] for all
        ca_cert: "{input.ca}"                 # optional PEM to trust (self-signed servers)
        description: the live system to inspect
    secrets: [ROBOT_KEY]
    policy: {mcp: allow}

A run with servers needs policy `mcp: allow` (default deny, like `network`)
and a driver whose capabilities include `mcp`; otherwise it errors before
anything is staged. Secret values reach only the agent's environment (the
driver references them, e.g. `${ROBOT_KEY}`), so they never appear in a
command line, task.md or the event log.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_SECRET_REF = re.compile(r"\{secret\.([A-Za-z_][A-Za-z0-9_]*)\}")


class McpSpecError(ValueError):
    pass


@dataclass
class McpServer:
    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)   # values may hold {secret.NAME}
    tools: list[str] = field(default_factory=list)          # exact names, or ["*"]
    ca_cert: Optional[str] = None
    description: str = ""

    @property
    def all_tools(self) -> bool:
        return "*" in self.tools

    def secret_refs(self) -> set[str]:
        return {m for value in self.headers.values() for m in _SECRET_REF.findall(value)}


def parse_mcp(raw: Any, declared_secrets: list[str], where: str) -> dict[str, McpServer]:
    """Validate a WORKFLOW.md ``mcp:`` mapping."""
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise McpSpecError(f"{where}: mcp must be a mapping of server name -> settings")
    servers: dict[str, McpServer] = {}
    for name, spec in raw.items():
        if not isinstance(name, str) or not _NAME.match(name):
            raise McpSpecError(f"{where}: mcp server name {name!r} must match [A-Za-z0-9_-]{{1,40}}")
        if not isinstance(spec, dict) or not isinstance(spec.get("url"), str) or not spec["url"].strip():
            raise McpSpecError(f"{where}: mcp server {name!r} needs a url")
        headers = spec.get("headers") or {}
        if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
            raise McpSpecError(f"{where}: mcp server {name!r} headers must be a string mapping")
        tools = spec.get("tools")
        if not isinstance(tools, list) or not tools or not all(isinstance(t, str) and t for t in tools):
            raise McpSpecError(f"{where}: mcp server {name!r} needs `tools`: a list of tool names, or [\"*\"]")
        server = McpServer(name=name, url=spec["url"].strip(), headers={str(k): v for k, v in headers.items()},
                           tools=list(tools), ca_cert=spec.get("ca_cert") or None,
                           description=str(spec.get("description") or "").strip())
        missing = sorted(server.secret_refs() - set(declared_secrets))
        if missing:
            raise McpSpecError(f"{where}: mcp server {name!r} references undeclared secrets {missing}")
        servers[name] = server
    return servers


def env_ref_headers(server: McpServer) -> dict[str, str]:
    """Headers with ``{secret.NAME}`` turned into ``${NAME}`` — for agents
    that expand environment variables in their MCP config (Claude Code does),
    so the value itself is never written anywhere."""
    return {k: _SECRET_REF.sub(lambda m: "${" + m.group(1) + "}", v) for k, v in server.headers.items()}
