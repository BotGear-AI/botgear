"""OscopeService — an oscilloscope for the bus.

Subprocess service. Each *channel* binds to a generic ``(source_topic,
field)`` so the scope can plot ANY publisher — arduino/raspi pins,
joystick axes, siggen waves, sensors. The service subscribes to the
sources, timestamps + ring-buffers each sample, evaluates an edge/level
trigger, PUBLISHES trigger events on the bus (so other services can react
to a trigger the way real gear uses a trigger-out), and batches new
samples into a small frame at ``frame_hz`` for the UI to render.

Design notes
------------
* The scope is a pure SUBSCRIBER — a source is responsible for streaming
  (arduino: poll_pin; raspi: poll_pin; siggen: always; joystick: poll).
* Sample timing uses the source's ``ts`` when present + plausible, else
  arrival time — robust to sources that don't stamp and to clock skew.
* Frames carry only NEW samples since the last frame (delta streaming),
  so a fast source can't flood the bus with redundant windows.

Bus topics
----------
  /oscope/{id}/state      retained — channels + timebase + trigger + last trigger
  /oscope/{id}/control    incoming @service_method actions
  /oscope/{id}/heartbeat  1Hz (base-class)
  /oscope/{id}/frame      ~frame_hz — {t, seq, running, ch:{id:[[t,v]…]}, trig}
  /oscope/{id}/trigger    trigger events — {channel_id, t, value, edge, direction, seq}
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from rlx_bus import SubprocessService, service_method

from .model import Channel, OscopeConfig, Trigger

logger = logging.getLogger(__name__)

# Default trace colours handed out to new channels in order.
_PALETTE = ["#38bdf8", "#4ade80", "#f59e0b", "#f87171",
            "#a78bfa", "#f472b6", "#22d3ee", "#a3e635"]


def resolve_field(payload: Any, field: str) -> Optional[float]:
    """Extract a numeric value from ``payload`` at dotted ``field``.

    Supports dict keys + list indices (``value``, ``axes.0``,
    ``pins.5.value``). Bools coerce to 0/1. Returns None if the path is
    missing or the leaf isn't numeric.
    """
    if field in (None, "", "."):
        cur: Any = payload
    else:
        cur = payload
        for key in str(field).split("."):
            if isinstance(cur, dict):
                cur = cur.get(key)
            elif isinstance(cur, (list, tuple)):
                try:
                    cur = cur[int(key)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
    if isinstance(cur, bool):
        return 1.0 if cur else 0.0
    if isinstance(cur, (int, float)):
        v = float(cur)
        # Python's json emits Infinity/NaN (non-standard) — a source could
        # deliver a non-finite value that would smear the trace/trigger. Drop it.
        return v if math.isfinite(v) else None
    return None


class OscopeService(SubprocessService):
    type_name = "oscope"
    heartbeat_interval_s = 1.0
    config_class = OscopeConfig
    publishes = ["state", "frame", "trigger"]

    def __init__(self, proxy_id: str, bus) -> None:
        super().__init__(proxy_id, bus)
        self._buffers: Dict[str, Deque[Tuple[float, float]]] = {}   # cid -> (t, v)
        self._new: Dict[str, List[Tuple[float, float]]] = {}        # cid -> new since frame
        self._topic_channels: Dict[str, Set[str]] = {}             # topic -> {cid}
        self._subscribed: Set[str] = set()                         # topics we've subscribed to
        self._trig_prev: Optional[float] = None                    # prev trigger-channel value
        self._last_trig_t: float = float("-inf")                   # holdoff clock
        self._trig_seq: int = 0
        self._last_trigger: Optional[Dict[str, Any]] = None
        self._frame_trig: Optional[Dict[str, Any]] = None
        self._frame_seq: int = 0
        self._single_stop_at: Optional[float] = None
        self._max_t: float = float("-inf")                         # newest sample time (for pruning)
        self._frame_task: Optional[asyncio.Task] = None

    # ─── lifecycle ───────────────────────────────────────────────────
    async def on_start(self) -> None:
        self._rebuild_routing()
        await self._ensure_subs()
        self._frame_task = asyncio.get_event_loop().create_task(self._frame_loop())
        await self.publish_state()

    async def on_stop(self) -> None:
        if self._frame_task is not None and not self._frame_task.done():
            self._frame_task.cancel()
            try:
                await self._frame_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._frame_task = None

    # ─── source subscription (router — the bus has no unsubscribe, so we
    #     subscribe once per topic and route to every channel on it) ────
    def _rebuild_routing(self) -> None:
        self._topic_channels = {}
        for ch in self.config.channels:
            self._topic_channels.setdefault(ch.source_topic, set()).add(ch.id)
            self._buffers.setdefault(ch.id, deque())
            self._new.setdefault(ch.id, [])
        live = {ch.id for ch in self.config.channels}
        for cid in [c for c in self._buffers if c not in live]:
            self._buffers.pop(cid, None)
            self._new.pop(cid, None)

    async def _ensure_subs(self) -> None:
        for topic in list(self._topic_channels):
            if topic not in self._subscribed:
                self._subscribed.add(topic)
                await self.bus.subscribe(topic, self._make_handler(topic))

    def _make_handler(self, topic: str):
        async def _handler(payload: Any) -> None:
            await self._on_source(topic, payload)
        return _handler

    async def _on_source(self, topic: str, payload: Any) -> None:
        if not self.config.running:
            return
        now = time.time()
        ts = payload.get("ts") if isinstance(payload, dict) else None
        # Trust the source stamp only when it's plausibly recent, else the
        # arrival time — robust to unstamped sources + cross-host skew.
        t = float(ts) if isinstance(ts, (int, float)) and abs(now - ts) < self.config.buffer_s else now
        by_id = {ch.id: ch for ch in self.config.channels}
        for cid in self._topic_channels.get(topic, ()):  # snapshot copy not needed — set is stable within tick
            ch = by_id.get(cid)
            if ch is None or not ch.enabled:
                continue
            v = resolve_field(payload, ch.field)
            if v is None:
                continue
            self._ingest(cid, t, v)
            if self.config.trigger.channel_id == cid:
                await self._trigger_check(t, v)

    def _ingest(self, cid: str, t: float, v: float) -> None:
        buf = self._buffers.setdefault(cid, deque())
        buf.append((t, v))
        # Prune against the NEWEST sample time seen (not this sample's t) so an
        # out-of-order/late sample can't shrink the window and drop good data.
        if t > self._max_t:
            self._max_t = t
        cutoff = self._max_t - self.config.buffer_s
        while buf and buf[0][0] < cutoff:
            buf.popleft()
        self._new.setdefault(cid, []).append((t, v))

    async def _trigger_check(self, t: float, v: float) -> None:
        trig = self.config.trigger
        prev = self._trig_prev
        self._trig_prev = v
        if prev is None:
            return
        level = trig.level
        rising = prev < level <= v
        falling = prev > level >= v
        fire = ((trig.edge == "rising" and rising)
                or (trig.edge == "falling" and falling)
                or (trig.edge == "either" and (rising or falling)))
        if not fire:
            return
        if (t - self._last_trig_t) < trig.holdoff_ms / 1000.0:
            return
        self._last_trig_t = t
        self._trig_seq += 1
        ev = {"channel_id": trig.channel_id, "t": t, "value": v,
              "edge": trig.edge, "direction": "rising" if rising else "falling",
              "seq": self._trig_seq}
        self._last_trigger = ev
        self._frame_trig = ev
        if trig.publish:
            await self.publish("trigger", ev)
        if trig.mode == "single":
            # Keep running long enough to capture the post-trigger window,
            # then freeze so the operator can inspect the single-shot.
            self._single_stop_at = t + (self.config.timebase_ms / 1000.0) * 0.6

    # ─── frame streaming ─────────────────────────────────────────────
    async def _frame_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(1.0 / max(1.0, float(self.config.frame_hz)))
                now = time.time()
                ch: Dict[str, List[Tuple[float, float]]] = {}
                for cid, samples in self._new.items():
                    if samples:
                        ch[cid] = samples
                        self._new[cid] = []
                self._frame_seq += 1
                frame = {"t": now, "seq": self._frame_seq, "running": self.config.running,
                         "ch": ch, "trig": self._frame_trig}
                self._frame_trig = None
                await self.publish("frame", frame)
                if self._single_stop_at is not None and now >= self._single_stop_at:
                    self._single_stop_at = None
                    await self._persist(running=False)
                    await self.publish_state()
        except asyncio.CancelledError:
            return

    # ─── helpers ─────────────────────────────────────────────────────
    def _by_id(self, cid: str) -> Optional[Channel]:
        return next((c for c in self.config.channels if c.id == cid), None)

    def _next_id(self) -> str:
        existing = {c.id for c in self.config.channels}
        i = 1
        while f"ch{i}" in existing:
            i += 1
        return f"ch{i}"

    async def _persist(self, *, channels: Optional[List[Channel]] = None,
                       trigger: Optional[Trigger] = None, **scalars: Any) -> None:
        patch: Dict[str, Any] = {}
        if channels is not None:
            patch["channels"] = [c.model_dump() for c in channels]
        if trigger is not None:
            patch["trigger"] = trigger.model_dump()
        patch.update(scalars)
        if not patch:
            return
        self.config = self.config.merge_dict(patch)
        await self.update_config(patch)

    def _snapshot(self) -> Dict[str, Any]:
        trig = self.config.trigger
        mode = trig.mode if trig.channel_id else "roll"
        return {
            "running": self.config.running,
            "mode": mode,
            "timebase_ms": self.config.timebase_ms,
            "frame_hz": self.config.frame_hz,
            "buffer_s": self.config.buffer_s,
            "channels": [c.model_dump() for c in self.config.channels],
            "trigger": trig.model_dump(),
            "last_trigger": self._last_trigger,
            "t_now": time.time(),
        }

    async def publish_state(self) -> None:
        await self.publish("state", self._snapshot(), retained=True)

    def _reset_trigger_state(self) -> None:
        self._trig_prev = None
        self._last_trig_t = float("-inf")
        self._single_stop_at = None

    # ─── @service_method actions ─────────────────────────────────────
    @service_method("add_channel", publishes=["state"])
    async def m_add_channel(
        self, source_topic: str, field: str = "value", kind: str = "analog",
        label: str = "", color: str = "", scale: float = 1.0, offset: float = 0.0,
    ) -> Dict[str, Any]:
        """Add a channel bound to ``source_topic`` + dotted ``field``."""
        cid = self._next_id()
        col = color or _PALETTE[len(self.config.channels) % len(_PALETTE)]
        ch = Channel(id=cid, label=label, source_topic=str(source_topic), field=str(field),
                     kind=("digital" if kind == "digital" else "analog"), color=col,
                     scale=float(scale), offset=float(offset))
        await self._persist(channels=[*self.config.channels, ch])
        self._rebuild_routing()
        await self._ensure_subs()
        await self.publish_state()
        return ch.model_dump()

    @service_method("remove_channel", publishes=["state"])
    async def m_remove_channel(self, id: str) -> Dict[str, Any]:
        """Remove a channel by id (its ring buffer is dropped)."""
        remaining = [c for c in self.config.channels if c.id != id]
        # If it was the trigger source, clear the trigger binding.
        trig = self.config.trigger
        if trig.channel_id == id:
            trig = trig.model_copy(update={"channel_id": None})
            self._reset_trigger_state()
        await self._persist(channels=remaining, trigger=trig)
        self._rebuild_routing()
        await self.publish_state()
        return {"id": id, "removed": True}

    @service_method("set_channel", publishes=["state"])
    async def m_set_channel(self, id: str, **fields: Any) -> Dict[str, Any]:
        """Update one channel (label/color/kind/scale/offset/enabled/
        source_topic/field). Re-routes subscriptions if the source changes."""
        allowed = {"label", "color", "kind", "auto", "scale", "offset", "enabled",
                   "source_topic", "field"}
        channels: List[Channel] = []
        found = False
        for c in self.config.channels:
            if c.id == id:
                found = True
                patch = {k: v for k, v in fields.items() if k in allowed}
                channels.append(c.model_copy(update=patch))
            else:
                channels.append(c)
        if not found:
            raise KeyError(f"unknown channel {id!r}")
        # If the TRIGGER channel's source/field changed, the buffered prev value
        # is from the old signal — reset so edge detection doesn't compare across
        # unrelated sources and fire spuriously.
        if self.config.trigger.channel_id == id and ("source_topic" in fields or "field" in fields):
            self._reset_trigger_state()
        await self._persist(channels=channels)
        self._rebuild_routing()
        await self._ensure_subs()
        await self.publish_state()
        return next(c.model_dump() for c in channels if c.id == id)

    @service_method("set_timebase", publishes=["state"])
    async def m_set_timebase(self, timebase_ms: float) -> Dict[str, Any]:
        """Set the horizontal window width (ms across the whole screen)."""
        await self._persist(timebase_ms=max(10.0, float(timebase_ms)))
        await self.publish_state()
        return {"timebase_ms": self.config.timebase_ms}

    @service_method("set_frame_rate", publishes=["state"])
    async def m_set_frame_rate(self, frame_hz: float) -> Dict[str, Any]:
        """Set the UI sample-frame publish rate."""
        await self._persist(frame_hz=max(1.0, min(120.0, float(frame_hz))))
        await self.publish_state()
        return {"frame_hz": self.config.frame_hz}

    @service_method("set_buffer", publishes=["state"])
    async def m_set_buffer(self, buffer_s: float) -> Dict[str, Any]:
        """Set the ring-buffer depth (seconds of history kept per channel)."""
        await self._persist(buffer_s=max(0.5, float(buffer_s)))
        await self.publish_state()
        return {"buffer_s": self.config.buffer_s}

    @service_method("set_trigger", publishes=["state"])
    async def m_set_trigger(self, **fields: Any) -> Dict[str, Any]:
        """Configure the trigger (channel_id/mode/edge/level/holdoff_ms/
        publish). Pass channel_id=null to disable (free-running roll)."""
        allowed = {"channel_id", "mode", "edge", "level", "holdoff_ms", "publish"}
        patch = {k: v for k, v in fields.items() if k in allowed}
        trig = self.config.trigger.model_copy(update=patch)
        self._reset_trigger_state()
        await self._persist(trigger=trig)
        await self.publish_state()
        return trig.model_dump()

    @service_method("set_running", publishes=["state"])
    async def m_set_running(self, running: bool) -> Dict[str, Any]:
        """Run (acquire) or stop (freeze the display)."""
        self._single_stop_at = None
        await self._persist(running=bool(running))
        await self.publish_state()
        return {"running": self.config.running}

    @service_method("arm_single", publishes=["state"])
    async def m_arm_single(self) -> Dict[str, Any]:
        """Arm a single-shot capture: run in single mode until the next
        trigger, then freeze. Requires a trigger channel."""
        trig = self.config.trigger.model_copy(update={"mode": "single"})
        self._reset_trigger_state()
        await self._persist(trigger=trig, running=True)
        await self.publish_state()
        return {"armed": True, "trigger_channel": trig.channel_id}

    @service_method("clear", publishes=["state"])
    async def m_clear(self) -> Dict[str, Any]:
        """Clear all ring buffers (blank the display)."""
        for cid in self._buffers:
            self._buffers[cid].clear()
            self._new[cid] = []
        await self.publish_state()
        return {"cleared": True}
