# unmanaged
"""Unit tests for slack_service. Same seam strategy as
test_discord_service.py — stub ``svc._web_client`` directly rather than
mocking the real ``slack_sdk`` package (not installed in the main venv
on purpose, see service.py's module docstring)."""
from __future__ import annotations

import sys
import typing
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from pydantic import SecretStr

_SLACK_SRC = Path(__file__).resolve().parents[1] / "repo" / "slack" / "1.0.0" / "src"
if str(_SLACK_SRC) not in sys.path:
    sys.path.insert(0, str(_SLACK_SRC))


class _FakeBus:
    def __init__(self) -> None:
        self.published: List[Dict[str, Any]] = []

    async def publish(self, topic: str, payload: Any, *, retained: bool = False) -> None:
        self.published.append({"topic": topic, "payload": payload, "retained": retained})

    async def subscribe(self, topic: str, handler) -> None:
        pass


def test_config_secret_fields_are_secretstr():
    from slack_service.service import SlackConfig
    for name in ("bot_token", "app_token"):
        ann = SlackConfig.model_fields[name].annotation
        assert SecretStr in typing.get_args(ann) or ann is SecretStr
    c = SlackConfig(bot_token="xoxb-1", app_token="xapp-1")
    dumped = c.model_dump(mode="json")
    assert dumped["bot_token"] == "**********"
    assert dumped["app_token"] == "**********"


class _FakeWebClient:
    def __init__(self) -> None:
        self.posted: List[Dict[str, Any]] = []
        self.ok = True

    async def chat_postMessage(self, channel: str, text: str) -> Dict[str, Any]:
        self.posted.append({"channel": channel, "text": text})
        return {"ok": self.ok}

    async def conversations_list(self, types: str) -> Dict[str, Any]:
        return {"channels": [{"id": "C1", "name": "general"}, {"id": "C2", "name": "random"}]}


@pytest.mark.asyncio
async def test_send_posts_message_and_returns_ok():
    from slack_service.service import SlackService
    svc = SlackService("slack-1", _FakeBus())
    fake = _FakeWebClient()
    svc._web_client = fake
    ok = await svc._send("C1", "hello")
    assert ok is True
    assert fake.posted == [{"channel": "C1", "text": "hello"}]


@pytest.mark.asyncio
async def test_send_no_channel_returns_false():
    from slack_service.service import SlackService
    svc = SlackService("slack-1", _FakeBus())
    svc._web_client = _FakeWebClient()
    assert await svc._send(None, "hi") is False


@pytest.mark.asyncio
async def test_send_reflects_platform_not_ok():
    from slack_service.service import SlackService
    svc = SlackService("slack-1", _FakeBus())
    fake = _FakeWebClient()
    fake.ok = False
    svc._web_client = fake
    assert await svc._send("C1", "hi") is False


@pytest.mark.asyncio
async def test_list_channels_maps_response():
    from slack_service.service import SlackService
    svc = SlackService("slack-1", _FakeBus())
    svc._web_client = _FakeWebClient()
    result = await svc._list_channels()
    assert result == [{"id": "C1", "name": "general"}, {"id": "C2", "name": "random"}]
