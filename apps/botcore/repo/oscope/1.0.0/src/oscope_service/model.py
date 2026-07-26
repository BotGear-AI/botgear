"""Typed config for the oscope (oscilloscope) service."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from rlx_bus import ServiceConfig


class Channel(BaseModel):
    """A scope channel = a binding to any bus signal.

    ``source_topic`` is any bus topic; ``field`` is a dotted path into
    its payload (``value``, ``axes.0``, ``pins.5.value`` …) — so a channel
    can plot ANY publisher (arduino/raspi pins, joystick axes, siggen
    waves, sensors), not just one source type. ``kind`` picks how it
    renders (analog line vs digital logic row).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Stable channel id")
    label: str = ""
    source_topic: str = Field(..., description="Bus topic to subscribe")
    field: str = "value"                 # dotted path into the payload
    kind: Literal["analog", "digital"] = "analog"
    color: str = "#38bdf8"
    auto: bool = True                    # auto-fit the trace to its data (any range)
    scale: float = 1.0                   # manual display gain (units per division)
    offset: float = 0.0                  # manual vertical position, in divisions (+ = up)
    enabled: bool = True


class Trigger(BaseModel):
    """Edge/level trigger. When ``channel_id`` is set the scope watches
    that channel; on a qualifying crossing it aligns the sweep and (when
    ``publish``) emits a bus event other services can react to."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Optional[str] = None     # None ⇒ free-running roll, no trigger
    mode: Literal["auto", "normal", "single"] = "auto"
    edge: Literal["rising", "falling", "either"] = "rising"
    level: float = 0.5
    holdoff_ms: float = 0.0
    publish: bool = True                 # emit /oscope/{id}/trigger events


class OscopeConfig(ServiceConfig):
    """Persisted scope config — channels + horizontal/trigger settings."""

    model_config = ConfigDict(extra="forbid")

    channels: List[Channel] = Field(default_factory=list)
    timebase_ms: float = 2000.0          # window width across the screen
    buffer_s: float = 20.0               # ring-buffer depth per channel
    frame_hz: float = 30.0               # sample-frame publish rate to the UI
    running: bool = True                 # run vs stop (freeze)
    trigger: Trigger = Field(default_factory=Trigger)
