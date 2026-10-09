# unmanaged
"""Unit tests for google_chat_service — the send-only webhook
integration. No lazy-SDK seam needed (stdlib urllib only); we mock
``urllib.request.urlopen`` directly."""
from __future__ import annotations

import sys
import typing
from pathlib import Path
from typing import Any, Dict, List

import pytest
from pydantic import SecretStr

_GC_SRC = Path(__file__).resolve().parents[1] / "repo" / "google_chat" / "1.0.0" / "src"
if str(_GC_SRC) not in sys.path:
    sys.path.insert(0, str(_GC_SRC))


class _FakeBus:
    def __init__(self) -> None:
        self.published: List[Dict[str, Any]] = []

    async def publish(self, topic: str, payload: Any, *, retained: bool = False) -> None:
        self.published.append({"topic": topic, "payload": payload, "retained": retained})

    async def subscribe(self, topic: str, handler) -> None:
        pass


def test_config_secret_field_is_secretstr():
    from google_chat_service.service import GoogleChatConfig
    ann = GoogleChatConfig.model_fields["webhook_url"].annotation
    assert SecretStr in typing.get_args(ann) or ann is SecretStr
    c = GoogleChatConfig(webhook_url="https://chat.googleapis.com/v1/spaces/x/messages?key=y")
    assert c.model_dump(mode="json")["webhook_url"] == "**********"


def test_capabilities_are_send_only():
    from google_chat_service.service import GoogleChatService
    assert GoogleChatService.capabilities == {"listen": False, "speak": True, "channels": False}


@pytest.mark.asyncio
async def test_connect_raises_without_webhook_url():
    from google_chat_service.service import GoogleChatService
    svc = GoogleChatService("gc-1", _FakeBus())
    with pytest.raises(RuntimeError, match="webhook_url"):
        await svc._connect()


@pytest.mark.asyncio
async def test_connect_succeeds_with_webhook_url():
    from google_chat_service.service import GoogleChatService
    svc = GoogleChatService("gc-1", _FakeBus())
    svc.config.webhook_url = SecretStr("https://chat.googleapis.com/v1/spaces/x/messages?key=y")
    await svc._connect()  # must not raise


@pytest.mark.asyncio
async def test_send_posts_json_body(monkeypatch):
    from google_chat_service import service as gc_service

    captured: Dict[str, Any] = {}

    class _FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["headers"] = req.headers
        return _FakeResp()

    monkeypatch.setattr(gc_service.urllib.request, "urlopen", _fake_urlopen)

    svc = gc_service.GoogleChatService("gc-1", _FakeBus())
    svc.config.webhook_url = SecretStr("https://chat.googleapis.com/v1/spaces/x/messages?key=y")
    ok = await svc._send(None, "hello world")
    assert ok is True
    assert captured["url"] == "https://chat.googleapis.com/v1/spaces/x/messages?key=y"
    assert b"hello world" in captured["data"]


@pytest.mark.asyncio
async def test_send_false_on_network_error(monkeypatch):
    from google_chat_service import service as gc_service
    import urllib.error

    def _fake_urlopen(req, timeout):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(gc_service.urllib.request, "urlopen", _fake_urlopen)

    svc = gc_service.GoogleChatService("gc-1", _FakeBus())
    svc.config.webhook_url = SecretStr("https://chat.googleapis.com/v1/spaces/x/messages?key=y")
    assert await svc._send(None, "hi") is False


@pytest.mark.asyncio
async def test_send_false_without_webhook_configured():
    from google_chat_service.service import GoogleChatService
    svc = GoogleChatService("gc-1", _FakeBus())
    assert await svc._send(None, "hi") is False
