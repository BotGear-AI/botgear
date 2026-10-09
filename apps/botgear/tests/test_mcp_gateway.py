# unmanaged
"""botgear MCP gateway (src/botgear/mcp_gateway): tool mapping, client
profiles, the call path (HALT → policy → bus → audit) and, end to end, the
real MCP SDK client talking to the gateway's ASGI app."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

import mcp_types as types

from botgear.agent_surface import ToolDescriptor, ToolResult, add_meta, check_call
from botgear.mcp_gateway import tools as mcp_tools
from botgear.mcp_gateway.gateway import McpGateway, _principal
from botgear.mcp_gateway.profiles import DEFAULT_PROFILE, load_profile, principal_key


SWITCH = {"topics": {"control": "/tasmota/switch-1/control"}, "methods": [
    {"name": "power_set", "doc": "Turn a relay on or off.",
     "args_schema": {"type": "object", "properties": {"on": {"type": "boolean"}, "relay": {"type": "integer"}},
                     "required": ["on"]}},
    {"name": "list_relays", "doc": "List relays."},
]}
CAMERA = {"topics": {"control": "/video/camera/control"}, "methods": [{"name": "capture_frame"}]}
BRAIN = {"topics": {"control": "/brain/brain/control"}, "methods": [
    {"name": "start_workflow"}, {"name": "list_workflows"}, {"name": "set_config"}]}
API_CLIENT = {"id": "client-1", "name": "claude-code", "roles": [], "auth_type": "shared_secret"}


# ─── tool mapping ───────────────────────────────────────────────────────


def test_tool_names_and_annotations():
    d = ToolDescriptor(topic="/tasmota/switch-1/control", action="power_set")
    assert mcp_tools.tool_name(d) == "switch-1__power_set"
    assert mcp_tools.annotations_for(d).read_only_hint is None          # may change state (MCP default)
    ro = mcp_tools.annotations_for(ToolDescriptor(topic="/x/y/control", action="list_relays"))
    assert ro.read_only_hint is True and ro.destructive_hint is False


def test_long_names_fit_provider_limits():
    d = ToolDescriptor(topic=f"/t/{'p' * 63}/control", action="a_very_long_action_name_indeed")
    name = mcp_tools.tool_name(d)
    assert len(name) <= mcp_tools.MAX_NAME and len("mcp__botgear__" + name) <= 64


def test_input_schema_always_has_an_object_root():
    assert mcp_tools.input_schema(ToolDescriptor(topic="/a/b/control", action="x")) == {
        "type": "object", "properties": {}}
    tool = mcp_tools.to_mcp_tool("switch-1__power_set", descriptors()["switch-1__power_set"])
    assert tool.input_schema["required"] == ["on"] and "topic /tasmota/switch-1/control" in tool.description


def descriptors() -> Dict[str, ToolDescriptor]:
    catalog: Dict[str, ToolDescriptor] = {}
    for meta in (SWITCH, CAMERA, BRAIN):
        add_meta(catalog, meta)
    return mcp_tools.name_map(catalog.values())


# ─── profiles ───────────────────────────────────────────────────────────


def test_principal_key():
    assert principal_key(API_CLIENT) == "claude-code"
    assert principal_key({"email": "op@x.com", "auth_type": "token"}) == "op@x.com"
    assert principal_key({"name": "a/b c", "auth_type": "shared_secret"}) == "a_b_c"


def test_default_profile(tmp_path):
    p = load_profile(tmp_path, API_CLIENT)
    assert check_call(p, "/tasmota/switch-1/control", "power_set").allowed
    assert not check_call(p, "/video/camera/control", "capture_frame").allowed          # shared exclude
    assert not check_call(p, "/brain/brain/control", "start_workflow").allowed          # brain self-control
    assert check_call(p, "/brain/brain/control", "list_workflows").allowed
    assert not check_call(p, "/tasmota/switch-1/control", "set_config").allowed         # config writes
    assert not check_call(p, "/agent/agent/control", "start_run").allowed               # no nested agents
    assert check_call(p, "/agent/agent/control", "list_runs").allowed
    assert DEFAULT_PROFILE["default_policy"] == "allow"


def test_client_and_default_profile_files(tmp_path):
    pdir = tmp_path / "mcp" / "profiles"
    pdir.mkdir(parents=True)
    (pdir / "default.yaml").write_text("allowed:\n  - topic: /tasmota/*/control\n    actions: [list_relays]\n")
    other = load_profile(tmp_path, {"name": "other", "auth_type": "shared_secret"})
    assert check_call(other, "/tasmota/s/control", "list_relays").allowed
    assert not check_call(other, "/tasmota/s/control", "power_set").allowed
    (pdir / "claude-code.yaml").write_text("default_policy: allow\n")
    mine = load_profile(tmp_path, API_CLIENT)
    assert check_call(mine, "/tasmota/s/control", "power_set").allowed
    assert not check_call(mine, "/video/c/control", "capture_frame").allowed           # excludes still apply
    (pdir / "claude-code.yaml").write_text("allowed: [oops")
    assert not check_call(load_profile(tmp_path, API_CLIENT), "/tasmota/s/control", "list_relays").allowed


# ─── gateway handlers ───────────────────────────────────────────────────


class FakeBus:
    def __init__(self) -> None:
        self.published: List[tuple] = []
        self.retained: Dict[str, Any] = {"/tasmota/switch-1/state": {"relays": [{"index": 1, "on": True}]}}

    def publish_sync(self, topic, payload, **_):
        self.published.append((topic, payload))

    def retained_get(self, topic):
        return SimpleNamespace(payload=self.retained[topic]) if topic in self.retained else None

    async def subscribe(self, topic, subscriber_id=None):
        if False:
            yield None


def make_gateway(tmp_path, *, authenticate=None):
    bus = FakeBus()
    gw = McpGateway(bus_getter=lambda: bus, data_dir=tmp_path,
                    authenticate=authenticate or (lambda headers: API_CLIENT))
    for meta in (SWITCH, CAMERA, BRAIN):
        add_meta(gw.catalog, meta)
    calls: List[tuple] = []

    class _Exec:
        async def call(self, *, topic, action, args, timeout):
            calls.append((topic, action, args))
            return ToolResult(status="ok", value={"relay": 1, "on": args.get("on")})

    gw._executor = _Exec()
    return gw, bus, calls


def run_as(principal, coro):
    async def _go():
        token = _principal.set(principal)
        try:
            return await coro
        finally:
            _principal.reset(token)
    return _go()


def params(name, args=None):
    return types.CallToolRequestParams(name=name, arguments=args or {})


@pytest.mark.asyncio
async def test_list_tools_respects_the_profile(tmp_path):
    gw, _, _ = make_gateway(tmp_path)
    names = [t.name for t in (await run_as(API_CLIENT, gw._on_list_tools(None, None))).tools]
    assert "switch-1__power_set" in names and "brain__list_workflows" in names
    assert "camera__capture_frame" not in names and "brain__start_workflow" not in names
    assert {"botgear_list_capabilities", "botgear_get_state", "botgear_halt"} <= set(names)


@pytest.mark.asyncio
async def test_call_goes_over_the_bus_and_is_audited(tmp_path, monkeypatch):
    from botgear.runtime import halt
    monkeypatch.setattr(halt, "is_halted", lambda: False)
    gw, bus, calls = make_gateway(tmp_path)
    res = await run_as(API_CLIENT, gw._on_call_tool(None, params("switch-1__power_set", {"on": False})))
    assert not res.is_error and res.structured_content == {"relay": 1, "on": False}
    assert calls == [("/tasmota/switch-1/control", "power_set", {"on": False})]
    audit = [json.loads(line) for line in (tmp_path / "mcp" / "calls.jsonl").read_text().splitlines()]
    assert audit[-1]["client"] == "claude-code" and audit[-1]["action"] == "power_set" and audit[-1]["ok"]
    assert any(t == "/mcp/gateway/calls" for t, _ in bus.published)


@pytest.mark.asyncio
async def test_blocked_and_unknown_tools_are_refused(tmp_path, monkeypatch):
    from botgear.runtime import halt
    monkeypatch.setattr(halt, "is_halted", lambda: False)
    gw, _, calls = make_gateway(tmp_path)
    for name in ("camera__capture_frame", "brain__start_workflow", "nope__nothing"):
        res = await run_as(API_CLIENT, gw._on_call_tool(None, params(name)))
        assert res.is_error, name
    assert calls == []


@pytest.mark.asyncio
async def test_halt_refuses_every_action(tmp_path, monkeypatch):
    from botgear.runtime import halt
    monkeypatch.setattr(halt, "is_halted", lambda: True)
    monkeypatch.setattr(halt, "state", lambda: {"source": "voice:ear", "reason": "heard 'stop'"})
    gw, _, calls = make_gateway(tmp_path)
    res = await run_as(API_CLIENT, gw._on_call_tool(None, params("switch-1__power_set", {"on": True})))
    assert res.is_error and "HALTED" in res.content[0].text and calls == []


@pytest.mark.asyncio
async def test_builtin_halt_broadcasts(tmp_path, monkeypatch):
    from botgear.runtime import halt
    seen = {}
    monkeypatch.setattr(halt, "request_halt", lambda level, source, reason: seen.update(
        level=level, source=source, reason=reason) or {"halted": True})
    gw, _, _ = make_gateway(tmp_path)
    res = await run_as(API_CLIENT, gw._on_call_tool(None, params("botgear_halt", {"reason": "loop", "level": "emergency"})))
    assert not res.is_error and seen == {"level": "emergency", "source": "mcp:claude-code", "reason": "loop"}


@pytest.mark.asyncio
async def test_builtin_get_state_and_capabilities(tmp_path, monkeypatch):
    from botgear.runtime import halt
    monkeypatch.setattr(halt, "is_halted", lambda: False)
    gw, _, _ = make_gateway(tmp_path)
    res = await run_as(API_CLIENT, gw._on_call_tool(None, params("botgear_get_state", {"service": "switch-1"})))
    assert res.structured_content == {"relays": [{"index": 1, "on": True}]}
    res = await run_as(API_CLIENT, gw._on_call_tool(None, params("botgear_get_state", {"service": "camera"})))
    assert res.is_error                                                 # not visible under the profile
    res = await run_as(API_CLIENT, gw._on_call_tool(None, params("botgear_list_capabilities")))
    caps = res.structured_content["capabilities"]
    assert caps["tasmota"]["instances"] == ["switch-1"] and "video" not in caps
    assert res.structured_content["halted"] is False


# ─── end to end: the real MCP client against the ASGI app ───────────────


@pytest.mark.asyncio
async def test_mcp_client_end_to_end(tmp_path, monkeypatch):
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from botgear.runtime import halt
    monkeypatch.setattr(halt, "is_halted", lambda: False)

    def authenticate(headers):
        if headers.get("x-api-key") != "s3cret":
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Invalid API key")
        return API_CLIENT

    gw, _, calls = make_gateway(tmp_path, authenticate=authenticate)
    runner = asyncio.create_task(gw.run())
    try:
        await asyncio.wait_for(gw._ready.wait(), 5)

        bad = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=gw), base_url="http://botgear")
        r = await bad.post("/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401
        await bad.aclose()

        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=gw), base_url="http://botgear",
                                  headers={"X-API-Key": "s3cret"})
        async with streamable_http_client("http://botgear/v1/mcp", http_client=http) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = {t.name for t in listed.tools}
                assert "switch-1__power_set" in names and "camera__capture_frame" not in names
                res = await session.call_tool("switch-1__power_set", {"on": True})
                assert not res.is_error
        assert calls == [("/tasmota/switch-1/control", "power_set", {"on": True})]
        await http.aclose()
    finally:
        runner.cancel()
        try:
            await runner
        except BaseException:  # noqa: BLE001
            pass
