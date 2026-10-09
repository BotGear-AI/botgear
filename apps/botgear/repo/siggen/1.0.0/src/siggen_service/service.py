"""SiggenService — a bus signal generator (function generator).

Subprocess service. Emits configurable waveforms (sine/square/triangle/
sawtooth/noise/dc/clock) on per-channel topics so a scope — or any other
consumer — can be exercised without hardware. Implements the
``signal_source`` capability: ``list_signals`` advertises the streaming
topics so consumers (e.g. the oscope service's channel picker) can
discover them.

Wire shape is declared in code below (``SiggenState``,
``SamplePayload`` via ``state_schema``/``topic_schemas``) and
introspected into ``/runtime/runtime/types/siggen``, as are the
``control`` actions. Samples stream on one topic per wave,
``ch/{wave_id}``.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel
from rlx_bus import SubprocessService, service_method

from .model import SiggenConfig, Wave

logger = logging.getLogger(__name__)


def wave_value(w: Wave, t: float) -> float:
    """Instantaneous value of wave ``w`` at elapsed time ``t`` (seconds)."""
    ph = w.freq_hz * t + w.phase_deg / 360.0
    frac = ph - math.floor(ph)               # position within the cycle, 0..1
    duty = min(1.0, max(0.0, w.duty))
    if w.shape == "sine":
        return w.offset + w.amplitude * math.sin(2.0 * math.pi * ph)
    if w.shape == "square":
        return w.offset + w.amplitude * (1.0 if frac < duty else -1.0)
    if w.shape == "triangle":
        return w.offset + w.amplitude * (1.0 - 4.0 * abs(frac - 0.5))
    if w.shape == "sawtooth":
        return w.offset + w.amplitude * (2.0 * frac - 1.0)
    if w.shape == "noise":
        return w.offset + w.amplitude * (2.0 * random.random() - 1.0)
    if w.shape == "clock":                   # the digital shape — emits 0/1
        return 1.0 if frac < duty else 0.0
    return w.offset                          # "dc"


# ── wire schemas for what this service PUBLISHES ──────────────────────

class SiggenState(BaseModel):
    """Retained on ``/siggen/{id}/state`` — running + rate + wave bank."""
    running: bool
    sample_hz: float
    waves: List[Wave]


class SamplePayload(BaseModel):
    """Published on ``/siggen/{id}/ch/{wave_id}`` at ``sample_hz`` per
    enabled wave."""
    value: float
    ts: float


class SiggenService(SubprocessService):
    """Function generator on the bus. One task ticks the whole wave bank."""

    type_name = "siggen"
    heartbeat_interval_s = 1.0
    config_class = SiggenConfig
    publishes = ["state"]
    state_schema = SiggenState
    # Per-wave stream — the suffix is templated, one topic per wave id.
    topic_schemas = {"ch/{wave_id}": SamplePayload}

    def __init__(self, proxy_id: str, bus) -> None:
        super().__init__(proxy_id, bus)
        self._t0: float = time.time()
        self._task: Optional[asyncio.Task] = None

    # ─── lifecycle ───────────────────────────────────────────────────
    async def on_start(self) -> None:
        # Seed a friendly default bank on first run so the generator is
        # useful (and the scope has something to show) out of the box.
        if not self.config.waves:
            defaults = [
                Wave(id="w1", label="sine 1 Hz", shape="sine", freq_hz=1.0, amplitude=1.0),
                Wave(id="w2", label="square 0.5 Hz", shape="square", freq_hz=0.5, amplitude=0.8),
                Wave(id="w3", label="clock 2 Hz", shape="clock", freq_hz=2.0, duty=0.5),
            ]
            await self._persist(waves=defaults)
        self._t0 = time.time()
        self._task = asyncio.get_event_loop().create_task(self._sample_loop())
        await self.publish_state()

    async def on_stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None

    # ─── generation loop ─────────────────────────────────────────────
    async def _sample_loop(self) -> None:
        """Publish every enabled wave once per tick at ``sample_hz``."""
        try:
            while True:
                period = 1.0 / max(1.0, float(self.config.sample_hz))
                if self.config.running:
                    now = time.time()
                    t = now - self._t0
                    for w in self.config.waves:
                        if not w.enabled:
                            continue
                        try:
                            v = wave_value(w, t)
                        except Exception:  # noqa: BLE001 — never let one wave kill the loop
                            logger.exception("siggen %s: wave %s eval failed", self.proxy_id, w.id)
                            continue
                        await self.bus.publish(self.ch_topic(w.id), SamplePayload(value=v, ts=now).model_dump())
                await asyncio.sleep(max(0.002, period))
        except asyncio.CancelledError:
            return

    # ─── helpers ─────────────────────────────────────────────────────
    def ch_topic(self, wave_id: str) -> str:
        return self.topic(f"ch/{wave_id}")

    def _next_id(self) -> str:
        existing = {w.id for w in self.config.waves}
        i = 1
        while f"w{i}" in existing:
            i += 1
        return f"w{i}"

    async def _persist(self, *, waves: Optional[List[Wave]] = None,
                       sample_hz: Optional[float] = None,
                       running: Optional[bool] = None) -> None:
        patch: Dict[str, Any] = {}
        if waves is not None:
            patch["waves"] = [w.model_dump() for w in waves]
        if sample_hz is not None:
            patch["sample_hz"] = float(sample_hz)
        if running is not None:
            patch["running"] = bool(running)
        if not patch:
            return
        self.config = self.config.merge_dict(patch)
        await self.update_config(patch)

    def _snapshot(self) -> Dict[str, Any]:
        state = SiggenState(
            running=self.config.running,
            sample_hz=self.config.sample_hz,
            waves=list(self.config.waves),
        )
        return state.model_dump()

    async def publish_state(self) -> None:
        await self.publish("state", self._snapshot(), retained=True)

    # ─── @service_method actions ─────────────────────────────────────
    @service_method("add_wave", publishes=["state"])
    async def m_add_wave(
        self, shape: str = "sine", freq_hz: float = 1.0, amplitude: float = 1.0,
        offset: float = 0.0, duty: float = 0.5, phase_deg: float = 0.0, label: str = "",
    ) -> Dict[str, Any]:
        """Add a wave to the bank. Returns the created wave."""
        w = Wave(id=self._next_id(), label=label, shape=shape, freq_hz=float(freq_hz),
                 amplitude=float(amplitude), offset=float(offset), duty=float(duty),
                 phase_deg=float(phase_deg))
        await self._persist(waves=[*self.config.waves, w])
        await self.publish_state()
        return w.model_dump()

    @service_method("remove_wave", publishes=["state"])
    async def m_remove_wave(self, id: str) -> Dict[str, Any]:
        """Remove a wave by id."""
        remaining = [w for w in self.config.waves if w.id != id]
        await self._persist(waves=remaining)
        await self.publish_state()
        return {"id": id, "removed": True}

    @service_method("set_wave", publishes=["state"])
    async def m_set_wave(self, id: str, **fields: Any) -> Dict[str, Any]:
        """Update fields of one wave (shape/freq_hz/amplitude/offset/duty/
        phase_deg/label/enabled). Unknown keys are ignored."""
        allowed = {"label", "shape", "freq_hz", "amplitude", "offset", "duty", "phase_deg", "enabled"}
        waves: List[Wave] = []
        found = False
        for w in self.config.waves:
            if w.id == id:
                found = True
                patch = {k: v for k, v in fields.items() if k in allowed}
                waves.append(w.model_copy(update=patch))
            else:
                waves.append(w)
        if not found:
            raise KeyError(f"unknown wave {id!r}")
        await self._persist(waves=waves)
        await self.publish_state()
        return next(w.model_dump() for w in waves if w.id == id)

    @service_method("set_sample_rate", publishes=["state"])
    async def m_set_sample_rate(self, sample_hz: float) -> Dict[str, Any]:
        """Set the per-wave publish rate (Hz)."""
        await self._persist(sample_hz=max(1.0, float(sample_hz)))
        await self.publish_state()
        return {"sample_hz": self.config.sample_hz}

    @service_method("set_running", publishes=["state"])
    async def m_set_running(self, running: bool) -> Dict[str, Any]:
        """Start/stop generation (the loop keeps ticking but goes silent)."""
        await self._persist(running=bool(running))
        await self.publish_state()
        return {"running": self.config.running}

    @service_method("clear", publishes=["state"])
    async def m_clear(self) -> Dict[str, Any]:
        """Remove all waves."""
        await self._persist(waves=[])
        await self.publish_state()
        return {"waves": 0}

    @service_method("list_signals")
    async def m_list_signals(self) -> Dict[str, Any]:
        """signal_source capability: advertise the streaming signals so a
        consumer (e.g. the oscope channel picker) can discover them."""
        signals = []
        for w in self.config.waves:
            if not w.enabled:
                continue
            if w.shape == "clock":
                kind, lo, hi = "digital", 0.0, 1.0
            else:
                kind = "analog"
                lo, hi = w.offset - w.amplitude, w.offset + w.amplitude
            signals.append({
                "topic": self.ch_topic(w.id),
                "field": "value",
                "label": w.label or f"{w.shape} {w.freq_hz:g}Hz",
                "kind": kind,
                "min": lo,
                "max": hi,
            })
        return {"signals": signals}
