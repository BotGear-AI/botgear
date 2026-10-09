# unmanaged
"""ClaudeAgentMind — a rented Claude agent session as brain's mind.

Unlike the other adapters this is not called once per step. It holds one
persistent Claude Agent SDK session for the whole run (roadmap Phase 4):
the engine opens it with the run's system prompt and the workflow's
allowed tools, then hands it one turn per operator utterance. The
session decides which tools to call; every call comes back into the
engine through ``ToolHandler`` (safety gate, speak_wait, step log, HALT
— see ``WorkflowEngine._session_loop``), so the session never touches
the bus directly and never sees a tool the workflow doesn't allow.

Auth is the backend user's Claude Code login (no API key in botgear).
Requires the optional ``claude-agent-sdk`` dependency
(``uv sync --extra dev --extra claude-agent``).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from brain.adapters.base import ModelAdapter, resolve_tool_names
from brain.schemas import ChatMessage, ModelResponse, ToolDescriptor

logger = logging.getLogger(__name__)

# (descriptor, args) -> (text for the model, is_error)
ToolHandler = Callable[[ToolDescriptor, Dict[str, Any]], Awaitable[Tuple[str, bool]]]

_SERVER = "botgear"


def sdk_available() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass
class TurnResult:
    """What one ``turn()`` produced."""
    text: str = ""                      # the session's closing text, if any
    tool_calls: int = 0
    cost_usd: float = 0.0               # this turn only
    input_tokens: int = 0
    output_tokens: int = 0
    api_turns: int = 0                  # model round trips in this turn
    is_error: bool = False
    error: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


class ClaudeAgentMind(ModelAdapter):
    name = "claude_agent"
    # The engine checks this to run ``_session_loop`` instead of ``_loop``.
    is_session = True

    def __init__(self, *, model: str = "haiku", cli_path: Optional[str] = None,
                 max_turns_per_utterance: int = 12, max_budget_usd: Optional[float] = None):
        self.model = model
        self.max_budget_usd = max_budget_usd
        self.cli_path = cli_path or None
        self.max_turns_per_utterance = max_turns_per_utterance
        self._client: Any = None
        self._cost_total = 0.0
        self.tool_names: Dict[str, ToolDescriptor] = {}

    async def complete(self, messages: List[ChatMessage], tools: Optional[List[ToolDescriptor]] = None,
                       *, model: Optional[str] = None, max_tokens: Optional[int] = None,
                       temperature: Optional[float] = None) -> ModelResponse:
        raise NotImplementedError("claude_agent is a session mind — the engine drives it via open()/turn()")

    def encode_tools_for_log(self, tools: List[ToolDescriptor]) -> List[Dict[str, Any]]:
        return [
            {"name": f"mcp__{_SERVER}__{n}", "description": t.description, "input_schema": _schema(t)}
            for n, t in zip(resolve_tool_names(tools), tools)
        ]

    async def open(self, *, system_prompt: str, tools: List[ToolDescriptor], handler: ToolHandler,
                   model: Optional[str] = None) -> None:
        """Start the session. ``tools`` are exposed as one in-process MCP
        server; nothing else (no files, shell or web) is enabled."""
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server, tool

        sdk_tools = []
        self.tool_names = {}
        for name, desc in zip(resolve_tool_names(tools), tools):
            self.tool_names[name] = desc
            sdk_tools.append(tool(name, desc.description or f"{desc.action} on {desc.topic}",
                                  _schema(desc))(_bind(handler, desc)))
        server = create_sdk_mcp_server(_SERVER, tools=sdk_tools)
        opts = ClaudeAgentOptions(
            system_prompt=system_prompt,
            model=model or self.model,
            mcp_servers={_SERVER: server},
            tools=[],                                   # no built-in tools at all
            allowed_tools=[f"mcp__{_SERVER}__{n}" for n in self.tool_names],
            setting_sources=[],                         # ignore user/project settings, CLAUDE.md
            max_turns=self.max_turns_per_utterance,
            cli_path=self.cli_path,
            max_budget_usd=self.max_budget_usd,         # whole session; the turn ends with an error past it
            # brain logs every run itself; keep robot conversations out of the
            # backend user's Claude Code history (~/.claude/projects).
            extra_args={"no-session-persistence": None},
        )
        self._client = ClaudeSDKClient(options=opts)
        await self._client.connect()

    async def turn(self, text: str) -> TurnResult:
        """Send one message and run the session until it ends its turn."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

        out = TurnResult()
        texts: List[str] = []
        await self._client.query(text)
        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        texts.append(block.text.strip())
                    elif isinstance(block, ToolUseBlock):
                        out.tool_calls += 1
            elif isinstance(msg, ResultMessage):
                total = float(msg.total_cost_usd or 0.0)
                out.cost_usd = max(0.0, total - self._cost_total)
                self._cost_total = max(self._cost_total, total)
                usage = msg.usage or {}
                out.input_tokens = int(usage.get("input_tokens") or 0) + int(
                    usage.get("cache_read_input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)
                out.output_tokens = int(usage.get("output_tokens") or 0)
                out.api_turns = int(msg.num_turns or 0)
                out.is_error = bool(msg.is_error)
                if msg.is_error:
                    out.error = "; ".join(msg.errors or []) or (msg.result or msg.subtype)
                out.raw = {"subtype": msg.subtype, "duration_ms": msg.duration_ms,
                           "duration_api_ms": msg.duration_api_ms, "session_id": msg.session_id,
                           "stop_reason": msg.stop_reason, "usage": usage}
        # The closing text is what the session said after its last tool call.
        out.text = texts[-1] if texts else ""
        return out

    @property
    def cost_total(self) -> float:
        return self._cost_total

    async def interrupt(self) -> None:
        if self._client is not None:
            try:
                await self._client.interrupt()
            except Exception:  # noqa: BLE001
                logger.debug("claude_agent: interrupt failed", exc_info=True)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except BaseException:  # noqa: BLE001 — never let teardown mask the run's outcome
                logger.debug("claude_agent: disconnect failed", exc_info=True)


def _schema(desc: ToolDescriptor) -> Dict[str, Any]:
    params = desc.parameters if isinstance(desc.parameters, dict) else {}
    if params.get("type") == "object":
        return params
    return {"type": "object", "properties": {}}


def _bind(handler: ToolHandler, desc: ToolDescriptor):
    async def _call(args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            text, is_error = await handler(desc, dict(args or {}))
        except Exception as exc:  # noqa: BLE001 — report, don't crash the session
            logger.exception("claude_agent: tool %s::%s failed", desc.topic, desc.action)
            text, is_error = json.dumps({"error": f"{type(exc).__name__}: {exc}"}), True
        out: Dict[str, Any] = {"content": [{"type": "text", "text": text}]}
        if is_error:
            out["is_error"] = True
        return out
    return _call
