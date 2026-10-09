# unmanaged
"""botgear's MCP gateway: any MCP client (Claude Code, Codex, the Claude
desktop app, an agent_harness run) can discover and call botgear's live
services — under one tool policy and HALT.

    POST/GET/DELETE  /v1/mcp        streamable HTTP, JSON responses, stateless

Per request:
  1. Authenticate exactly like the REST API: ``X-API-Key`` (an API client)
     or ``Authorization: Bearer <login token>``. Anything else → 401.
  2. Resolve the caller's profile (profiles.py) → ToolPolicy.
  3. tools/list → every live action the profile allows, as MCP tools, plus
     the built-ins (list_capabilities / get_state / halt).
  4. tools/call → HALT check (agent_surface.halt_verdict) → policy check →
     bus request/reply (agent_surface.ToolExecutor) → result. Every call is
     audited: ``<data_dir>/mcp/calls.jsonl`` + ``/mcp/gateway/calls`` on the bus.

Stateless on purpose: no sessions to leak, auth on every request, and the
tool list is recomputed from the live catalog each time (so no
``tools/list_changed`` push is needed — a client that re-lists sees new
services). See docs/AGENT_ROADMAP.md, Phase 1.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager

from botgear.agent_surface import (
    Catalog,
    ToolDescriptor,
    ToolExecutor,
    ToolPolicy,
    bus_reply_iter,
    check_call,
    halt_verdict,
    watch_meta,
    watch_releases,
)
from botgear.mcp_gateway import tools as mcp_tools
from botgear.mcp_gateway.profiles import load_profile, principal_key

logger = logging.getLogger(__name__)

CALLS_TOPIC = "/mcp/gateway/calls"
CALL_TIMEOUT_S = 30.0
_HIDDEN_STATE_PREFIXES = ("/security/",)          # never readable through get_state

# The authenticated caller of the current request (set by the ASGI auth
# wrapper; stateless handlers run inside that request's task, so they see it).
_principal: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "botgear_mcp_principal", default=None)

INSTRUCTIONS = (
    "You are connected to a live botgear robot runtime. Tools act on REAL devices. "
    "Start with botgear_list_capabilities. Read-only tools are marked readOnlyHint. "
    "If anything goes wrong, or a person asks you to stop, call botgear_halt — it "
    "stops speech, playback and running workflows at once. While the runtime is "
    "HALTed every other action is refused until an operator resumes."
)


class McpGateway:
    def __init__(
        self,
        *,
        bus_getter: Callable[[], Any],
        data_dir: Path,
        authenticate: Callable[[Dict[str, str]], Dict[str, Any]],
        version: str = "",
    ):
        self._bus = bus_getter
        self._data_dir = Path(data_dir)
        self._authenticate = authenticate
        self.catalog: Catalog = {}
        self._executor = ToolExecutor(
            publish=lambda topic, payload: self._bus().publish_sync(topic, payload),
            subscribe_reply=lambda topic: bus_reply_iter(self._bus(), topic, subscriber_id=f"mcp-gw-reply-{topic}"),
            reply_root="/mcp/gateway/_replies",
        )
        self.server = Server(
            "botgear",
            version=version,
            title="botgear robot runtime",
            instructions=INSTRUCTIONS,
            on_list_tools=self._on_list_tools,
            on_call_tool=self._on_call_tool,
        )
        self._manager = StreamableHTTPSessionManager(app=self.server, stateless=True, json_response=True)
        self._mcp_asgi = StreamableHTTPASGIApp(self._manager)
        self._ready = asyncio.Event()
        self._tasks: list = []

    # ─── lifecycle ───────────────────────────────────────────────────

    async def run(self) -> None:
        """Feed the catalog and run the MCP session manager until cancelled.
        Started once from the backend's startup (event_handlers)."""
        async def _changed() -> None:
            return None

        bus = self._bus()
        self._tasks = [
            asyncio.create_task(watch_meta(self.catalog, bus, "mcp-gateway-types", _changed)),
            asyncio.create_task(watch_releases(self.catalog, bus, "mcp-gateway-lifecycle", _changed)),
        ]
        try:
            async with self._manager.run():
                self._ready.set()
                logger.info("mcp_gateway: serving /v1/mcp")
                await asyncio.Event().wait()
        finally:
            for t in self._tasks:
                t.cancel()

    # ─── ASGI (auth wrapper around the SDK's handler) ────────────────

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        try:
            principal = self._authenticate(headers)
        except Exception as exc:  # noqa: BLE001 — HTTPException(401/500) or anything else
            status = getattr(exc, "status_code", 401)
            detail = getattr(exc, "detail", None) or "authentication required"
            await _send_json(send, status, {"error": str(detail)},
                             extra=[(b"www-authenticate", b'Bearer realm="botgear"')] if status == 401 else None)
            return
        if not self._ready.is_set():
            try:
                await asyncio.wait_for(self._ready.wait(), timeout=5)
            except asyncio.TimeoutError:
                await _send_json(send, 503, {"error": "MCP gateway is starting"})
                return
        token = _principal.set(principal)
        try:
            await self._mcp_asgi(scope, receive, send)
        finally:
            _principal.reset(token)

    # ─── MCP handlers ────────────────────────────────────────────────

    def _policy(self) -> ToolPolicy:
        return load_profile(self._data_dir, _principal.get())

    def _allowed(self, policy: ToolPolicy) -> Dict[str, ToolDescriptor]:
        visible = [d for d in list(self.catalog.values())
                   if check_call(policy, d.topic, d.action, catalog=self.catalog).allowed]
        return mcp_tools.name_map(visible)

    async def _on_list_tools(self, ctx, params) -> types.ListToolsResult:
        allowed = self._allowed(self._policy())
        listed = mcp_tools.builtin_tools() + [mcp_tools.to_mcp_tool(n, d) for n, d in sorted(allowed.items())]
        return types.ListToolsResult(tools=listed)

    async def _on_call_tool(self, ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        name = params.name
        args = dict(params.arguments or {})
        principal = _principal.get()
        client = principal_key(principal)
        t0 = time.monotonic()

        if name.startswith(mcp_tools.BUILTIN_PREFIX):
            result = await self._builtin(name, args, client)
            self._audit(client, name, None, None, args, result, t0)
            return result

        policy = self._policy()
        allowed = self._allowed(policy)
        descriptor = allowed.get(name)
        if descriptor is None:
            result = _error(f"unknown or not-allowed tool {name!r} — call tools/list or botgear_list_capabilities")
            self._audit(client, name, None, None, args, result, t0)
            return result

        refusal = halt_verdict() or check_call(policy, descriptor.topic, descriptor.action, catalog=self.catalog)
        if not refusal.allowed:
            result = _error(f"refused: {refusal.reason}")
            self._audit(client, name, descriptor.topic, descriptor.action, args, result, t0)
            return result

        res = await self._executor.call(topic=descriptor.topic, action=descriptor.action, args=args,
                                        timeout=CALL_TIMEOUT_S)
        if res.status == "ok":
            result = _ok(res.value)
        elif res.status == "timeout":
            result = _error(f"timed out: {res.error}")
        else:
            result = _error(res.error or "error")
        self._audit(client, name, descriptor.topic, descriptor.action, args, result, t0)
        return result

    async def _builtin(self, name: str, args: Dict[str, Any], client: str) -> types.CallToolResult:
        short = name[len(mcp_tools.BUILTIN_PREFIX):]
        if short == "halt":
            from botgear.runtime import halt
            level = args.get("level") if args.get("level") in ("stop", "emergency") else "stop"
            state = halt.request_halt(level, source=f"mcp:{client}",
                                      reason=str(args.get("reason") or "requested by an MCP client"))
            return _ok({"halted": True, "state": state})
        if short == "list_capabilities":
            from botgear.runtime import halt
            allowed = self._allowed(self._policy())
            return _ok({"halted": halt.is_halted(), "capabilities": mcp_tools.capability_summary(allowed)})
        if short == "get_state":
            return self._get_state(str(args.get("service") or "").strip())
        return _error(f"unknown tool {name!r}")

    def _get_state(self, service: str) -> types.CallToolResult:
        if not service:
            return _error("service is required, e.g. service='switch-1'")
        if service == "halt":
            topic = "/runtime/halt_state"
        elif service == "runtime":
            topic = "/runtime/runtime/state"
        else:
            policy = self._policy()
            try:
                topic, control = mcp_tools.state_topic_for(service, self._allowed(policy))
            except KeyError:
                return _error(f"no visible service {service!r} — see botgear_list_capabilities")
            if topic.startswith(_HIDDEN_STATE_PREFIXES):
                return _error(f"state of {service!r} is not readable over MCP")
        try:
            msg = self._bus().retained_get(topic)
        except Exception:  # noqa: BLE001
            msg = None
        payload = msg.payload if hasattr(msg, "payload") else msg
        if payload is None:
            return _error(f"{service!r} has published no state yet ({topic})")
        return _ok(payload)

    # ─── audit ───────────────────────────────────────────────────────

    def _audit(self, client: str, tool: str, topic: Optional[str], action: Optional[str],
               args: Dict[str, Any], result: types.CallToolResult, t0: float) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "id": uuid.uuid4().hex[:12],
            "client": client,
            "tool": tool,
            "topic": topic,
            "action": action,
            "args": args,
            "ok": not result.is_error,
            "detail": _first_text(result)[:500],
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }
        logger.info("mcp_gateway: %s %s → %s", client, tool, "ok" if record["ok"] else record["detail"][:120])
        try:
            path = self._data_dir / "mcp" / "calls.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError:
            logger.exception("mcp_gateway: audit write failed")
        try:
            self._bus().publish_sync(CALLS_TOPIC, record)
        except Exception:  # noqa: BLE001
            logger.debug("mcp_gateway: audit publish failed", exc_info=True)


# ─── helpers ─────────────────────────────────────────────────────────


def _ok(value: Any) -> types.CallToolResult:
    text = value if isinstance(value, str) else json.dumps(value, default=str, indent=1)
    structured = value if isinstance(value, dict) else None
    return types.CallToolResult(content=[types.TextContent(text=text)], structured_content=structured)


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=message)], is_error=True)


def _first_text(result: types.CallToolResult) -> str:
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text:
            return text
    return ""


async def _send_json(send, status: int, body: Dict[str, Any], extra=None) -> None:
    raw = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers + list(extra or [])})
    await send({"type": "http.response.body", "body": raw})
