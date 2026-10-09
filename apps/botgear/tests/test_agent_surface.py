# unmanaged
"""botgear.agent_surface — the tool catalog, policy, invocation and
audit shapes every mind shares (brain today; the MCP gateway next).
Brain's own tests cover the same behaviour through brain; these pin the
surface's API directly and check brain really uses this one copy."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from botgear.agent_surface import (
    SHARED_EXCLUDES,
    ToolDescriptor,
    ToolExecutor,
    ToolPattern,
    ToolPolicy,
    add_meta,
    capability_prefix,
    check_call,
    descriptors_from_meta,
    evict_proxy,
    halt_verdict,
    load_policy_file,
    policy_from_dict,
    released_proxy,
    shared_excludes,
    topic_matches,
    watch_meta,
    watch_releases,
)

_BRAIN_PKG = Path(__file__).resolve().parents[1] / "repo" / "brain" / "1.0.0"
sys.path.insert(0, str(_BRAIN_PKG))


# ─── policy ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("pattern, topic, ok", [
    ("/video/*/control", "/video/video-1/control", True),
    ("/video/*/control", "/video/a/b/control", False),          # * is one segment
    ("/video/*/control", "/video/video-1@peer/control", False),  # federation needs opt-in
    ("/video/*@**/control", "/video/video-1@peer/control", True),
    ("/video/*@**/control", "/video/video-1/control", True),
    ("/speech/mouth/control", "/speech/mouth/control", True),
])
def test_topic_matches(pattern, topic, ok):
    assert topic_matches(pattern, topic) is ok


def test_capability_prefix():
    assert capability_prefix("/speech/mouth/control") == "speech"
    assert capability_prefix("") == ""


def test_block_always_wins_over_allow_and_default():
    policy = ToolPolicy(
        allowed=[ToolPattern(topic="/tasmota/*/control", actions=["*"])],
        blocked=[ToolPattern(topic="/tasmota/*/control", actions=["power_set"])],
        default_policy="allow",
    )
    assert check_call(policy, "/tasmota/sw/control", "power_set").guard == "block_list"
    assert check_call(policy, "/tasmota/sw/control", "list_relays").allowed


def test_default_deny_vs_allow():
    deny = ToolPolicy()
    v = check_call(deny, "/speech/mouth/control", "speak")
    assert not v.allowed and v.guard == "allow_list"
    allow = policy_from_dict({"default_policy": "allow"})
    assert check_call(allow, "/speech/mouth/control", "speak").allowed


def test_unknown_target_only_checked_for_allow_rule_matches():
    policy = ToolPolicy(allowed=[ToolPattern(topic="/speech/*/control", actions=["speak"])])
    catalog = {"/speech/mouth/control::speak": ToolDescriptor(topic="/speech/mouth/control", action="speak")}
    assert check_call(policy, "/speech/mouth/control", "speak", catalog=catalog).allowed
    v = check_call(policy, "/speech/gone/control", "speak", catalog=catalog)
    assert not v.allowed and v.guard == "unknown_target"


def test_default_allow_inherits_shared_excludes_deduped():
    policy = policy_from_dict({
        "default_policy": "allow",
        "blocked": [{"topic": "/video/*/control", "actions": ["*"]}],   # already one of the excludes
    })
    topics = [p.topic for p in policy.blocked]
    assert topics.count("/video/*/control") == 1
    assert "/motor_control/*/control" in topics and "/sabertooth/*/control" in topics
    assert check_call(policy, "/sabertooth/s1/control", "motor_set").guard == "block_list"


def test_default_deny_does_not_add_excludes():
    assert policy_from_dict({"default_policy": "deny"}).blocked == []


def test_load_policy_file(tmp_path):
    assert load_policy_file(tmp_path / "missing.yaml") == ToolPolicy()
    f = tmp_path / "allowed_tools.yaml"
    f.write_text("allowed:\n  - topic: /stt/*/control\n    actions: [listen]\n")
    p = load_policy_file(f)
    assert p.default_policy == "deny" and p.allowed[0].actions == ["listen"]


def test_shared_excludes_copy_is_independent():
    mine = shared_excludes()
    mine.append(ToolPattern(topic="/x/*/control", actions=["*"]))
    mine[0].actions.append("oops")
    assert len(SHARED_EXCLUDES) == 6 and SHARED_EXCLUDES[0].actions == ["*"]


def test_halt_verdict(monkeypatch):
    from botgear.runtime import halt
    monkeypatch.setattr(halt, "is_halted", lambda: False)
    assert halt_verdict() is None
    monkeypatch.setattr(halt, "is_halted", lambda: True)
    monkeypatch.setattr(halt, "state", lambda: {"source": "voice:ear", "reason": "heard 'stop'"})
    v = halt_verdict()
    assert not v.allowed and v.guard == "halted" and "voice:ear" in v.reason


# ─── catalog ────────────────────────────────────────────────────────────


META = {
    "topics": {"control": "/tasmota/switch-1/control"},
    "methods": [
        {"name": "power_set", "doc": "  Turn a relay on/off.  ", "args_schema": {"type": "object"}},
        {"name": "list_relays"},
        {"doc": "nameless — skipped"},
    ],
}


def test_descriptors_from_meta():
    ds = descriptors_from_meta(META)
    assert [d.name for d in ds] == ["/tasmota/switch-1/control::power_set", "/tasmota/switch-1/control::list_relays"]
    assert ds[0].description == "Turn a relay on/off." and ds[1].parameters == {}
    assert descriptors_from_meta({"topics_root": "/old/x-1", "methods": [{"name": "go"}]})[0].topic == "/old/x-1/control"
    assert descriptors_from_meta({"methods": [{"name": "go"}]}) == []
    assert descriptors_from_meta(None) == []


def test_add_and_evict():
    catalog: Dict[str, ToolDescriptor] = {}
    assert add_meta(catalog, META) == 2
    add_meta(catalog, {"topics": {"control": "/speech/mouth/control"}, "methods": [{"name": "speak"}]})
    assert evict_proxy(catalog, "switch-1", "speech") == 0          # wrong type
    assert evict_proxy(catalog, "switch-1") == 2
    assert list(catalog) == ["/speech/mouth/control::speak"]


def test_released_proxy():
    assert released_proxy({"id": "sw", "status": "uninstalled", "service_meta_id": "tasmota@1.0.0"}) == ("sw", "tasmota")
    assert released_proxy({"id": "sw", "status": "running"}) is None
    assert released_proxy({"status": "uninstalled"}) is None


class _Bus:
    def __init__(self, payloads: List[Any]):
        self.payloads = payloads

    async def subscribe(self, topic, subscriber_id):
        for p in self.payloads:
            yield SimpleNamespace(payload=p)


@pytest.mark.asyncio
async def test_watchers_feed_and_prune_the_catalog():
    catalog: Dict[str, ToolDescriptor] = {}
    changes = []

    async def on_change():
        changes.append(len(catalog))

    await watch_meta(catalog, _Bus([META, "not a dict"]), "t", on_change)
    assert len(catalog) == 2 and changes == [2]
    await watch_releases(catalog, _Bus([
        {"id": "other", "status": "uninstalled"},
        {"id": "switch-1", "status": "running"},
        {"id": "switch-1", "status": "uninstalled", "service_meta_id": "tasmota@1.0.0"},
    ]), "t", on_change)
    assert catalog == {} and changes == [2, 0]                      # only a real eviction notifies


# ─── invocation ─────────────────────────────────────────────────────────


def _executor(reply: Any, delay: float = 0.0):
    sent: List[tuple] = []

    async def subscribe_reply(topic):
        await asyncio.sleep(delay)
        yield reply

    ex = ToolExecutor(publish=lambda t, p: sent.append((t, p)), subscribe_reply=subscribe_reply, reply_root="/x/_r")
    return ex, sent


@pytest.mark.asyncio
async def test_executor_ok_error_timeout():
    ex, sent = _executor({"relay": 1, "on": False})
    r = await ex.call(topic="/tasmota/sw/control", action="power_set", args={"on": False})
    assert r.status == "ok" and r.value == {"relay": 1, "on": False}
    assert sent[0][0] == "/tasmota/sw/control" and sent[0][1]["action"] == "power_set"
    assert sent[0][1]["reply_to"].startswith("/x/_r/")

    ex, _ = _executor({"error": "relay busy"})
    r = await ex.call(topic="/t", action="a", args={})
    assert r.status == "error" and r.error == "relay busy"

    ex, _ = _executor({"late": True}, delay=1.0)
    r = await ex.call(topic="/t", action="a", args={}, timeout=0.05)
    assert r.status == "timeout"


# ─── one implementation ────────────────────────────────────────────────


def test_brain_uses_the_surface_not_a_copy():
    import brain.context_loader as cl
    import brain.safety_gate as sg
    import brain.schemas as bs
    import brain.tool_executor as te
    import botgear.agent_surface as surface
    assert bs.ToolDescriptor is surface.ToolDescriptor
    assert bs.AllowedTools is surface.ToolPolicy
    assert bs.ToolResult is surface.ToolResult and bs.ToolCallRecord is surface.ToolCallRecord
    assert sg._topic_matches is surface.topic_matches
    assert sg.capability_prefix is surface.capability_prefix
    assert te.ToolExecutor is surface.ToolExecutor
    assert [p.topic for p in cl._load_shared_excludes()] == [p.topic for p in SHARED_EXCLUDES]
