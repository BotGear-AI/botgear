# unmanaged
"""SlackService — the `messaging` capability over Slack Socket Mode.

See ``packages/rlx_messaging/README.md`` for the full design. All the
generic plumbing (inbox queue, listen/speak/send/list_channels, retained
state) lives in ``rlx_messaging.MessagingServiceBase`` — this module only
implements the Slack-specific hooks.

IMPORTANT: ``import slack_sdk`` is deliberately kept OUT of module scope
(it happens inside ``_connect``/``_send``/``_list_channels`` instead) —
same reasoning as ``discord_service.service``: it lets the backend
resolve ``SlackConfig`` (for secret-field masking) without needing
``slack_sdk`` installed in the main venv.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, ClassVar, Dict, List, Optional

from pydantic import Field, SecretStr
from rlx_bus import service_method
from rlx_messaging import MessagingConfig, MessagingServiceBase


logger = logging.getLogger(__name__)


class SlackConfig(MessagingConfig):
    bot_token: Optional[SecretStr] = Field(
        None, description="xoxb-... bot token from OAuth & Permissions. Needs chat:write + channels:read scopes."
    )
    app_token: Optional[SecretStr] = Field(
        None, description="xapp-... app-level token from Basic Information -> App-Level Tokens. "
                           "Needs connections:write scope; Socket Mode must be enabled for the app."
    )


class SlackService(MessagingServiceBase):
    type_name: ClassVar[str] = "slack"
    config_class: ClassVar[Any] = SlackConfig
    capabilities: ClassVar[Dict[str, bool]] = {"listen": True, "speak": True, "channels": True}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._web_client: Any = None
        self._socket_client: Any = None
        self._bot_user_id: Optional[str] = None

    async def _connect(self) -> None:
        from slack_sdk.socket_mode.aiohttp import SocketModeClient
        from slack_sdk.socket_mode.request import SocketModeRequest
        from slack_sdk.socket_mode.response import SocketModeResponse
        from slack_sdk.web.async_client import AsyncWebClient

        bot_token = self.config.bot_token.get_secret_value() if self.config.bot_token else None
        app_token = self.config.app_token.get_secret_value() if self.config.app_token else None
        if not bot_token or not app_token:
            raise RuntimeError("slack: bot_token and app_token are both required")

        web_client = AsyncWebClient(token=bot_token)
        auth = await web_client.auth_test()
        self._bot_user_id = auth.get("user_id")

        socket_client = SocketModeClient(app_token=app_token, web_client=web_client)

        async def on_request(client: "SocketModeClient", req: "SocketModeRequest") -> None:  # noqa: ANN001
            # Ack immediately — Slack requires a response within 3s
            # regardless of how long our own handling takes.
            await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
            if req.type != "events_api":
                return
            event = (req.payload or {}).get("event") or {}
            if event.get("type") != "message" or event.get("subtype") is not None:
                return  # subtype covers edits/joins/etc — only plain messages
            if event.get("bot_id") and not self.config.echo_own_messages:
                return
            text = event.get("text")
            if not isinstance(text, str) or not text:
                return
            await self._on_platform_message(
                text, sender=event.get("user"), channel=event.get("channel"),
            )

        socket_client.socket_mode_request_listeners.append(on_request)
        await socket_client.connect()

        self._web_client = web_client
        self._socket_client = socket_client

    async def _disconnect(self) -> None:
        if self._socket_client is not None:
            try:
                await self._socket_client.disconnect()
            except Exception:  # noqa: BLE001
                logger.exception("slack: socket_client.disconnect() raised")
            try:
                await self._socket_client.close()
            except Exception:  # noqa: BLE001
                pass
        self._socket_client = None
        self._web_client = None

    async def _send(self, channel: Optional[str], text: str) -> bool:
        if self._web_client is None or not channel:
            return False
        resp = await self._web_client.chat_postMessage(channel=channel, text=text)
        return bool(resp.get("ok"))

    async def _list_channels(self) -> List[Dict[str, str]]:
        if self._web_client is None:
            return []
        resp = await self._web_client.conversations_list(types="public_channel,private_channel")
        return [
            {"id": c.get("id", ""), "name": c.get("name", "")}
            for c in (resp.get("channels") or [])
        ]

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
