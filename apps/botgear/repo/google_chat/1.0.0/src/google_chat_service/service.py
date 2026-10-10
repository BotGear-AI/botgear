# unmanaged
"""GoogleChatService — the `messaging` capability's speak-only tier.

See ``packages/rlx_messaging/README.md`` ("Platforms") for why this is send-only
in v1 (no public HTTPS endpoint / GCP Pub/Sub project available to a
robot laptop on a home LAN). ``capabilities.listen`` is False —
``listen()`` (inherited from ``MessagingServiceBase`` unmodified) simply
always times out, since nothing ever calls ``_on_platform_message``.

No lazy-import trick needed here (unlike discord/slack) — the webhook
POST uses only the stdlib, so this module has no third-party import to
defer in the first place. That also means the whole per-type venv this
service gets has zero third-party dependencies.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any, ClassVar, Dict, Optional

from pydantic import Field, SecretStr
from rlx_messaging import MessagingConfig, MessagingServiceBase


logger = logging.getLogger(__name__)

_POST_TIMEOUT_SECONDS = 10.0


class GoogleChatConfig(MessagingConfig):
    webhook_url: Optional[SecretStr] = Field(
        None, description="Incoming webhook URL from the target Chat space "
                           "(Apps & integrations -> Manage webhooks). Bearer-equivalent — keep it secret."
    )


class GoogleChatService(MessagingServiceBase):
    type_name: ClassVar[str] = "google_chat"
    config_class: ClassVar[Any] = GoogleChatConfig
    capabilities: ClassVar[Dict[str, bool]] = {"listen": False, "speak": True, "channels": False}

    async def _connect(self) -> None:
        # No persistent connection for a webhook — just validate config
        # up front so a missing URL surfaces at start time, not on the
        # first speak() call.
        if not self.config.webhook_url or not self.config.webhook_url.get_secret_value():
            raise RuntimeError("google_chat: webhook_url is not configured")

    async def _disconnect(self) -> None:
        pass  # nothing to close

    async def _send(self, channel: Optional[str], text: str) -> bool:
        del channel  # v1: one webhook = one space, no routing
        if not self.config.webhook_url:
            return False
        url = self.config.webhook_url.get_secret_value()
        return await asyncio.to_thread(self._post_webhook, url, text)

    @staticmethod
    def _post_webhook(url: str, text: str) -> bool:
        body = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json; charset=UTF-8"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_POST_TIMEOUT_SECONDS) as resp:
                return 200 <= resp.status < 300
        except urllib.error.URLError:
            logger.exception("google_chat: webhook POST failed")
            return False

    # list_channels stays the base's default {"channels": []} —
    # capabilities.channels is False, so it's never called for real.
