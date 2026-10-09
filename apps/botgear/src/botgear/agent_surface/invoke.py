# unmanaged
"""Call a service action over the bus and await its reply.

    result = await executor.call(topic="/video/video-1/control",
                                 action="capture_frame", args={"camera": 0})

Publishes ``{action, **args, reply_to}`` to the control topic — the wire
contract every botgear service's control loop answers — and turns the
reply (or its absence) into a ToolResult. The executor is the only part
of the surface that touches the bus directly, so a consumer can be unit
tested by swapping in a stub whose ``call()`` returns scripted results.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, AsyncIterator, Callable, Dict

from botgear.agent_surface.models import ToolResult

logger = logging.getLogger(__name__)


class ToolExecutor:
    """Bus publish + reply_to round trip.

    Constructed with a ``publish`` callable (``bus.publish_sync``-shaped)
    and ``subscribe_reply`` (topic → async iterator of reply payloads,
    e.g. ``bus_reply_iter``)."""

    def __init__(
        self,
        *,
        publish: Callable[..., Any],
        subscribe_reply: Callable[[str], AsyncIterator[Any]],
        reply_root: str = "/brain/_replies",
    ):
        self._publish = publish
        self._subscribe_reply = subscribe_reply
        self._reply_root = reply_root.rstrip("/")

    async def call(
        self,
        *,
        topic: str,
        action: str,
        args: Dict[str, Any],
        timeout: float = 30.0,
    ) -> ToolResult:
        """``error`` and ``timeout`` come back in the result's ``status`` —
        this only raises on bus plumbing errors, never on remote failures."""
        reply_to = f"{self._reply_root}/{uuid.uuid4().hex[:16]}"
        payload = {"action": action, **args, "reply_to": reply_to}

        # Subscribe BEFORE publishing so a synchronous reply isn't missed.
        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()

        async def _consume():
            try:
                async for msg in self._subscribe_reply(reply_to):
                    if not future.done():
                        future.set_result(msg)
                    return
            except Exception as exc:  # noqa: BLE001
                if not future.done():
                    future.set_exception(exc)

        consume_task = asyncio.create_task(_consume())
        try:
            self._publish(topic, payload)
            t0 = time.monotonic()
            try:
                msg = await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                return ToolResult(
                    status="timeout",
                    error=f"no reply on {reply_to} within {timeout}s",
                    duration_ms=int((time.monotonic() - t0) * 1000),
                )
            duration_ms = int((time.monotonic() - t0) * 1000)
            # Normalise: {"error": "..."} → status=error
            if isinstance(msg, dict) and "error" in msg and msg.get("error"):
                return ToolResult(status="error", error=str(msg["error"]), duration_ms=duration_ms)
            return ToolResult(status="ok", value=msg, duration_ms=duration_ms)
        finally:
            consume_task.cancel()
            try:
                await consume_task
            except (asyncio.CancelledError, Exception):
                pass


async def bus_reply_iter(bus: Any, topic: str, *, subscriber_id: str) -> AsyncIterator[Any]:
    """``bus.subscribe`` → payloads; the ``subscribe_reply`` shape
    ToolExecutor wants for the in-process bus."""
    async for msg in bus.subscribe(topic, subscriber_id=subscriber_id):
        yield msg.payload if hasattr(msg, "payload") else msg
