# unmanaged
"""runtime_control — the runtime's read-only /meta + /control surface."""
from __future__ import annotations

import asyncio
import inspect

import pytest

from botgear.runtime import runtime_control
from botgear.runtime.bus import Bus


@pytest.fixture
def bus(monkeypatch):
    test_bus = Bus()
    monkeypatch.setattr(runtime_control, "get_bus", lambda: test_bus)
    return test_bus


def test_actions_are_read_only_and_argless():
    # Default-allow brain workflows can call anything on this topic, and a
    # block can't be re-opened per action — a write action here would be
    # reachable by them. Changing this set means re-reading the module
    # docstring (and probably using a separate topic).
    assert set(runtime_control.ACTIONS) == {
        "get_time", "get_info", "get_system_state", "get_power", "list_services",
    }
    for fn in runtime_control.ACTIONS.values():
        assert not inspect.signature(fn).parameters


def test_meta_matches_service_meta_shape(bus):
    meta = runtime_control.build_meta()
    assert meta["topics"]["control"] == "/runtime/runtime/control"
    names = [m["name"] for m in meta["methods"]]
    assert names == list(runtime_control.ACTIONS)
    assert all(m["doc"] and m["args_schema"]["type"] == "object" for m in meta["methods"])


def test_get_time_fields():
    t = runtime_control.get_time()
    assert {"local", "spoken", "weekday", "timezone", "utc_offset", "utc", "unix"} <= set(t)
    assert t["weekday"] in t["spoken"]


def test_reads_retained_runtime_topics(bus):
    bus.publish_sync("/runtime/info", {"id": "droid-1", "version": "0.1.42",
                                       "started_at": "2026-10-03T20:00:00+00:00"}, retained=True)
    bus.publish_sync("/runtime/runtime/power", {"battery": {"percent": 80}}, retained=True)
    bus.publish_sync("/runtime/runtime/services", {"services": [
        {"proxy_id": "brain-1", "type": "brain", "version": "1.0.0", "transport": "in_process",
         "pid": 1, "status": "running"},
    ]}, retained=True)
    info = runtime_control.get_info()
    assert info["runtime_id"] == "droid-1" and info["botgear_version"] == "0.1.42"
    assert info["dev_build"] is False and info["uptime_s"] is not None
    assert runtime_control.get_power()["battery"]["percent"] == 80
    assert runtime_control.list_services() == {"count": 1, "services": [
        {"proxy_id": "brain-1", "type": "brain", "version": "1.0.0", "transport": "in_process"},
    ]}
    assert "error" in runtime_control.get_system_state()     # not published in this test


@pytest.mark.asyncio
async def test_control_request_reply_roundtrip(bus):
    runtime_control.start()
    try:
        assert bus.has_retained(runtime_control.META_TOPIC)
        replies = []

        async def _collect(topic):
            async for msg in bus.subscribe(topic, f"test-{topic}"):
                replies.append(msg.payload)
                return

        ok = asyncio.create_task(_collect("/test/reply/1"))
        bad = asyncio.create_task(_collect("/test/reply/2"))
        await asyncio.sleep(0.01)
        bus.publish_sync(runtime_control.CONTROL_TOPIC, {"action": "get_time", "reply_to": "/test/reply/1"})
        bus.publish_sync(runtime_control.CONTROL_TOPIC, {"action": "shutdown", "reply_to": "/test/reply/2"})
        await asyncio.wait_for(asyncio.gather(ok, bad), timeout=2)
        assert any("weekday" in r for r in replies)
        assert {"error": "unknown action: shutdown"} in replies
    finally:
        await runtime_control.stop()


@pytest.mark.asyncio
async def test_brain_tool_executor_calls_runtime(bus):
    """End to end: brain's ToolExecutor → /runtime/runtime/control → reply."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repo" / "brain" / "1.0.0"))
    from brain.tool_executor import ToolExecutor

    async def _sub(topic):
        async for msg in bus.subscribe(topic, f"brain-test-{topic}"):
            yield msg.payload

    executor = ToolExecutor(
        publish=lambda topic, payload, **_: bus.publish_sync(topic, payload),
        subscribe_reply=_sub, reply_root="/brain/brain-1/_replies",
    )
    runtime_control.start()
    await asyncio.sleep(0.01)          # let the control loop subscribe
    try:
        result = await executor.call(topic=runtime_control.CONTROL_TOPIC, action="get_time",
                                     args={}, timeout=2.0)
        assert result.status == "ok" and "weekday" in result.value
    finally:
        await runtime_control.stop()
