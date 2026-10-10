# unmanaged
"""Unit tests for slam_service.backends — the MappingBackend interface
and its v1 implementation, HandRolledBackend (log-odds ray-casting grid
+ inflation + uncorrected pose integration). See repo/slam/1.0.0/README.md § Mapping backends Pure math, no bus/hardware needed."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_SLAM_SRC = Path(__file__).resolve().parents[1] / "repo" / "slam" / "1.0.0" / "src"
if str(_SLAM_SRC) not in sys.path:
    sys.path.insert(0, str(_SLAM_SRC))

from slam_service.config import SlamConfig  # noqa: E402
from slam_service.geometry import PoseDelta, ScanPoint  # noqa: E402
from slam_service.backends import HandRolledBackend, build_backend  # noqa: E402


def _config(**overrides) -> SlamConfig:
    defaults = dict(map_size_m=10.0, map_resolution_m=0.1, footprint_radius_m=0.3)
    defaults.update(overrides)
    return SlamConfig(**defaults)


# ─── registry ───────────────────────────────────────────────────────────

def test_build_backend_default_is_hand_rolled():
    backend = build_backend(_config())
    assert isinstance(backend, HandRolledBackend)


def test_build_backend_rejects_unknown_name():
    with pytest.raises(ValueError, match="unknown mapping_backend"):
        build_backend(_config(mapping_backend="nonexistent"))


# ─── pose integration (no correction — the v1 tradeoff) ────────────────

def test_update_with_no_scan_still_integrates_pose():
    backend = HandRolledBackend(_config())
    x, y, theta = backend.update([], PoseDelta(dx_m=1.0, dy_m=0.0, dtheta_deg=0.0))
    assert (x, y, theta) == pytest.approx((1.0, 0.0, 0.0))


def test_pose_accumulates_across_updates():
    backend = HandRolledBackend(_config())
    backend.update([], PoseDelta(dx_m=1.0, dy_m=0.0, dtheta_deg=90.0))
    x, y, theta = backend.update([], PoseDelta(dx_m=1.0, dy_m=0.0, dtheta_deg=0.0))
    # After the first update, heading is 90deg, so the second update's
    # "forward 1m" moves along world +y, not +x.
    assert x == pytest.approx(1.0, abs=1e-6)
    assert y == pytest.approx(1.0, abs=1e-6)
    assert theta == pytest.approx(90.0)


# ─── grid population ────────────────────────────────────────────────────

def test_grid_starts_all_unknown():
    backend = HandRolledBackend(_config())
    grid = backend.occupancy_grid()
    assert grid.shape == (100, 100)  # 10m / 0.1m resolution
    assert np.all(grid == 0.0)


def test_scan_with_obstacle_marks_free_and_occupied_cells():
    backend = HandRolledBackend(_config())
    # Straight ahead (bearing 0), obstacle at 2m.
    backend.update([ScanPoint(bearing_deg=0.0, range_m=2.0)], PoseDelta(0.0, 0.0, 0.0))
    grid = backend.occupancy_grid()
    center = grid.shape[0] // 2
    # Cell ~2m ahead (along +x -> smaller-row/"up") should trend
    # occupied; a cell partway there (e.g. 1m) should trend free.
    occupied_row = center - int(round(2.0 / 0.1))
    free_row = center - int(round(1.0 / 0.1))
    assert grid[occupied_row, center] > 0
    assert grid[free_row, center] < 0


def test_repeated_hits_increase_confidence_up_to_clamp():
    backend = HandRolledBackend(_config())
    for _ in range(50):
        backend.update([ScanPoint(bearing_deg=0.0, range_m=2.0)], PoseDelta(0.0, 0.0, 0.0))
    grid = backend.occupancy_grid()
    center = grid.shape[0] // 2
    occupied_row = center - int(round(2.0 / 0.1))
    assert grid[occupied_row, center] == pytest.approx(5.0)  # _LOG_ODDS_CLAMP


def test_intermittent_hits_settle_free_not_occupied():
    """The bug found live: oakd's depth-decimation blends two genuinely
    different valid depths together at an object's silhouette edge (a
    straddling cell isn't invalid, so _decimate_depth_masked's invalid-
    pixel guard doesn't catch it), producing a bearing that only
    intermittently reports a hit, at scattered ranges, when it does. The
    OLD log-odds pair (-0.4 free / +0.85 occupied) has a break-even
    hit-rate of only ~32% (0.4/(0.85+0.4)) — ANY bearing hit MORE often
    than that eventually saturates to fully "occupied" at the clamp,
    however noisy. Replaying 30 real consecutive depth_sample ticks
    confirmed exactly this: genuine edge-noise columns at 33%/47%
    hit-rate saturated to +3 to +4 log-odds under the old weights.

    This constructs a bearing hit 40% of the time (2 hits, 3 misses,
    repeating) — comfortably above the OLD 32% break-even (would have
    saturated occupied) but below the NEW ~60% break-even
    (1.3/(0.85+1.3)) — and confirms it now settles solidly FREE, while a
    bearing hit on almost every tick (the real-edge case, matching the
    box's own 100%-hit-rate columns in that live capture) still
    saturates fully occupied, unaffected."""
    backend = HandRolledBackend(_config())
    noisy_bearing = 20.0
    solid_bearing = -20.0
    for i in range(150):
        scan = []
        # Noisy: hit on 2 of every 5 ticks (40%) — reported range wobbles
        # tick to tick, matching real edge-blend noise (not that the
        # exact range matters for this test — the terminal cell still
        # differs slightly per hit, but the log-odds question is about
        # HOW OFTEN a cell near this bearing gets a terminal nudge).
        if i % 5 < 2:
            scan.append(ScanPoint(bearing_deg=noisy_bearing, range_m=2.0))
        else:
            scan.append(ScanPoint(bearing_deg=noisy_bearing, range_m=None))
        # Solid: hits on 9 of every 10 ticks (90%) — a real, mostly-
        # stable edge should stay confidently occupied regardless.
        if i % 10 != 0:
            scan.append(ScanPoint(bearing_deg=solid_bearing, range_m=2.0))
        else:
            scan.append(ScanPoint(bearing_deg=solid_bearing, range_m=None))
        backend.update(scan, PoseDelta(0.0, 0.0, 0.0))

    grid = backend.occupancy_grid()
    center = grid.shape[0] // 2
    import math
    def target_row_col(bearing_deg):
        heading = math.radians(bearing_deg)
        row, col = backend._world_to_cell(2.0 * math.cos(heading), 2.0 * math.sin(heading))
        return row, col

    noisy_row, noisy_col = target_row_col(noisy_bearing)
    solid_row, solid_col = target_row_col(solid_bearing)
    assert grid[noisy_row, noisy_col] < 0  # settles free, not occupied
    assert grid[solid_row, solid_col] == pytest.approx(5.0)  # still saturates fully occupied


def test_no_obstacle_scan_marks_free_up_to_map_edge():
    backend = HandRolledBackend(_config(map_size_m=4.0, map_resolution_m=0.1))
    backend.update([ScanPoint(bearing_deg=0.0, range_m=None)], PoseDelta(0.0, 0.0, 0.0))
    grid = backend.occupancy_grid()
    center = grid.shape[0] // 2
    # A ray with range_m=None should ray-cast all the way to the map
    # boundary without ever writing an "occupied" terminal cell. Forward
    # sweeps toward row 0 (smaller row = "up"), not larger.
    assert np.all(grid[: center + 1, center] <= 0)


def test_forward_bearing_obstacle_maps_to_smaller_row_not_larger():
    # World +x (the robot's OWN forward direction at pose (0,0,0)) must
    # map to a SMALLER row — i.e. further UP a canvas that blits this
    # grid row-major with row 0 at the top — not a larger one, so the
    # map renders "forward is up" like every other top-down robot/game
    # map. Getting this backwards doesn't crash or fail a y/col-only
    # assertion, it just makes the robot's forward direction render as
    # screen-right instead of screen-up — exactly the live-hardware
    # confusion this test is here to catch a regression of (an operator
    # reading "top of map = forward" sees the map as 90 degrees wrong,
    # even though theta/bearing math underneath is internally
    # consistent — see repo/slam/1.0.0/README.md's Phase 2 rotation-direction
    # writeup).
    backend = HandRolledBackend(_config())
    # Pure forward bearing (0deg), obstacle at 2m: hits world (2, 0).
    backend.update([ScanPoint(bearing_deg=0.0, range_m=2.0)], PoseDelta(0.0, 0.0, 0.0))
    grid = backend.occupancy_grid()
    center = grid.shape[0] // 2
    occupied_row = center - int(round(2.0 / 0.1))
    assert grid[occupied_row, center] > 0
    # And NOT at the mirrored (larger-row) cell a sign-flip bug would hit.
    mirrored_row = center + int(round(2.0 / 0.1))
    assert grid[mirrored_row, center] == 0.0


def test_left_bearing_obstacle_maps_to_smaller_col_not_larger():
    # World +y (the robot's own left, PoseDelta's convention) must map
    # to a SMALLER col — further LEFT on screen — matching a "forward is
    # up" map orientation where the robot faces up the screen at its
    # start pose (its own left is screen-left, same as a car's
    # heading-up GPS view). See the forward/row test above for the
    # companion axis.
    backend = HandRolledBackend(_config())
    # Pure left bearing (90deg), obstacle at 2m: hits world (0, 2).
    backend.update([ScanPoint(bearing_deg=90.0, range_m=2.0)], PoseDelta(0.0, 0.0, 0.0))
    grid = backend.occupancy_grid()
    center = grid.shape[0] // 2
    occupied_col = center - int(round(2.0 / 0.1))
    assert grid[center, occupied_col] > 0
    mirrored_col = center + int(round(2.0 / 0.1))
    assert grid[center, mirrored_col] == 0.0


def test_out_of_bounds_ray_does_not_crash():
    backend = HandRolledBackend(_config(map_size_m=2.0, map_resolution_m=0.1))
    # Obstacle well past the map's edge.
    backend.update([ScanPoint(bearing_deg=0.0, range_m=50.0)], PoseDelta(0.0, 0.0, 0.0))
    grid = backend.occupancy_grid()
    assert grid.shape == (20, 20)  # didn't crash, didn't resize


# ─── inflation ───────────────────────────────────────────────────────────

def test_inflated_grid_grows_occupied_region():
    backend = HandRolledBackend(_config(footprint_radius_m=0.5, map_resolution_m=0.1))
    for _ in range(10):
        backend.update([ScanPoint(bearing_deg=0.0, range_m=2.0)], PoseDelta(0.0, 0.0, 0.0))
    raw_occupied = int((backend.occupancy_grid() > 1.0).sum())
    inflated_occupied = int(backend.inflated_grid().sum())
    assert inflated_occupied > raw_occupied


def test_inflated_grid_is_boolean():
    backend = HandRolledBackend(_config())
    backend.update([ScanPoint(bearing_deg=0.0, range_m=2.0)], PoseDelta(0.0, 0.0, 0.0))
    assert backend.inflated_grid().dtype == bool


def test_inflated_grid_cache_invalidates_on_update():
    backend = HandRolledBackend(_config(footprint_radius_m=0.5, map_resolution_m=0.1))
    first = backend.inflated_grid().sum()
    for _ in range(10):
        backend.update([ScanPoint(bearing_deg=0.0, range_m=2.0)], PoseDelta(0.0, 0.0, 0.0))
    second = backend.inflated_grid().sum()
    assert second > first
