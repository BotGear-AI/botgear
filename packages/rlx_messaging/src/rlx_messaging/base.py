# unmanaged
"""MessagingServiceBase — shared plumbing for the `messaging` capability.

See ``packages/rlx_messaging/README.md`` for the full design. Mirrors
the precedent set by ``rlx_audio.SpeechServiceBase`` and
``rlx_input.KeyboardServiceBase``: this base owns the generic
request/response + inbox-queue + retained-state machinery; a concrete
engine (discord/slack/google_chat) implements a handful of small I/O
hooks (``_connect``/``_disconnect``/``_send``/``_list_channels``).

Wire contract (the ``messaging`` capability, ``implements: [messaging]``):

  listen(timeout_seconds=8, channel=None)
      -> {"text": str, "sender": str|None, "channel": str|None,
          "bearing": None, "timeout": bool}
  speak(text, channel=None)
      -> {"spoken": str, "ts": float, "channel": str|None, "sent": bool}
  send(text, channel=None)      # manual/test back-channel only — injects
      -> {"sent": text}          # a synthetic inbound message, does NOT
                                   # call the platform.
  list_channels()
      -> {"channels": [{"id": str, "name": str}]}

Topic shapes (``inbox`` in AND out, ``spoken``, retained ``state``) are
declared below as ``InboxMessage``/``SpokenMessage``/``MessagingState``
and introspected into each engine's ``/runtime/runtime/types/<type>``.

Inbound messages have exactly ONE entry point regardless of source: the
service's own ``/inbox`` topic. A UI "simulate inbound" button and a real
platform event both funnel through it — the UI publishes directly (same
as chat's UI does today), and a subclass's gateway/socket handler calls
``_on_platform_message(...)`` which just publishes to the same topic.
This keeps a single code path AND means the brain's raw
``/{type}/+/inbox`` subscription (``exit_on_phrase``) sees genuine
platform messages exactly like it sees chat's operator messages today —
no special-casing needed on the consumer side.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, ClassVar, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field
from rlx_bus import ServiceConfig, SubprocessService, service_method


logger = logging.getLogger(__name__)

_INBOX_MAXSIZE = 64


class MessagingConfig(ServiceConfig):
    """Common fields every messaging platform config subclasses. A
    concrete service (discord/slack/google_chat) adds its own auth
    fields (bot tokens, webhook URL — see each service's own Config for
    why those are ``SecretStr``, not plain ``str``)."""

    default_channel: Optional[str] = Field(
        None, description="Channel/room speak() targets when the caller doesn't pass one explicitly."
    )
    echo_own_messages: bool = Field(
        False,
        description="Whether messages this bot itself sends should also loop back onto /inbox. "
                    "Almost always False — True causes a feedback loop with anything listening on /inbox.",
    )


# ── wire schemas ──────────────────────────────────────────────────────

class InboxMessage(BaseModel):
    """``/inbox`` — the single funnel for inbound messages, both
    published (real platform events) and consumed (UI "simulate inbound",
    ``send()``). Only ``text`` is required to be accepted."""
    model_config = ConfigDict(extra="allow")
    text: str
    sender: Optional[str] = None
    channel: Optional[str] = None
    ts: Optional[float] = None


class SpokenMessage(BaseModel):
    """Published on ``/spoken`` after every ``speak()``."""
    text: str
    channel: Optional[str]
    ts: float
    sent: bool = Field(description="False when the platform send failed")


class MessagingState(BaseModel):
    """Retained ``/state``."""
    connected: bool
    listening: bool
    queued: int
    last_inbox: str
    last_spoken: str
    default_channel: Optional[str]
    last_channel: Optional[str]
    capabilities: Dict[str, bool] = Field(description="What this engine can really do: listen / speak / channels")


class MessagingServiceBase(SubprocessService):
    """Subclass and implement ``_connect``/``_disconnect``/``_send``
    (+ optionally ``_list_channels``). Declare ``capabilities`` to tell
    consumers honestly what this engine can actually do — a webhook-only
    integration (Google Chat v1) sets ``listen: False`` rather than
    silently hanging every ``listen()`` call forever."""

    config_class: ClassVar[Any] = MessagingConfig
    publishes: ClassVar[List[str]] = ["inbox", "spoken", "state"]
    state_schema: ClassVar = MessagingState
    topic_schemas: ClassVar[Dict[str, Any]] = {"inbox": InboxMessage, "spoken": SpokenMessage}
    subscribes_schemas: ClassVar[Dict[str, Any]] = {"inbox": InboxMessage}
    capabilities: ClassVar[Dict[str, bool]] = {"listen": True, "speak": True, "channels": False}

    async def on_start(self) -> None:
        self._inbox_queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=_INBOX_MAXSIZE)
        self._last_inbox: str = ""
        self._last_spoken: str = ""
        self._last_channel: Optional[str] = None
        self._listening: bool = False
        self._connected: bool = False
        await self.subscribe("inbox", self._on_inbox)
        try:
            await self._connect()
            self._connected = True
        except Exception:  # noqa: BLE001 — bad creds / network down must not crash boot
            logger.exception("%s: _connect failed — starting disconnected", self._type_name)
            self._connected = False
        await self._publish_state()

    async def on_stop(self) -> None:
        try:
            await self._disconnect()
        except Exception:  # noqa: BLE001
            logger.exception("%s: _disconnect raised", self._type_name)

    # ─── @service_method actions (shared by every implementor) ───────────

    @service_method("listen", publishes=["state"])
    async def listen(
        self,
        timeout_seconds: float = 8,
        channel: Optional[str] = None,
        bearing: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Pull one inbound message. Blocks up to ``timeout_seconds``;
        returns ``timeout: True`` (never raises) when nothing arrives —
        same non-throwing-timeout contract ``chat.listen()`` already has,
        so the brain's ``loop_on_timeout`` behavior is unchanged.

        ``channel``, if given, filters to that channel only — non-matching
        messages queued in the meantime are dropped (v1: one shared
        queue, not a per-channel one; a busy multi-channel bot with a
        channel-scoped listener will lose interleaved traffic from other
        channels while waiting, same tradeoff chat already accepts for
        malformed inbox payloads)."""
        del bearing  # API parity only — see chat.listen's docstring
        try:
            deadline_s = max(0.5, float(timeout_seconds))
        except (TypeError, ValueError):
            deadline_s = 8.0
        self._listening = True
        await self._publish_state()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + deadline_s
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                msg = await asyncio.wait_for(self._inbox_queue.get(), timeout=remaining)
                if channel is not None and msg.get("channel") not in (None, channel):
                    continue  # not our channel — drop and keep waiting
                self._last_inbox = str(msg.get("text", ""))
                self._listening = False
                await self._publish_state()
                return {
                    "text": self._last_inbox,
                    "sender": msg.get("sender"),
                    "channel": msg.get("channel"),
                    "bearing": None,
                    "timeout": False,
                }
        except asyncio.TimeoutError:
            self._listening = False
            await self._publish_state()
            return {"text": "", "sender": None, "channel": channel, "bearing": None, "timeout": True}

    @service_method("speak", publishes=["spoken", "state"])
    async def speak(self, text: str, channel: Optional[str] = None) -> Dict[str, Any]:
        """Send ``text`` out to the platform (via the subclass's
        ``_send`` hook) and publish it to ``/spoken`` for the UI. A
        platform-level send failure (rate limit, network) does NOT raise
        — it comes back as ``sent: False`` so a calling workflow can
        decide what to do, same as any other soft failure. A bad
        ``text`` type still raises ``TypeError``, matching chat.

        ``channel`` resolution order: explicit argument > configured
        ``default_channel`` > the channel the most recent inbound message
        arrived on. That last fallback is what makes "reply to whoever
        just DMed" work without the caller (an LLM tool call, the UI's
        composer, a script) having to thread ``channel`` through by
        hand — a caller that never mentions ``channel`` still replies to
        the right place as long as a message has come in since start."""
        if not isinstance(text, str):
            raise TypeError(f"speak.text must be str, got {type(text).__name__}")
        target = channel or self.config.default_channel or self._last_channel
        sent = False
        try:
            sent = bool(await self._send(target, text))
        except Exception:  # noqa: BLE001
            logger.exception("%s: _send failed", self._type_name)
            sent = False
        ts = time.time()
        self._last_spoken = text
        await self.publish("spoken", SpokenMessage(text=text, channel=target, ts=ts, sent=sent).model_dump())
        await self._publish_state()
        return {"spoken": text, "ts": ts, "channel": target, "sent": sent}

    @service_method("send")
    async def send(self, text: str, channel: Optional[str] = None) -> Dict[str, Any]:
        """Manual/test back-channel — injects a SYNTHETIC inbound message
        onto ``/inbox``, exactly like chat's existing ``send()``. Does
        NOT talk to the platform; it's for scripting an "operator turn"
        without a live Discord/Slack message, same use case chat's
        docstring describes."""
        if not isinstance(text, str):
            raise TypeError(f"send.text must be str, got {type(text).__name__}")
        await self.publish("inbox", InboxMessage(text=text, sender=None, channel=channel, ts=time.time()).model_dump())
        return {"sent": text}

    @service_method("list_channels")
    async def list_channels(self) -> Dict[str, Any]:
        if not self.capabilities.get("channels"):
            return {"channels": []}
        try:
            channels = await self._list_channels()
        except Exception:  # noqa: BLE001
            logger.exception("%s: _list_channels failed", self._type_name)
            channels = []
        return {"channels": channels}

    # ─── hooks a concrete engine implements ───────────────────────────────

    async def _connect(self) -> None:
        """Open the platform connection (Gateway/Socket Mode) or, for a
        webhook-only integration, just validate config. Default: no-op
        (treated as immediately connected) — a webhook-only engine can
        rely on this default."""

    async def _disconnect(self) -> None:
        """Close whatever ``_connect`` opened. Default: no-op."""

    async def _send(self, channel: Optional[str], text: str) -> bool:
        """Actually deliver ``text`` to the platform. Return True on
        success. Every concrete engine must override this — the base
        has no transport of its own."""
        raise NotImplementedError

    async def _list_channels(self) -> List[Dict[str, str]]:
        """Only called when ``capabilities['channels']`` is True."""
        return []

    # ─── called by a subclass's gateway/socket event handler ─────────────

    async def _on_platform_message(
        self, text: str, sender: Optional[str] = None, channel: Optional[str] = None
    ) -> None:
        """Call this when the platform delivers a real inbound message
        (a Discord/Slack event handler, etc). Publishes onto ``/inbox``
        — the single funnel every inbound message passes through, so it
        reaches ``listen()`` and any raw ``/inbox`` subscriber (the
        brain's ``exit_on_phrase`` watcher) identically to a UI-simulated
        message."""
        msg = InboxMessage(text=text, sender=sender, channel=channel, ts=time.time())
        await self.publish("inbox", msg.model_dump())

    # ─── internals ─────────────────────────────────────────────────────

    async def _on_inbox(self, payload: Any) -> None:
        """The single funnel every inbound message passes through — see
        the module docstring. Malformed payloads are dropped silently,
        matching chat's existing behavior."""
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            return
        channel = payload.get("channel")
        if channel is not None:
            # Tracked as soon as a message arrives (not just when listen()
            # eventually consumes it) so speak()'s fallback is live even
            # for a caller that never calls listen() itself.
            self._last_channel = channel
        try:
            self._inbox_queue.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                self._inbox_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._inbox_queue.put_nowait(payload)
        await self._publish_state()

    def _snapshot(self) -> Dict[str, Any]:
        state = MessagingState(
            connected=self._connected,
            listening=self._listening,
            queued=self._inbox_queue.qsize() if hasattr(self, "_inbox_queue") else 0,
            last_inbox=self._last_inbox if hasattr(self, "_last_inbox") else "",
            last_spoken=self._last_spoken if hasattr(self, "_last_spoken") else "",
            default_channel=self.config.default_channel,
            last_channel=self._last_channel if hasattr(self, "_last_channel") else None,
            capabilities=dict(self.capabilities),
        )
        return state.model_dump()

    async def _publish_state(self) -> None:
        await self.publish("state", self._snapshot(), retained=True)
