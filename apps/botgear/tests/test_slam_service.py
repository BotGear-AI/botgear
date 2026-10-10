# unmanaged
"""Unit tests for slam_service.service.SlamService — the bus-wiring
layer (Phase 2, repo/slam/1.0.0/README.md § Pipeline). Same _FakeBus seam pattern as
test_messaging_base.py: no real websocket, direct construction, manual
handler invocation instead of a running SubprocessService.run() loop.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pytest

_SLAM_SRC = Path(__file__).resolve().parents[1] / "repo" / "slam" / "1.0.0" / "src"
if str(_SLAM_SRC) not in sys.path:
    sys.path.insert(0, str(_SLAM_SRC))

from slam_service.service import SlamService, _find_channel_output, _quantize_grid  # noqa: E402


class _FakeBus:
    def __init__(self) -> None:
        self.published: List[Dict[str, Any]] = []
        self.subscriptions: Dict[str, Any] = {}

    async def publish(self, topic: str, payload: Any, *, retained: bool = False) -> None:
        self.published.append({"topic": topic, "payload": payload, "retained": retained})

    async def subscribe(self, topic: str, handler) -> None:
        self.subscriptions[topic] = handler

    def by_topic_suffix(self, suffix: str) -> List[Dict[str, Any]]:
        return [m for m in self.published if m["topic"].endswith(suffix)]

    def latest(self, suffix: str):
        matches = self.by_topic_suffix(suffix)
        return matches[-1]["payload"] if matches else None


def _motor_state(*, left: float, right: float, left_id="left", right_id="right") -> Dict[str, Any]:
    return {
        "estopped": False,
        "channels": [
            {"id": left_id, "output": left},
            {"id": right_id, "output": right},
        ],
    }


def _depth_sample(depth_mm, cols=4, rows=3) -> Dict[str, Any]:
    return {"cols": cols, "rows": rows, "depth_mm": depth_mm, "ts": 0.0}


def _flat_depth_grid(mm: int, cols=4, rows=3):
    return [[mm] * cols for _ in range(rows)]


# ─── _find_channel_output ────────────────────────────────────────────────

def test_find_channel_output_matches_by_id():
    channels = [{"id": "left", "output": 0.5}, {"id": "right", "output": -0.25}]
    assert _find_channel_output(channels, "left") == 0.5
    assert _find_channel_output(channels, "right") == -0.25


def test_find_channel_output_missing_id_returns_none():
    channels = [{"id": "left", "output": 0.5}]
    assert _find_channel_output(channels, "right") is None


def test_find_channel_output_rejects_non_list():
    assert _find_channel_output({"left": 0.5}, "left") is None  # dict, not list — motor_control's real shape is a list
    assert _find_channel_output(None, "left") is None


# ─── _quantize_grid ─────────────────────────────────────────────────────

def test_quantize_grid_encodes_expected_bytes():
    import numpy as np
    log_odds = np.array([[-1.0, 0.0], [2.0, -1.0]], dtype=np.float32)
    inflated = np.array([[False, False], [False, True]], dtype=bool)
    out = _quantize_grid(log_odds, inflated)
    assert out.dtype == np.uint8
    assert out[0, 0] == 85    # free
    assert out[0, 1] == 0     # unknown
    assert out[1, 0] == 255   # occupied
    assert out[1, 1] == 170   # inflated (overrides the free-ish -1.0 underneath)


# ─── service wiring ─────────────────────────────────────────────────────

def _svc(**config_overrides) -> SlamService:
    bus = _FakeBus()
    svc = SlamService("slam-1", bus)
    defaults = dict(
        oakd_proxy_id="oakd-1", motor_control_proxy_id="mc-1",
        left_channel="left", right_channel="right",
        wheel_speed_calibration_m_per_s=1.0, track_width_m=0.5,
        map_size_m=4.0, map_resolution_m=0.1,
    )
    defaults.update(config_overrides)
    for k, v in defaults.items():
        setattr(svc.config, k, v)
    return svc


@pytest.mark.asyncio
async def test_on_start_subscribes_to_both_derived_topics():
    svc = _svc()
    await svc.on_start()
    assert "/motor_control/mc-1/state" in svc.bus.subscriptions
    assert "/oakd/oakd-1/depth_sample" in svc.bus.subscriptions


@pytest.mark.asyncio
async def test_on_start_respects_explicit_depth_sample_topic_override():
    svc = _svc(depth_sample_topic="/oakd/other-cam/depth_sample")
    await svc.on_start()
    assert "/oakd/other-cam/depth_sample" in svc.bus.subscriptions
    assert "/oakd/oakd-1/depth_sample" not in svc.bus.subscriptions


@pytest.mark.asyncio
async def test_first_motor_tick_establishes_baseline_without_moving():
    svc = _svc()
    await svc._on_motor_state(_motor_state(left=1.0, right=1.0))
    assert svc._pending_dx_m == 0.0
    assert svc._motor_control_connected is True


@pytest.mark.asyncio
async def test_depth_sample_with_no_odometry_still_updates_pose_and_map():
    svc = _svc()
    await svc.on_start()
    grid = _flat_depth_grid(3000)
    await svc._on_depth_sample(_depth_sample(grid))
    assert svc._oakd_connected is True
    pose = svc.bus.latest("/pose")
    assert pose is not None
    assert pose["x_m"] == pytest.approx(0.0)
    # Map publish is throttled but always fires on the FIRST sample.
    assert svc.bus.latest("/map") is not None


@pytest.mark.asyncio
async def test_malformed_inputs_are_ignored_not_fatal():
    svc = _svc()
    await svc.on_start()
    # A non-dict payload isn't even a valid state message — ignored
    # entirely, connected flag untouched.
    await svc._on_motor_state("not a dict")
    await svc._on_depth_sample(None)
    assert svc._motor_control_connected is False
    assert svc._oakd_connected is False
    # A dict payload with malformed/missing channel data still means
    # "we heard from motor_control" (connected=True) — the odometry
    # integration itself just no-ops rather than crashing.
    await svc._on_motor_state({"channels": "also not a list"})
    assert svc._motor_control_connected is True
    assert svc._pending_dx_m == 0.0  # nothing was integrated
    # depth_sample missing the depth_mm key is dropped before flipping
    # oakd_connected, since there's genuinely no scan data to act on.
    await svc._on_depth_sample({"cols": 4})
    assert svc._oakd_connected is False


@pytest.mark.asyncio
async def test_map_message_shape():
    svc = _svc(map_size_m=2.0, map_resolution_m=0.5)  # 4x4 grid
    await svc.on_start()
    await svc._on_depth_sample(_depth_sample(_flat_depth_grid(3000)))
    map_msg = svc.bus.latest("/map")
    assert map_msg["cells"] == 4
    assert map_msg["resolution_m"] == 0.5
    assert map_msg["origin_cell"] == 2
    assert isinstance(map_msg["grid_b64"], str) and len(map_msg["grid_b64"]) > 0


@pytest.mark.asyncio
async def test_reset_map_clears_pose_and_rebuilds_backend():
    svc = _svc()
    await svc.on_start()
    await svc._on_depth_sample(_depth_sample(_flat_depth_grid(1500)))
    old_backend = svc._backend
    result = await svc.m_reset_map()
    assert result == {"reset": True}
    assert svc._pose == (0.0, 0.0, 0.0)
    assert svc._backend is not old_backend


# ─── point cloud ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_points_message_published_on_first_sample():
    svc = _svc()
    await svc.on_start()
    await svc._on_depth_sample(_depth_sample(_flat_depth_grid(1500)))
    points_msg = svc.bus.latest("/points")
    assert points_msg is not None
    assert points_msg["count"] > 0
    assert points_msg["voxel_size_m"] == svc.config.point_voxel_size_m
    assert isinstance(points_msg["voxels_b64"], str) and len(points_msg["voxels_b64"]) > 0
    assert len(svc._voxels) == points_msg["count"]


@pytest.mark.asyncio
async def test_point_cloud_disabled_skips_accumulation_and_publish():
    svc = _svc(point_cloud_enabled=False)
    await svc.on_start()
    await svc._on_depth_sample(_depth_sample(_flat_depth_grid(1500)))
    assert len(svc._voxels) == 0
    assert svc.bus.latest("/points") is None


@pytest.mark.asyncio
async def test_reset_map_clears_point_cloud_and_force_publishes_empty():
    svc = _svc()
    await svc.on_start()
    await svc._on_depth_sample(_depth_sample(_flat_depth_grid(1500)))
    assert len(svc._voxels) > 0
    await svc.m_reset_map()
    assert len(svc._voxels) == 0
    # force=True means the reset itself must overwrite the retained
    # /points message with an explicit empty one, not silently skip —
    # otherwise a fresh subscriber after a reset would see the STALE
    # pre-reset point cloud (retained messages don't self-expire).
    points_msg = svc.bus.latest("/points")
    assert points_msg is not None
    assert points_msg["count"] == 0


@pytest.mark.asyncio
async def test_floor_points_are_dropped_but_in_band_points_are_kept():
    """The point cloud reuses obstacle_height_min_m/max_m (the SAME band
    the 2D grid already uses) to drop floor before it ever reaches a
    voxel — confirmed live this session: without this, the point-cloud
    view was one dense dark floor mass with real objects barely
    distinguishable. A genuine floor pixel (height_above_floor == 0,
    same first-principles construction as the geometry-layer floor
    tests) should add nothing; a clearly in-band pixel should."""
    import math

    svc = _svc(camera_height_m=1.0, camera_tilt_deg=0.0, obstacle_height_min_m=0.05, obstacle_height_max_m=2.0)
    await svc.on_start()

    rows, cols = 5, 5
    hfov, vfov = math.radians(72.0), math.radians(50.0)  # SlamConfig defaults

    # A genuine floor point at row=4 (near-bottom -> steep downward pitch).
    r, c = 4, 2
    row_frac, col_frac = (r + 0.5) / rows, (c + 0.5) / cols
    v_angle = -vfov / 2 + row_frac * vfov
    h_angle = hfov / 2 - col_frac * hfov
    true_pitch = v_angle + 0.0  # tilt=0 -> true_pitch == v_angle at h_angle's small-angle center-ish column
    d_fwd = math.cos(v_angle) * math.cos(h_angle)
    floor_range_m = 1.0 / math.sin(true_pitch)  # camera_height_m=1.0, height_above_floor == 0
    floor_depth_mm = int(round(floor_range_m * d_fwd * 1000))

    grid = [[0] * cols for _ in range(rows)]
    grid[r][c] = floor_depth_mm
    await svc._on_depth_sample(_depth_sample(grid, cols=cols, rows=rows))
    assert len(svc._voxels) == 0

    # A pixel dead-center (pitch ~0) with a plain depth reading sits at
    # height_above_floor ~= camera_height_m (1.0m) -- comfortably in-band.
    grid2 = [[0] * cols for _ in range(rows)]
    grid2[2][2] = 1500
    await svc._on_depth_sample(_depth_sample(grid2, cols=cols, rows=rows))
    assert len(svc._voxels) > 0


@pytest.mark.asyncio
async def test_point_cloud_caps_at_max_voxels_evicting_oldest():
    svc = _svc(point_cloud_max_voxels=5, point_voxel_size_m=0.1)
    await svc.on_start()
    # A wide, varied grid so many distinct voxels get produced in one tick.
    grid = [[1000 + 50 * c for c in range(8)] for _ in range(6)]
    await svc._on_depth_sample(_depth_sample(grid, cols=8, rows=6))
    assert len(svc._voxels) <= 5


@pytest.mark.asyncio
async def test_point_cloud_reobserved_voxel_not_evicted_early():
    """A voxel seen again moves to the end of the LRU order — repeatedly
    reobserving ONE voxel while feeding enough NEW voxels to exceed the
    cap several times over should never evict the reobserved one."""
    # Wide-open height band — this test is about LRU eviction order, not
    # height filtering, and the geometry that lands a single fixed pixel
    # in-band at the default camera_height_m/tilt is fiddly to reason
    # about; a wide band sidesteps that entirely.
    svc = _svc(
        point_cloud_max_voxels=3, point_voxel_size_m=0.1,
        obstacle_height_min_m=-10.0, obstacle_height_max_m=10.0,
    )
    await svc.on_start()
    # rows=2 (not 1): row 0 is always excluded by depth_border_margin_rows
    # (default 1), so a 1-row grid would yield zero points.
    stable_grid = _flat_depth_grid(1000, cols=1, rows=2)  # always the same single point (row 1)
    await svc._on_depth_sample(_depth_sample(stable_grid, cols=1, rows=2))
    assert len(svc._voxels) == 1
    stable_key = next(iter(svc._voxels))
    for i in range(10):
        # A different flat depth each tick -> a different voxel each time.
        churn_grid = _flat_depth_grid(1000 + i * 200, cols=1, rows=2)
        await svc._on_depth_sample(_depth_sample(churn_grid, cols=1, rows=2))
        await svc._on_depth_sample(_depth_sample(stable_grid, cols=1, rows=2))  # reobserve
    assert stable_key in svc._voxels


# ─── true-color point cloud (oakd depth_align_to_rgb) ────────────────────

def _flat_rgb_grid(rgb, cols=4, rows=3):
    return [[list(rgb) for _ in range(cols)] for _ in range(rows)]


@pytest.mark.asyncio
async def test_depth_sample_with_rgb_colors_the_voxels_and_publishes_colors_b64():
    svc = _svc()
    await svc.on_start()
    sample = _depth_sample(_flat_depth_grid(1500))
    sample["rgb"] = _flat_rgb_grid((10, 20, 30))
    await svc._on_depth_sample(sample)
    assert len(svc._voxels) > 0
    assert all(color == (10, 20, 30) for color in svc._voxels.values())
    points_msg = svc.bus.latest("/points")
    assert isinstance(points_msg.get("colors_b64"), str) and len(points_msg["colors_b64"]) > 0
    colors = np.frombuffer(base64.b64decode(points_msg["colors_b64"]), dtype=np.uint8).reshape(-1, 3)
    assert colors.shape[0] == points_msg["count"]
    assert (colors == np.array([10, 20, 30], dtype=np.uint8)).all()


@pytest.mark.asyncio
async def test_depth_sample_without_rgb_omits_colors_b64():
    """No oakd depth_align_to_rgb -> no `rgb` field on depth_sample -> the
    /points payload shouldn't carry a same-size-as-voxels all-zeros
    colors_b64 nobody asked for (see _publish_points' docstring)."""
    svc = _svc()
    await svc.on_start()
    await svc._on_depth_sample(_depth_sample(_flat_depth_grid(1500)))
    points_msg = svc.bus.latest("/points")
    assert "colors_b64" not in points_msg


@pytest.mark.asyncio
async def test_voxel_color_updates_to_latest_observation():
    svc = _svc()
    await svc.on_start()
    grid = _flat_depth_grid(1500)
    first = _depth_sample(grid)
    first["rgb"] = _flat_rgb_grid((10, 20, 30))
    await svc._on_depth_sample(first)
    assert all(color == (10, 20, 30) for color in svc._voxels.values())

    second = _depth_sample(grid)
    second["rgb"] = _flat_rgb_grid((200, 210, 220))
    await svc._on_depth_sample(second)
    assert all(color == (200, 210, 220) for color in svc._voxels.values())


@pytest.mark.asyncio
async def test_malformed_rgb_grid_falls_back_to_uncolored_not_fatal():
    svc = _svc()
    await svc.on_start()
    sample = _depth_sample(_flat_depth_grid(1500))
    sample["rgb"] = [[1, 2, 3]]  # wrong shape entirely — not (rows, cols, 3)
    await svc._on_depth_sample(sample)  # must not raise
    assert len(svc._voxels) > 0
    assert all(color is None for color in svc._voxels.values())
    assert "colors_b64" not in svc.bus.latest("/points")


# ─── landmarks ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_landmark_projects_from_current_pose():
    svc = _svc()
    await svc.on_start()
    # Robot at the origin, facing +x (theta=0) — "6 feet directly ahead"
    # should land on the +x axis.
    result = await svc.m_add_landmark("target-1", range_m=1.8288)
    assert result["name"] == "target-1"
    assert result["x_m"] == pytest.approx(1.8288, abs=1e-6)
    assert result["y_m"] == pytest.approx(0.0, abs=1e-6)
    assert svc._landmarks["target-1"] == {"x_m": pytest.approx(1.8288), "y_m": pytest.approx(0.0)}
    published = svc.bus.latest("/landmarks")
    assert published["landmarks"] == [{"name": "target-1", "x_m": pytest.approx(1.8288), "y_m": pytest.approx(0.0)}]


@pytest.mark.asyncio
async def test_add_landmark_accounts_for_robot_heading_and_bearing():
    svc = _svc()
    await svc.on_start()
    svc._pose = (1.0, 1.0, 90.0)  # robot at (1,1), facing world +y
    # bearing=0 (straight ahead, i.e. world +y from here) at range 2.
    result = await svc.m_add_landmark("t", range_m=2.0, bearing_deg=0.0)
    assert result["x_m"] == pytest.approx(1.0, abs=1e-6)
    assert result["y_m"] == pytest.approx(3.0, abs=1e-6)


@pytest.mark.asyncio
async def test_add_landmark_with_footprint_becomes_a_bounding_box():
    svc = _svc()
    await svc.on_start()
    result = await svc.m_add_landmark("target-1", range_m=0.9144, width_m=0.3556, depth_m=0.1524)
    assert result["width_m"] == pytest.approx(0.3556)
    assert result["depth_m"] == pytest.approx(0.1524)
    # theta=0, bearing=0 -> heading_deg == 0 (mod 360).
    assert result["heading_deg"] == pytest.approx(0.0)
    published = svc.bus.latest("/landmarks")["landmarks"][0]
    assert published["width_m"] == pytest.approx(0.3556)
    assert published["depth_m"] == pytest.approx(0.1524)


@pytest.mark.asyncio
async def test_add_landmark_footprint_heading_is_a_snapshot_of_current_pose():
    svc = _svc()
    await svc.on_start()
    svc._pose = (0.0, 0.0, 30.0)
    result = await svc.m_add_landmark("t", range_m=1.0, bearing_deg=15.0, width_m=0.2, depth_m=0.1)
    assert result["heading_deg"] == pytest.approx(45.0)


@pytest.mark.asyncio
async def test_add_landmark_without_both_dimensions_stays_a_point_marker():
    svc = _svc()
    await svc.on_start()
    # Only one of width_m/depth_m given — not enough to define a box.
    result = await svc.m_add_landmark("t", range_m=1.0, width_m=0.3556)
    assert "width_m" not in result
    assert "depth_m" not in result
    assert "heading_deg" not in result


@pytest.mark.asyncio
async def test_add_landmark_same_name_replaces_not_duplicates():
    svc = _svc()
    await svc.on_start()
    await svc.m_add_landmark("target-1", range_m=1.0)
    await svc.m_add_landmark("target-1", range_m=2.0)
    assert len(svc._landmarks) == 1
    assert svc._landmarks["target-1"]["x_m"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_reset_map_does_not_clear_landmarks():
    # Deliberately kept across a reset — see m_reset_map's docstring for
    # why (they went stale relative to the new origin in theory, but in
    # practice a reset happens far more often with the robot stationary,
    # where losing a placed reference target on every reset was pure
    # friction, confirmed repeatedly in a live calibration session).
    svc = _svc()
    await svc.on_start()
    await svc.m_add_landmark("target-1", range_m=1.0)
    assert svc._landmarks
    await svc.m_reset_map()
    assert svc._landmarks
    assert svc.bus.latest("/landmarks")["landmarks"] != []


@pytest.mark.asyncio
async def test_state_reports_connection_flags():
    svc = _svc()
    await svc.on_start()
    state = svc.bus.latest("/state")
    assert state["motor_control_connected"] is False
    assert state["oakd_connected"] is False
    await svc._on_depth_sample(_depth_sample(_flat_depth_grid(2000)))
    await svc._publish_state()
    state2 = svc.bus.latest("/state")
    assert state2["oakd_connected"] is True


# ─── Phase 4b: visual odometry wiring — repo/slam/1.0.0/README.md § Visual odometry ────────────

def _feature_flow(matches) -> Dict[str, Any]:
    return {"ts": 0.0, "matches": matches}


def _oakd_state(intrinsics) -> Dict[str, Any]:
    return {"connected": True, "intrinsics": intrinsics}


@pytest.mark.asyncio
async def test_motor_control_mode_does_not_subscribe_to_feature_flow():
    svc = _svc(odometry_source="motor_control")
    await svc.on_start()
    assert "/oakd/oakd-1/feature_flow_sample" not in svc.bus.subscriptions
    assert "/oakd/oakd-1/state" not in svc.bus.subscriptions
    assert "/motor_control/mc-1/state" in svc.bus.subscriptions  # unchanged


@pytest.mark.asyncio
async def test_visual_flow_mode_subscribes_to_derived_feature_flow_and_oakd_state():
    svc = _svc(odometry_source="visual_flow")
    await svc.on_start()
    assert "/oakd/oakd-1/feature_flow_sample" in svc.bus.subscriptions
    assert "/oakd/oakd-1/state" in svc.bus.subscriptions
    assert "/motor_control/mc-1/state" in svc.bus.subscriptions  # still subscribed — §9.7 fallback input


@pytest.mark.asyncio
async def test_visual_flow_mode_respects_feature_flow_topic_override():
    svc = _svc(odometry_source="visual_flow", feature_flow_topic="/oakd/other-cam/feature_flow_sample")
    await svc.on_start()
    assert "/oakd/other-cam/feature_flow_sample" in svc.bus.subscriptions
    assert "/oakd/oakd-1/feature_flow_sample" not in svc.bus.subscriptions


@pytest.mark.asyncio
async def test_motor_control_mode_still_integrates_directly():
    """Sanity check: switching the default to "motor_control" (explicit
    here, matches the config default) must reproduce the pre-Phase-4b
    behavior exactly — _on_feature_flow is never wired up, so all
    integration still flows through _on_motor_state as before."""
    svc = _svc(odometry_source="motor_control")
    await svc.on_start()
    await svc._on_motor_state(_motor_state(left=1.0, right=1.0))  # baseline tick
    await svc._on_motor_state(_motor_state(left=1.0, right=1.0))  # second tick — now integrates
    assert svc._pending_dx_m > 0.0  # moved forward — direct motor integration still works


@pytest.mark.asyncio
async def test_visual_flow_mode_does_not_integrate_from_motor_state_directly():
    """In visual_flow mode, _on_motor_state must NOT feed the pending
    accumulator itself (that would double-count alongside visual
    updates) — it only caches the commanded speed for §9.7's fallback."""
    svc = _svc(odometry_source="visual_flow")
    await svc.on_start()
    await svc._on_motor_state(_motor_state(left=1.0, right=1.0))
    await svc._on_motor_state(_motor_state(left=1.0, right=1.0))
    assert svc._pending_dx_m == 0.0  # untouched — motor_control is a standby input only
    assert svc._last_motor_speeds == (1.0, 1.0)  # but the fallback cache IS kept current


@pytest.mark.asyncio
async def test_oakd_state_caches_intrinsics():
    svc = _svc(odometry_source="visual_flow")
    await svc.on_start()
    assert svc._oakd_intrinsics is None
    await svc._on_oakd_state(_oakd_state({"fx": 450.0, "fy": 450.0, "cx": 320.0, "cy": 240.0}))
    assert svc._oakd_intrinsics == {"fx": 450.0, "fy": 450.0, "cx": 320.0, "cy": 240.0}


@pytest.mark.asyncio
async def test_feature_flow_first_tick_establishes_baseline_without_moving():
    svc = _svc(odometry_source="visual_flow")
    await svc.on_start()
    await svc._on_feature_flow(_feature_flow([{"id": 1, "prev_uv": [1, 1], "curr_uv": [1, 1],
                                                "prev_depth_mm": 1000, "curr_depth_mm": 1000}]))
    assert svc._pending_dx_m == 0.0
    assert svc._pending_dtheta_deg == 0.0


@pytest.mark.asyncio
async def test_feature_flow_falls_back_to_dead_reckoning_without_intrinsics():
    """No /oakd/<id>/state has arrived yet (or it arrived without
    intrinsics) — estimate_visual_pose_delta can't back-project
    anything, so §9.7's fallback to commanded dead-reckoning kicks in
    using whatever _last_motor_speeds currently holds."""
    svc = _svc(odometry_source="visual_flow")
    await svc.on_start()
    await svc._on_motor_state(_motor_state(left=1.0, right=1.0))  # cache a nonzero commanded speed
    assert svc._oakd_intrinsics is None  # never arrived
    await svc._on_feature_flow(_feature_flow([{"id": 1, "prev_uv": [1, 1], "curr_uv": [1, 1],
                                                "prev_depth_mm": 1000, "curr_depth_mm": 1000}]))  # baseline tick
    await svc._on_feature_flow(_feature_flow([{"id": 1, "prev_uv": [1, 1], "curr_uv": [1, 1],
                                                "prev_depth_mm": 1000, "curr_depth_mm": 1000}]))  # integrates
    assert svc._pending_dx_m > 0.0  # fell back to the cached commanded speed, not stuck at zero


@pytest.mark.asyncio
async def test_feature_flow_malformed_payload_is_ignored_not_fatal():
    svc = _svc(odometry_source="visual_flow")
    await svc.on_start()
    await svc._on_feature_flow("not a dict")
    await svc._on_feature_flow({"ts": 0.0})  # missing "matches" entirely
    assert svc._pending_dx_m == 0.0
    assert svc._pending_dtheta_deg == 0.0


@pytest.mark.asyncio
async def test_state_reports_odometry_source():
    svc = _svc(odometry_source="visual_flow")
    await svc.on_start()
    state = svc.bus.latest("/state")
    assert state["odometry_source"] == "visual_flow"
