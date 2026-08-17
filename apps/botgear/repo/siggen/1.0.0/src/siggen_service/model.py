"""Typed config for the siggen (signal generator) service."""
from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field

from rlx_bus import ServiceConfig

# Analog waveshapes emit a float centred on ``offset`` with peak deviation
# ``amplitude``; ``clock`` is the one DIGITAL shape (emits 0/1) so a scope
# can be exercised with both analog and logic traces from one generator.
WaveShape = Literal["sine", "square", "triangle", "sawtooth", "noise", "dc", "clock"]


class Wave(BaseModel):
    """One generated channel. Streams on /siggen/{id}/ch/{id} as {value, ts}."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Stable channel id (topic suffix)")
    label: str = ""
    shape: WaveShape = "sine"
    freq_hz: float = 1.0
    amplitude: float = 1.0            # peak deviation (analog); ignored for clock/dc
    offset: float = 0.0              # vertical centre (analog); ignored for clock
    duty: float = 0.5               # high fraction 0..1 for square/clock
    phase_deg: float = 0.0
    enabled: bool = True


class SiggenConfig(ServiceConfig):
    """Persisted generator config — a bank of waves + the shared sample rate."""

    model_config = ConfigDict(extra="forbid")

    waves: List[Wave] = Field(default_factory=list)
    sample_hz: float = 100.0         # per-wave publish rate
    running: bool = True
