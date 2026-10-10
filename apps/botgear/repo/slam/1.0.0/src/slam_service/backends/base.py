# unmanaged
"""MappingBackend — the pluggable interface between the geometry/
odometry pipeline and grid accumulation + pose correction.

See README.md § Mapping backends for the full reuse-vs-build reasoning
behind this boundary (spiked BreezySLAM, decided to start hand-rolled,
kept this interface so a BreezySlamBackend is a swap-in later, not a
rewrite). v1 ships exactly one implementation, `HandRolledBackend`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Tuple

import numpy as np

from ..geometry import PoseDelta, ScanPoint


class MappingBackend(ABC):
    """One cycle: robot-frame scan + odometry delta in, current-best
    pose out. Implementations own the occupancy grid entirely — cell
    semantics are internal to each backend; the caller (Phase 2's
    SlamService) normalizes before publishing so a UI/consumer doesn't
    need to know which backend is active."""

    @abstractmethod
    def update(self, scan: List[ScanPoint], pose_change: PoseDelta) -> Tuple[float, float, float]:
        """Returns the backend's current best (x_m, y_m, theta_deg) in
        the map frame. `HandRolledBackend` just integrates
        `pose_change` with no correction (the named "no drift
        correction" limitation, README.md § Limitations); a scan-matching
        backend would use `scan` to correct it against the accumulated
        map."""

    @abstractmethod
    def occupancy_grid(self) -> np.ndarray:
        """Current grid. `HandRolledBackend` returns a log-odds
        float32 array (positive = more likely occupied, negative =
        more likely free, ~0 = unknown) — README.md § Mapping backends"""

    @abstractmethod
    def inflated_grid(self) -> np.ndarray:
        """Boolean grid — True where the robot's footprint could not
        be centered on that cell without overlapping an obstacle (the
        "configuration space" trick, README.md § Pipeline step 4)."""
