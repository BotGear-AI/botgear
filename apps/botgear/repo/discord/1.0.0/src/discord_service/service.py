# unmanaged
"""DiscordService — the `messaging` capability over Discord's Gateway.

See ``packages/rlx_messaging/README.md`` for the full design. All the
generic plumbing (inbox queue, listen/speak/send/list_channels, retained
state) lives in ``rlx_messaging.MessagingServiceBase`` — this module only
implements the Discord-specific hooks.

IMPORTANT: ``import discord`` is deliberately kept OUT of module scope
(it happens inside ``_connect``/``_send``/``_list_channels`` instead).
``discord.py`` is only installed in this service's own per-type venv,
not the main backend venv — but the backend still needs to import THIS
MODULE (just for ``DiscordConfig``) to resolve which config fields are
secret before publishing config to the bus (see
``botgear.runtime.boot.resolve_config_class`` /
``botgear.runtime.discovery._mask_secret_fields``). Keeping the SDK
import lazy is what makes that resolution succeed instead of raising
``ModuleNotFoundError`` in a venv that never has discord.py.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, ClassVar, Dict, List, Optional

from pydantic import Field, SecretStr, field_validator
from rlx_bus import service_method
from rlx_messaging import MessagingConfig, MessagingServiceBase


logger = logging.getLogger(__name__)

_READY_TIMEOUT_SECONDS = 30.0


class DiscordConfig(MessagingConfig):
    bot_token: Optional[SecretStr] = Field(
        None, description="Bot token from the Bot tab of your Discord application. "
                           "Enable the 'Message Content Intent' there too, or inbound text arrives empty."
    )
    guild_id: Optional[str] = Field(
        None, description="Narrows list_channels() to one server. Blank = every server the bot has joined."
    )
    allowed_sender_ids: Optional[List[str]] = Field(
        None, description="Discord user IDs (numeric, e.g. '777775417691078668') permitted to reach /inbox. "
                           "Any message — DM or guild channel — from a sender not in this list is dropped "
                           "before it ever hits the bus. Blank/None = unfiltered (every sender's messages pass "
                           "through), so leave this set for a personal-assistant bot others shouldn't be able "
                           "to inject messages into."
    )

    @field_validator("allowed_sender_ids", mode="before")
    @classmethod
    def _split_allowed_sender_ids(cls, value: Any) -> Any:
        """The wizard form submits this as one comma-separated string
        (no multi-value widget in wizard_config yet) — accept that shape
        alongside an already-normalized list."""
        if isinstance(value, str):
            ids = [v.strip() for v in value.split(",") if v.strip()]
            return ids or None
        return value


class DiscordService(MessagingServiceBase):
    type_name: ClassVar[str] = "discord"
    config_class: ClassVar[Any] = DiscordConfig
    capabilities: ClassVar[Dict[str, bool]] = {"listen": True, "speak": True, "channels": True}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client: Any = None
        self._client_task: Optional[asyncio.Task] = None

    async def _connect(self) -> None:
        import discord  # lazy — see module docstring

        token = self.config.bot_token.get_secret_value() if self.config.bot_token else None
        if not token:
            raise RuntimeError("discord: bot_token is not configured")

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        # client.wait_until_ready() races client.start()'s own internal setup:
        # if wait_until_ready() runs before client.start()'s task has taken a
        # single step, discord.py's self._ready is still None and it raises
        # "Client has not been properly initialised" immediately instead of
        # actually waiting. Track readiness ourselves via on_ready and race
        # that against the client task completing (e.g. a bad token raising
        # LoginFailure) so a real auth failure surfaces right away instead of
        # only after the full timeout.
        ready_event = asyncio.Event()

        @client.event
        async def on_ready() -> None:
            ready_event.set()

        @client.event
        async def on_message(message: "discord.Message") -> None:  # noqa: ANN001
            if message.author == client.user and not self.config.echo_own_messages:
                return
            allowed = self.config.allowed_sender_ids
            if allowed and str(message.author.id) not in allowed:
                logger.info(
                    "discord: dropped inbound message from non-allowed sender_id=%s",
                    message.author.id,
                )
                return
            await self._on_platform_message(
                message.content,
                sender=str(message.author),
                channel=str(message.channel.id),
            )

        self._client = client
        self._client_task = asyncio.create_task(client.start(token), name="discord.client")
        ready_wait = asyncio.create_task(ready_event.wait(), name="discord.ready_wait")
        done, _pending = await asyncio.wait(
            {ready_wait, self._client_task},
            timeout=_READY_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if ready_wait in done:
            return
        ready_wait.cancel()
        if self._client_task in done:
            exc = self._client_task.exception()
            await self._disconnect()
            if exc is not None:
                raise RuntimeError(f"discord: client.start() failed: {exc}") from exc
            raise RuntimeError("discord: client.start() exited before becoming ready")
        await self._disconnect()
        raise RuntimeError("discord: timed out waiting for Gateway ready (bad token?)")

    async def _disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001
                logger.exception("discord: client.close() raised")
        if self._client_task is not None and not self._client_task.done():
            self._client_task.cancel()
            try:
                await self._client_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._client = None
        self._client_task = None

    async def _send(self, channel: Optional[str], text: str) -> bool:
        if self._client is None or not channel:
            return False
        target = self._client.get_channel(int(channel))
        if target is None:
            target = await self._client.fetch_channel(int(channel))
        await target.send(text)
        return True

    async def _list_channels(self) -> List[Dict[str, str]]:
        if self._client is None:
            return []
        out: List[Dict[str, str]] = []
        for guild in self._client.guilds:
            if self.config.guild_id and str(guild.id) != str(self.config.guild_id):
                continue
            for channel in guild.text_channels:
                out.append({"id": str(channel.id), "name": f"{guild.name} / #{channel.name}"})
        return out

    @service_method("reconnect")
    async def reconnect(self) -> Dict[str, Any]:
        """Manual retry — e.g. after fixing a bad token via config
        update without wanting to release/recreate the whole proxy."""
        await self._disconnect()
        try:
            await self._connect()
            self._connected = True
        except Exception as exc:  # noqa: BLE001
            self._connected = False
            await self._publish_state()
            return {"connected": False, "error": str(exc)}
        await self._publish_state()
        return {"connected": True}
