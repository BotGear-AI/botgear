# unmanaged
"""HandRolledBackend — v1's only MappingBackend implementation.

Pure numpy: log-odds occupancy grid + Bresenham ray-casting, footprint
inflation via manual binary dilation (no scipy — keeps the "numpy
only" dependency story clean), and UNCORRECTED pose integration
(accepts odometry's pose_change as-is). See docs/TODO_SLAM.md §5.2a
for why this is the deliberate v1 choice over a scan-matching backend.
"""
from __future__ import annotations

import math
from typing import List, Tuple

import numpy as np

from ..config import SlamConfig
from ..geometry import PoseDelta, ScanPoint, apply_pose_delta
from .base import MappingBackend

# Log-odds increments. A ray passing through a cell nudges it toward
# "free"; a terminal obstacle hit nudges it toward "occupied" more
# strongly PER OBSERVATION — but that per-hit strength is exactly what
# lets a column that's WRONG more often than it's right still win over
# time. With the original -0.4/+0.85 pair, any bearing hit on just
# ~32% of ticks (0.4/(0.85+0.4)) eventually saturates to fully
# "occupied" at the clamp, no matter how it's distributed. Confirmed
# live: replaying 30 real consecutive depth_sample ticks (robot
# stationary) through project_depth_sample_to_scan, the columns
# straddling a real depth discontinuity (oakd's depth-decimation
# blending two genuinely different valid depths into one decimated
# cell right at an object's silhouette edge — see
# oakd_service._decimate_depth_masked, which only guards against
# blending in INVALID pixels, not blending two valid-but-different
# ones) hit only 33-47% of ticks with 6-13cm of range scatter when
# they did — textbook decimation-edge noise, not a real surface. At
# -0.4 those columns still saturated to +3 to +4 log-odds (occupied
# threshold is 1.0): confident, permanent phantom obstacles. Real,
# stable surfaces in the same capture (the box's own face, other
# background objects) hit 93-100% of ticks with <5cm scatter.
#
# -1.3 raises the break-even hit-rate to ~60% (1.3/(0.85+1.3)),
# clearing that gap: replaying the SAME real sequences, the noisy
# 33%/47% columns now settle to -5.0/-2.5 (free) while every
# genuinely stable column (real edges included) still saturates fully
# occupied, unaffected — see
# test_intermittent_hits_settle_free_not_occupied.
_LOG_ODDS_FREE = -1.3
_LOG_ODDS_OCCUPIED = 0.85
_LOG_ODDS_CLAMP = 5.0
_OCCUPIED_THRESHOLD = 1.0  # log-odds value above which a cell counts as occupied for inflation


class HandRolledBackend(MappingBackend):
    def __init__(self, config: SlamConfig) -> None:
        self._config = config
        self._resolution_m = config.map_resolution_m
        self._cells = max(1, int(round(config.map_size_m / config.map_resolution_m)))
        # Grid is centered on the pose at construction time — cell
        # (origin_cell, origin_cell) is world-origin (0, 0).
        self._origin_cell = self._cells // 2
        self._log_odds = np.zeros((self._cells, self._cells), dtype=np.float32)
        self._x_m = 0.0
        self._y_m = 0.0
        self._theta_deg = 0.0
        self._inflated_dirty = True
        self._inflated_cache: np.ndarray = np.zeros((self._cells, self._cells), dtype=bool)

    # ─── coordinate helpers ─────────────────────────────────────────────

    def _world_to_cell(self, x_m: float, y_m: float) -> Tuple[int, int]:
        # 90-degree-CCW-rotated map orientation: world +x (the robot's
        # OWN forward direction at pose (0,0,0)) maps to a SMALLER row
        # (further UP the screen), and world +y (the robot's left) maps
        # to a SMALLER col (further LEFT on screen) — i.e. the map
        # renders "forward is up," the same convention every other
        # top-down robot/game map uses, robot icon pointing up the
        # screen at its start pose. Confirmed live, real hardware: the
        # PREVIOUS convention (x=right, y=up — see git history for that
        # version's reasoning) put the robot's forward direction on
        # SCREEN-RIGHT instead of screen-up, which every operator reading
        # "top of map = forward" misread as the map being 90 degrees
        # wrong — cross-checked against a fresh scan/pose read that
        # confirmed the underlying math was internally consistent
        # (obstacles landed exactly where theta+bearing said they
        # should); the bug was purely this rendering convention not
        # matching the intuitive "north-up" reading. This is a ROTATION
        # of the earlier fix, not a reflection — it does NOT reintroduce
        # the mirrored-handedness bug that fix addressed (verified: the
        # (x,y) -> (-y,-x) linear map here has determinant +1, same
        # rotational handedness as the identity map the previous fix
        # produced, just rotated 90 degrees further).
        col = self._origin_cell - int(round(y_m / self._resolution_m))
        # See slam/1.0.0/ui/View.tsx's matching `px`/`py` computation,
        # which must stay in sync with this.
        row = self._origin_cell - int(round(x_m / self._resolution_m))
        return row, col

    def _in_bounds(self, row: int, col: int) -> bool:
        return 0 <= row < self._cells and 0 <= col < self._cells

    # ─── MappingBackend ─────────────────────────────────────────────────

    def update(self, scan: List[ScanPoint], pose_change: PoseDelta) -> Tuple[float, float, float]:
        self._x_m, self._y_m, self._theta_deg = apply_pose_delta(
            self._x_m, self._y_m, self._theta_deg, pose_change,
        )
        origin_row, origin_col = self._world_to_cell(self._x_m, self._y_m)
        for point in scan:
            self._cast_ray(origin_row, origin_col, point)
        self._inflated_dirty = True
        return self._x_m, self._y_m, self._theta_deg

    def occupancy_grid(self) -> np.ndarray:
        return self._log_odds

    def inflated_grid(self) -> np.ndarray:
        if self._inflated_dirty:
            self._recompute_inflated()
        return self._inflated_cache

    # ─── internals ──────────────────────────────────────────────────────

    def _cast_ray(self, origin_row: int, origin_col: int, point: ScanPoint) -> None:
        heading = math.radians(self._theta_deg + point.bearing_deg)
        max_range_m = point.range_m if point.range_m is not None else self._config.map_size_m
        end_row, end_col = self._world_to_cell(
            self._x_m + max_range_m * math.cos(heading),
            self._y_m + max_range_m * math.sin(heading),
        )
        for row, col in _bresenham(origin_row, origin_col, end_row, end_col):
            if not self._in_bounds(row, col):
                break
            is_terminal = (row, col) == (end_row, end_col)
            if is_terminal and point.range_m is not None:
                self._log_odds[row, col] = min(_LOG_ODDS_CLAMP, self._log_odds[row, col] + _LOG_ODDS_OCCUPIED)
            else:
                self._log_odds[row, col] = max(-_LOG_ODDS_CLAMP, self._log_odds[row, col] + _LOG_ODDS_FREE)

    def _recompute_inflated(self) -> None:
        occupied = self._log_odds > _OCCUPIED_THRESHOLD
        radius_cells = max(1, int(math.ceil(self._config.footprint_radius_m / self._resolution_m)))
        self._inflated_cache = _binary_dilate(occupied, radius_cells)
        self._inflated_dirty = False


def _bresenham(row0: int, col0: int, row1: int, col1: int):
    """Standard integer Bresenham line — every grid cell from
    (row0,col0) to (row1,col1) inclusive. No external dependency
    (avoids pulling in scipy/OpenCV just for this)."""
    d_row = abs(row1 - row0)
    d_col = abs(col1 - col0)
    step_row = 1 if row0 < row1 else -1
    step_col = 1 if col0 < col1 else -1
    row, col = row0, col0
    if d_col >= d_row:
        err = d_col / 2.0
        while col != col1:
            yield row, col
            err -= d_row
            if err < 0:
                row += step_row
                err += d_col
            col += step_col
        yield row1, col1
    else:
        err = d_row / 2.0
        while row != row1:
            yield row, col
            err -= d_col
            if err < 0:
                col += step_col
                err += d_row
            row += step_row
        yield row1, col1


def _binary_dilate(mask: np.ndarray, radius_cells: int) -> np.ndarray:
    """Manual binary dilation by a disk of `radius_cells`, numpy-only —
    avoids a scipy dependency for one operation. Lazily recomputed (not
    called per-frame, see `inflated_grid`), so O(occupied_cells ×
    disk_area) is fine at the map sizes this is designed for; swap for
    `scipy.ndimage.binary_dilation` if that ever changes."""
    if radius_cells <= 0:
        return mask.copy()
    out = mask.copy()
    ys, xs = np.nonzero(mask)
    r2 = radius_cells * radius_cells
    h, w = mask.shape
    offsets = [
        (dy, dx)
        for dy in range(-radius_cells, radius_cells + 1)
        for dx in range(-radius_cells, radius_cells + 1)
        if dy * dy + dx * dx <= r2
    ]
    for y, x in zip(ys.tolist(), xs.tolist()):
        for dy, dx in offsets:
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w:
                out[ny, nx] = True
    return out
