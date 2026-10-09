# unmanaged
"""Unit tests for discord_service.

``discord.py`` is NOT installed in the main app venv on purpose (see
service.py's module docstring — the SDK import is deliberately lazy so
the backend can resolve DiscordConfig for secret-field masking without
it). These tests exercise the orchestration logic (_send/_list_channels)
by stubbing ``svc._client`` directly rather than importing/mocking the
real ``discord`` package.
"""
from __future__ import annotations

import sys
import typing
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from pydantic import SecretStr

_DISCORD_SRC = Path(__file__).resolve().parents[1] / "repo" / "discord" / "1.0.0" / "src"
if str(_DISCORD_SRC) not in sys.path:
    sys.path.insert(0, str(_DISCORD_SRC))


class _FakeBus:
    def __init__(self) -> None:
        self.published: List[Dict[str, Any]] = []

    async def publish(self, topic: str, payload: Any, *, retained: bool = False) -> None:
        self.published.append({"topic": topic, "payload": payload, "retained": retained})

    async def subscribe(self, topic: str, handler) -> None:
        pass


def test_config_secret_field_is_secretstr():
    from discord_service.service import DiscordConfig
    ann = DiscordConfig.model_fields["bot_token"].annotation
    assert SecretStr in typing.get_args(ann) or ann is SecretStr
    c = DiscordConfig(bot_token="abc123")
    assert c.model_dump(mode="json")["bot_token"] == "**********"  # masked on dump
    assert c.bot_token.get_secret_value() == "abc123"  # real value still reachable in-process


def test_config_defaults():
    from discord_service.service import DiscordConfig
    c = DiscordConfig()
    assert c.bot_token is None
    assert c.guild_id is None
    assert c.default_channel is None


class _FakeChannel:
    def __init__(self, cid: int, name: str = "general") -> None:
        self.id = cid
        self.name = name
        self.sent: List[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


class _FakeGuild:
    def __init__(self, gid: int, name: str, channels: List[_FakeChannel]) -> None:
        self.id = gid
        self.name = name
        self.text_channels = channels


class _FakeClient:
    def __init__(self, guilds: List[_FakeGuild]) -> None:
        self.guilds = guilds
        self._channels = {c.id: c for g in guilds for c in g.text_channels}

    def get_channel(self, cid: int):
        return self._channels.get(cid)

    async def fetch_channel(self, cid: int):
        return self._channels.get(cid)


@pytest.mark.asyncio
async def test_send_uses_get_channel_and_calls_send():
    from discord_service.service import DiscordService
    ch = _FakeChannel(123)
    svc = DiscordService("discord-1", _FakeBus())
    svc._client = _FakeClient([_FakeGuild(1, "guild1", [ch])])
    ok = await svc._send("123", "hello")
    assert ok is True
    assert ch.sent == ["hello"]


@pytest.mark.asyncio
async def test_send_no_channel_returns_false():
    from discord_service.service import DiscordService
    svc = DiscordService("discord-1", _FakeBus())
    svc._client = _FakeClient([])
    assert await svc._send(None, "hi") is False


@pytest.mark.asyncio
async def test_list_channels_filters_by_guild_id():
    from discord_service.service import DiscordService
    ch1 = _FakeChannel(1)
    ch2 = _FakeChannel(2)
    svc = DiscordService("discord-1", _FakeBus())
    svc._client = _FakeClient([
        _FakeGuild(10, "alpha", [ch1]),
        _FakeGuild(20, "beta", [ch2]),
    ])
    svc.config.guild_id = "20"
    result = await svc._list_channels()
    assert result == [{"id": "2", "name": "beta / #general"}]


@pytest.mark.asyncio
async def test_list_channels_no_filter_returns_all():
    from discord_service.service import DiscordService
    ch1 = _FakeChannel(1)
    ch2 = _FakeChannel(2)
    svc = DiscordService("discord-1", _FakeBus())
    svc._client = _FakeClient([
        _FakeGuild(10, "alpha", [ch1]),
        _FakeGuild(20, "beta", [ch2]),
    ])
    result = await svc._list_channels()
    assert {c["id"] for c in result} == {"1", "2"}
