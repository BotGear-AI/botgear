# unmanaged
"""Unit tests for rlx_messaging.MessagingServiceBase — the shared
plumbing discord/slack/google_chat all subclass.

Same fixture shape as test_keyboard_local.py: a minimal _FakeBus double
(no real websocket), direct ``ConcreteService(proxy_id, bus)``
construction, ``on_start()`` awaited explicitly so tests don't need the
full ``SubprocessService.run()`` boot sequence.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest
import pytest_asyncio

from rlx_messaging import MessagingServiceBase


class _FakeBus:
    def __init__(self) -> None:
        self.published: List[Dict[str, Any]] = []
        self._subs: Dict[str, Any] = {}

    async def publish(self, topic: str, payload: Any, *, retained: bool = False) -> None:
        self.published.append({"topic": topic, "payload": payload, "retained": retained})

    async def subscribe(self, topic: str, handler) -> None:
        self._subs[topic] = handler

    def by_topic_suffix(self, suffix: str) -> List[Dict[str, Any]]:
        return [m for m in self.published if m["topic"].endswith(suffix)]

    def latest(self, suffix: str) -> Optional[Dict[str, Any]]:
        matches = self.by_topic_suffix(suffix)
        return matches[-1]["payload"] if matches else None


class _TestMessagingService(MessagingServiceBase):
    type_name = "testmsg"
    capabilities = {"listen": True, "speak": True, "channels": True}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.sent: List[tuple] = []
        self.connect_calls = 0
        self.fail_connect = False

    async def _connect(self) -> None:
        self.connect_calls += 1
        if self.fail_connect:
            raise RuntimeError("boom")

    async def _send(self, channel: Optional[str], text: str) -> bool:
        self.sent.append((channel, text))
        return True

    async def _list_channels(self) -> List[Dict[str, str]]:
        return [{"id": "c1", "name": "general"}]


@pytest_asyncio.fixture
async def svc() -> _TestMessagingService:
    s = _TestMessagingService("proxy-1", _FakeBus())
    await s.on_start()
    return s


# ─── listen ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_listen_returns_message_queued_before_call(svc):
    await svc._on_inbox({"text": "hi", "sender": "u1", "channel": "c1"})
    result = await svc.listen(timeout_seconds=1)
    assert result == {"text": "hi", "sender": "u1", "channel": "c1", "bearing": None, "timeout": False}


@pytest.mark.asyncio
async def test_listen_times_out_without_raising(svc):
    result = await svc.listen(timeout_seconds=0.3)
    assert result["timeout"] is True
    assert result["text"] == ""


@pytest.mark.asyncio
async def test_listen_wakes_on_message_after_call(svc):
    async def _deliver_later():
        await asyncio.sleep(0.05)
        await svc._on_inbox({"text": "later", "channel": None})

    task = asyncio.create_task(_deliver_later())
    result = await svc.listen(timeout_seconds=2)
    await task
    assert result["text"] == "later"
    assert result["timeout"] is False


@pytest.mark.asyncio
async def test_listen_channel_filter_skips_other_channels(svc):
    await svc._on_inbox({"text": "for-c1", "channel": "c1"})
    await svc._on_inbox({"text": "for-c2", "channel": "c2"})
    result = await svc.listen(timeout_seconds=2, channel="c2")
    assert result == {"text": "for-c2", "sender": None, "channel": "c2", "bearing": None, "timeout": False}


@pytest.mark.asyncio
async def test_malformed_inbox_payload_dropped(svc):
    await svc._on_inbox("not a dict")
    await svc._on_inbox({"no_text_field": True})
    await svc._on_inbox({"text": 123})  # wrong type
    result = await svc.listen(timeout_seconds=0.3)
    assert result["timeout"] is True


# ─── speak / send ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_speak_calls_send_hook_and_publishes(svc):
    result = await svc.speak("hello", channel="c1")
    assert svc.sent == [("c1", "hello")]
    assert result == {"spoken": "hello", "ts": result["ts"], "channel": "c1", "sent": True}
    assert svc.bus.latest("/spoken") == {"text": "hello", "channel": "c1", "ts": result["ts"], "sent": True}


@pytest.mark.asyncio
async def test_speak_defaults_to_configured_channel(svc):
    svc.config.default_channel = "c9"
    result = await svc.speak("hi")
    assert svc.sent == [("c9", "hi")]
    assert result["channel"] == "c9"


@pytest.mark.asyncio
async def test_speak_defaults_to_last_inbound_channel(svc):
    """No explicit channel + no configured default_channel: fall back
    to the channel the most recent inbound message arrived on — lets a
    caller (LLM tool call, UI composer, script) reply correctly without
    threading channel through itself (the conversation_session gap
    this fixed)."""
    await svc._on_inbox({"text": "hi", "sender": "op", "channel": "dm-42"})
    result = await svc.speak("hello back")
    assert svc.sent == [("dm-42", "hello back")]
    assert result["channel"] == "dm-42"


@pytest.mark.asyncio
async def test_speak_explicit_channel_overrides_last_inbound(svc):
    await svc._on_inbox({"text": "hi", "sender": "op", "channel": "dm-42"})
    result = await svc.speak("hello elsewhere", channel="dm-99")
    assert svc.sent == [("dm-99", "hello elsewhere")]
    assert result["channel"] == "dm-99"


@pytest.mark.asyncio
async def test_speak_configured_default_channel_wins_over_last_inbound(svc):
    """default_channel is a deliberate operator setting; it should win
    over the ambient last-sender heuristic when both are available and
    no explicit channel is passed."""
    svc.config.default_channel = "c9"
    await svc._on_inbox({"text": "hi", "sender": "op", "channel": "dm-42"})
    result = await svc.speak("hi")
    assert svc.sent == [("c9", "hi")]
    assert result["channel"] == "c9"


@pytest.mark.asyncio
async def test_speak_rejects_non_str_text(svc):
    with pytest.raises(TypeError, match="must be str"):
        await svc.speak(123)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_speak_send_failure_reports_sent_false(svc):
    async def _boom(channel, text):
        raise RuntimeError("platform down")
    svc._send = _boom  # type: ignore[method-assign]
    result = await svc.speak("hi", channel="c1")
    assert result["sent"] is False


@pytest.mark.asyncio
async def test_send_injects_synthetic_inbound(svc):
    result = await svc.send("scripted operator turn", channel="c1")
    assert result == {"sent": "scripted operator turn"}
    inbox_msg = svc.bus.latest("/inbox")
    assert inbox_msg["text"] == "scripted operator turn"
    assert inbox_msg["channel"] == "c1"


@pytest.mark.asyncio
async def test_send_rejects_non_str_text(svc):
    with pytest.raises(TypeError, match="must be str"):
        await svc.send(None)  # type: ignore[arg-type]


# ─── list_channels ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_channels_delegates_when_capable(svc):
    result = await svc.list_channels()
    assert result == {"channels": [{"id": "c1", "name": "general"}]}


@pytest.mark.asyncio
async def test_list_channels_empty_when_not_capable():
    class _NoChannels(_TestMessagingService):
        capabilities = {"listen": True, "speak": True, "channels": False}
    s = _NoChannels("proxy-2", _FakeBus())
    await s.on_start()
    assert await s.list_channels() == {"channels": []}


# ─── connect / state ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connect_failure_does_not_crash_on_start():
    s = _TestMessagingService("proxy-3", _FakeBus())
    s.fail_connect = True
    await s.on_start()  # must not raise
    assert s._connected is False
    state = s.bus.latest("/state")
    assert state["connected"] is False
    assert state["capabilities"] == {"listen": True, "speak": True, "channels": True}


@pytest.mark.asyncio
async def test_state_shape_on_successful_connect(svc):
    state = svc.bus.latest("/state")
    assert state["connected"] is True
    assert state["listening"] is False
    assert state["queued"] == 0
    assert state["default_channel"] is None
