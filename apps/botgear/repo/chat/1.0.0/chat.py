# unmanaged
"""ChatService — text bridge for the brain's conversation workflows.

The brain's bundled ``conversation_session`` workflow was designed
against a speech service exposing ``listen`` + ``speak``. This is the
text-only equivalent — same method names + return shapes, but messages
travel as plain text over the bus instead of audio. Useful for
developing conversation workflows on a dev box without hooking up STT
+ TTS.

Implements the ``messaging`` capability (``implements: [messaging]`` in
package.yml — see ``docs/TODO_MESSAGING.md``), the same wire contract
``discord``/``slack``/``google_chat`` implement, so any of them can
stand in for chat in a brain workflow. chat satisfies it by hand (it's
in-process, not a subclass of ``rlx_messaging.MessagingServiceBase`` —
the capability is a wire contract, not a shared base class) with
``channel``/``list_channels`` as inert shape-parity stubs, since chat
has no channel concept.

Wire shape is declared below in code, not documented here:
``topic_schemas`` for what this service PUBLISHES (``spoken``,
``state``), ``subscribes_schemas`` for what it CONSUMES (``inbox`` —
operator utterances: UI text box, ``rlx publish``, etc.). Both are
introspected into this type's descriptor at
``/runtime/runtime/types/chat``. ``control`` actions (listen/speak/
send/list_channels) are discoverable the same way, off their
@service_method signatures.

Why ``listen()`` blocks instead of streaming
--------------------------------------------
The brain's tool-call model is request/response — one call returns one
ToolResult. A streaming subscription wouldn't fit the existing engine.
``listen()`` therefore parks on an inbox queue until a message arrives
or its timeout fires, then returns the next utterance as a tool
result. The brain's workflow drives the conversation loop by calling
``listen()`` repeatedly.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict
from rlx_bus import ServiceConfig
from botgear.framework import Service, service_method


logger = logging.getLogger(__name__)


class ChatConfig(ServiceConfig):
    """``default_channel``/``echo_own_messages`` exist purely for shape
    parity with ``rlx_messaging.MessagingConfig`` (see
    docs/TODO_MESSAGING.md) — chat has no channel concept, so
    ``default_channel`` is always ``None`` and ignored. Kept so a future
    generic "messaging config" UI panel doesn't need a chat-specific
    exception."""
    default_channel: Optional[str] = None
    echo_own_messages: bool = False


# ── wire schemas for what this service PUBLISHES ──────────────────────

class SpokenPayload(BaseModel):
    """Published on ``/chat/{id}/spoken`` when ``speak()`` is called."""
    text: str
    ts: float


class ChatState(BaseModel):
    """Retained on ``/chat/{id}/state`` — UI reads it without
    subscribing to every event topic."""
    connected: bool
    listening: bool
    queued: int
    last_inbox: str
    last_spoken: str
    default_channel: Optional[str]
    capabilities: Dict[str, bool]


# ── wire schema for what this service SUBSCRIBES to (inbound) ─────────

class InboxPayload(BaseModel):
    """Consumed from ``/chat/{id}/inbox`` — operator utterances (UI text
    box, ``rlx publish``, etc.). Only ``text`` is read by
    ``_inbox_loop``; extra fields are accepted and ignored there, so
    this model allows them too rather than rejecting a payload the
    runtime itself wouldn't reject."""
    model_config = ConfigDict(extra="allow")
    text: str
    ts: Optional[float] = None


class ChatService(Service):
    config_class = ChatConfig
    state_schema = ChatState
    topic_schemas = {"spoken": SpokenPayload}
    subscribes_schemas = {"inbox": InboxPayload}
    publishes = ["spoken", "state"]
    _inbox_queue: "asyncio.Queue[Dict[str, Any]]"
    _inbox_task: Optional[asyncio.Task]
    _controller_task: Optional[asyncio.Task]
    _last_inbox: str
    _last_spoken: str
    _listening: bool

    async def on_start(self) -> None:
        # The queue holds operator messages that arrived since the last
        # ``listen()`` returned. Bounded to keep a runaway publisher
        # from eating memory; oldest messages drop on overflow.
        self._inbox_queue = asyncio.Queue(maxsize=64)
        self._last_inbox = ""
        self._last_spoken = ""
        self._listening = False
        self._publish_state()
        self._inbox_task = asyncio.create_task(self._inbox_loop())
        self._controller_task = asyncio.create_task(self.run_control_loop())

    async def on_stop(self) -> None:
        for task in (self._inbox_task, self._controller_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(t for t in (self._inbox_task, self._controller_task) if t is not None),
            return_exceptions=True,
        )

    # ─── @service_method actions ────────────────────────────────────────

    @service_method("listen", publishes=["state"])
    async def listen(
        self,
        timeout_seconds: int = 8,
        channel: Optional[str] = None,
        bearing: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Pull one operator utterance off the inbox queue. Blocks for
        up to ``timeout_seconds`` waiting for a message; returns an
        empty utterance on timeout so the workflow can decide whether
        to retry or terminate (the brain's conversation_session exits
        after two timeouts in a row).

        ``channel`` is accepted for shape parity with the ``messaging``
        capability (docs/TODO_MESSAGING.md) — chat has no channels, so
        it's ignored and always echoed back as ``None``. ``bearing`` is
        reserved — the speech service would report an angle to the
        speaker; chat has no audio so it's None. Both are kept in the
        signature so the brain's prompt + workflows transfer verbatim
        across every ``messaging`` implementor.

        Returns ``{"text": str, "sender": None, "channel": None,
        "bearing": float | None, "timeout": bool}``."""
        del bearing, channel  # accepted for API parity, unused in text chat
        try:
            timeout = max(0.5, float(timeout_seconds))
        except (TypeError, ValueError):
            timeout = 8.0
        self._listening = True
        self._publish_state()
        try:
            msg = await asyncio.wait_for(self._inbox_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            self._listening = False
            self._publish_state()
            return {"text": "", "sender": None, "channel": None, "bearing": None, "timeout": True}
        text = str(msg.get("text", "")).strip()
        self._last_inbox = text
        self._listening = False
        self._publish_state()
        return {"text": text, "sender": None, "channel": None, "bearing": None, "timeout": False}

    @service_method("speak", publishes=["spoken", "state"])
    def speak(self, text: str, channel: Optional[str] = None) -> Dict[str, Any]:
        """Emit a reply. Publishes to ``/chat/{id}/spoken`` so the UI
        renders it. Returns immediately — text "speech" has no
        playback to wait for, so the brain workflow's
        ``await speak; await listen`` ordering simply means "publish,
        then go back to listening".

        ``channel`` is accepted for ``messaging``-capability shape
        parity and ignored (chat has no channels).

        Returns ``{"spoken": str, "ts": float, "channel": None,
        "sent": True}`` so callers can log what they emitted."""
        del channel
        if not isinstance(text, str):
            raise TypeError(f"speak.text must be str, got {type(text).__name__}")
        ts = time.time()
        self._last_spoken = text
        self.publish("spoken", SpokenPayload(text=text, ts=ts).model_dump())
        self._publish_state()
        return {"spoken": text, "ts": ts, "channel": None, "sent": True}

    @service_method("send")
    def send(self, text: str, channel: Optional[str] = None) -> Dict[str, Any]:
        """Back-channel for tests + manual exercises. Equivalent to
        publishing ``{"text": text}`` to ``/chat/{id}/inbox`` directly.
        ``channel`` is accepted for shape parity and ignored.

        Useful when you want to script an operator turn without the UI
        — e.g. ``rlx call /chat/chat-1/control send text="hi"``."""
        del channel
        if not isinstance(text, str):
            raise TypeError(f"send.text must be str, got {type(text).__name__}")
        self.publish("inbox", {"text": text, "ts": time.time()})
        return {"sent": text}

    @service_method("list_channels")
    def list_channels(self) -> Dict[str, Any]:
        """chat has exactly one implicit channel — returned as a
        synthetic entry so a ``messaging``-capability consumer that
        always calls ``list_channels()`` doesn't need a chat-specific
        branch."""
        return {"channels": [{"id": "local", "name": "local"}]}

    # ─── internals ──────────────────────────────────────────────────────

    def _publish_state(self) -> None:
        """One retained state message — UI reads it to render the
        current chat status without subscribing to every event topic."""
        state = ChatState(
            connected=True,  # chat is local — always "connected"
            listening=self._listening,
            queued=self._inbox_queue.qsize() if hasattr(self, "_inbox_queue") else 0,
            last_inbox=self._last_inbox,
            last_spoken=self._last_spoken,
            default_channel=None,
            capabilities={"listen": True, "speak": True, "channels": False},
        )
        self.publish("state", state.model_dump(), retained=True)

    async def _inbox_loop(self) -> None:
        """Drain ``/chat/{id}/inbox`` into the queue. Anything that
        isn't a dict with a ``text`` field is dropped — silently for
        now; switch to a warn-log if operators start mis-shaping
        messages frequently."""
        async for msg in self.subscribe_iter("inbox"):
            payload = msg.payload if isinstance(msg.payload, dict) else None
            if not payload or not isinstance(payload.get("text"), str):
                continue
            try:
                self._inbox_queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop the oldest to make room — the operator just
                # spoke and they care more about the latest message
                # than a stale one from before listen() was bound.
                try:
                    self._inbox_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self._inbox_queue.put_nowait(payload)
            self._publish_state()
