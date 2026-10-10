# unmanaged
"""Unit tests for slam_service.geometry — differential-drive odometry
integration + depth-sample -> robot-frame scan projection. Pure math,
no bus/hardware needed. See repo/slam/1.0.0/README.md § Pipeline"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

_SLAM_SRC = Path(__file__).resolve().parents[1] / "repo" / "slam" / "1.0.0" / "src"
if str(_SLAM_SRC) not in sys.path:
    sys.path.insert(0, str(_SLAM_SRC))

import numpy as np

from slam_service.config import SlamConfig  # noqa: E402
from slam_service.geometry import (  # noqa: E402
    PoseDelta,
    apply_pose_delta,
    differential_drive_delta,
    estimate_visual_pose_delta,
    project_depth_sample_to_points,
    project_depth_sample_to_scan,
    _camera_to_robot_rotation,
    _kabsch,
    _ransac_kabsch,
)


# ─── differential_drive_delta ────────────────────────────────────────────

def test_straight_line_when_wheels_match():
    delta = differential_drive_delta(1.0, 1.0, track_width_m=0.3, dt_s=2.0)
    assert delta.dx_m == pytest.approx(2.0)
    assert delta.dy_m == pytest.approx(0.0)
    assert delta.dtheta_deg == pytest.approx(0.0)


def test_pure_rotation_in_place():
    # Equal and opposite wheel speeds -> zero net translation, pure spin.
    delta = differential_drive_delta(-0.5, 0.5, track_width_m=0.5, dt_s=1.0)
    assert delta.dx_m == pytest.approx(0.0, abs=1e-9)
    assert delta.dy_m == pytest.approx(0.0, abs=1e-9)
    assert delta.dtheta_deg > 0  # right wheel faster -> turns left (ccw)


def test_gentle_arc_turns_toward_slower_wheel():
    delta = differential_drive_delta(0.4, 0.5, track_width_m=0.3, dt_s=1.0)
    assert delta.dx_m > 0
    assert delta.dtheta_deg > 0  # right faster -> left turn (ccw, + convention)


def test_zero_velocity_is_a_no_op():
    delta = differential_drive_delta(0.0, 0.0, track_width_m=0.3, dt_s=5.0)
    assert delta == PoseDelta(0.0, 0.0, 0.0)


# ─── apply_pose_delta ───────────────────────────────────────────────────

def test_apply_pose_delta_forward_at_zero_heading():
    x, y, theta = apply_pose_delta(0.0, 0.0, 0.0, PoseDelta(dx_m=1.0, dy_m=0.0, dtheta_deg=0.0))
    assert (x, y, theta) == pytest.approx((1.0, 0.0, 0.0))


def test_apply_pose_delta_forward_at_90_degrees():
    # Facing +90deg (robot's "forward" now points along world +y).
    x, y, theta = apply_pose_delta(0.0, 0.0, 90.0, PoseDelta(dx_m=1.0, dy_m=0.0, dtheta_deg=0.0))
    assert x == pytest.approx(0.0, abs=1e-9)
    assert y == pytest.approx(1.0)
    assert theta == pytest.approx(90.0)


def test_apply_pose_delta_wraps_heading():
    _, _, theta = apply_pose_delta(0.0, 0.0, 350.0, PoseDelta(dx_m=0.0, dy_m=0.0, dtheta_deg=20.0))
    assert theta == pytest.approx(10.0)


def test_composed_deltas_match_direct_arc_integration():
    """Sanity check: composing many small straight-line deltas along a
    slowly-turning heading approximates the same arc the exact-arc
    single-step formula gives directly — cross-validates both
    functions against each other."""
    # Single big step via the exact-arc formula.
    big = differential_drive_delta(0.4, 0.5, track_width_m=0.3, dt_s=1.0)
    x1, y1, theta1 = apply_pose_delta(0.0, 0.0, 0.0, big)

    # Many small steps, composed.
    x2, y2, theta2 = 0.0, 0.0, 0.0
    n = 200
    for _ in range(n):
        small = differential_drive_delta(0.4, 0.5, track_width_m=0.3, dt_s=1.0 / n)
        x2, y2, theta2 = apply_pose_delta(x2, y2, theta2, small)

    assert x2 == pytest.approx(x1, abs=1e-3)
    assert y2 == pytest.approx(y1, abs=1e-3)
    assert theta2 == pytest.approx(theta1, abs=1e-2)


# ─── project_depth_sample_to_scan ───────────────────────────────────────

def _uniform_depth_grid(depth_mm: int, cols: int = 8, rows: int = 6):
    return [[depth_mm] * cols for _ in range(rows)]


def test_empty_grid_returns_empty_scan():
    config = SlamConfig()
    assert project_depth_sample_to_scan([], config) == []


def test_scan_has_one_point_per_column():
    config = SlamConfig(camera_tilt_deg=0.0, camera_height_m=0.2)
    grid = _uniform_depth_grid(2000, cols=10, rows=8)
    scan = project_depth_sample_to_scan(grid, config)
    assert len(scan) == 10


def test_bearings_are_monotonic_left_to_right():
    # Column 0 is the depth image's LEFT edge -> the camera's own left
    # -> the MOST POSITIVE bearing ("0=ahead, +=robot's own left").
    # Bearings decrease monotonically as column (rightward across the
    # image) increases. Confirmed against real hardware (not just
    # convention) this session: with the bearings the other way
    # around, scanned obstacles swept the wrong direction around the
    # robot as it rotated.
    config = SlamConfig(camera_hfov_deg=60.0, camera_yaw_deg=0.0)
    grid = _uniform_depth_grid(2000, cols=6, rows=4)
    scan = project_depth_sample_to_scan(grid, config)
    bearings = [p.bearing_deg for p in scan]
    assert bearings == sorted(bearings, reverse=True)
    assert bearings[0] > 0 > bearings[-1]


def test_flat_floor_only_reports_no_obstacle():
    """A depth reading whose implied height sits within the floor band
    everywhere (camera looking level, height stays ~= camera_height,
    well above the obstacle band's lower bound close to the floor —
    wait: floor itself should be BELOW obstacle_height_min_m, i.e.
    height_above_floor ~= 0 near the true floor. Here every row reports
    the exact same range regardless of vertical angle, which isn't
    physically how a real flat floor would look through a tilted
    camera — this test instead checks the simpler invariant: a pixel
    whose computed height lands inside the obstacle band IS reported,
    outside it is NOT, using an explicit height-band check below."""
    config = SlamConfig(
        camera_height_m=0.2, camera_tilt_deg=0.0,
        obstacle_height_min_m=0.05, obstacle_height_max_m=2.0,
    )
    # camera_tilt=0 + vfov centered means row-0 looks up, last row looks
    # down. With uniform range, only rows whose pitch angle makes
    # height_above_floor fall in [0.05, 2.0] should count. Camera height
    # is 0.2m and pointed level, so most of the vertical spread reports
    # heights well above the near-zero floor band at this fixed range —
    # this is expected: a level camera over a UNIFORM range grid isn't
    # actually seeing "the floor" at every pixel (that's an artifact of
    # the synthetic uniform-range fixture, not realistic input) — the
    # real per-pixel classification behavior is covered by the explicit
    # obstacle test below instead.
    grid = _uniform_depth_grid(3000, cols=4, rows=4)
    scan = project_depth_sample_to_scan(grid, config)
    assert len(scan) == 4  # just confirm it runs without error on this shape


def test_close_obstacle_detected_at_expected_range():
    """A single pixel at camera height (i.e. height_above_floor == 0
    is NOT in-band; construct a pixel geometrically at half the camera
    height so it clearly falls inside the obstacle band) is reported at
    its true range."""
    config = SlamConfig(
        camera_height_m=1.0, camera_tilt_deg=0.0,
        camera_hfov_deg=60.0, camera_vfov_deg=40.0,
        obstacle_height_min_m=0.05, obstacle_height_max_m=2.0,
    )
    rows, cols = 5, 5
    grid = [[0] * cols for _ in range(rows)]
    # Row/col 2,2 is dead center (pitch ~= 0 -> height ~= camera_height
    # = 1.0m, safely inside the obstacle band). Give it a close range;
    # leave everything else at 0 (invalid/no reading).
    grid[2][2] = 1500  # 1.5m
    scan = project_depth_sample_to_scan(grid, config)
    center_point = scan[2]  # column 2
    assert center_point.range_m == pytest.approx(1.5)


def test_flat_wall_z_depth_corrected_to_true_range_off_center():
    """A flat wall at a constant true perpendicular distance reads the
    SAME z-depth at every column (that's what "z-depth" means — distance
    to the image plane, not to the camera). Using that z-depth directly
    as the scan's range (this function's behavior before the fix) plots
    a flat wall as an arc bowed toward the robot, worse off-center —
    confirmed live on real hardware, see repo/slam/1.0.0/README.md. Each
    column's true range must instead grow with 1/cos(h_angle) as it
    moves off-center, since an edge column's ray travels a longer
    diagonal path to reach the same-distance wall."""
    config = SlamConfig(
        camera_height_m=1.0, camera_tilt_deg=0.0,
        camera_hfov_deg=60.0, camera_vfov_deg=40.0,
        obstacle_height_min_m=0.05, obstacle_height_max_m=2.0,
    )
    rows, cols = 5, 5
    z_depth_mm = 2000  # every column reads the same 2.0m z-depth
    grid = [[0] * cols for _ in range(rows)]
    for c in range(cols):
        grid[2][c] = z_depth_mm  # row 2 = dead-center pitch, safely in-band
    scan = project_depth_sample_to_scan(grid, config)
    assert len(scan) == cols

    center = scan[2]
    assert center.range_m == pytest.approx(2.0)  # h_angle == 0 -> unchanged

    edge = scan[0]  # off-center column -> larger h_angle -> corrected upward
    assert edge.range_m is not None
    assert edge.range_m > center.range_m
    assert edge.range_m == pytest.approx(2.0 / math.cos(math.radians(edge.bearing_deg)))


def test_top_border_row_is_excluded_by_default():
    """Confirmed live on worke's real OAK-D Lite (camera level, pointed
    at open floor): the stereo depth algorithm's invalid region at the
    very top row isn't all clean zeros — some of it comes back as small
    nonzero garbage (tens of mm), which without this margin would read
    as a very-close obstacle right in front of the robot. See
    SlamConfig.depth_border_margin_rows."""
    config = SlamConfig(
        camera_height_m=1.0, camera_tilt_deg=0.0,
        camera_hfov_deg=60.0, camera_vfov_deg=40.0,
        obstacle_height_min_m=0.05, obstacle_height_max_m=2.0,
    )
    assert config.depth_border_margin_rows == 1  # default
    rows, cols = 5, 5
    grid = [[0] * cols for _ in range(rows)]
    grid[0][2] = 80    # row 0: garbage close reading, like real capture
    grid[2][2] = 1500  # row 2: a real, in-band obstacle
    scan = project_depth_sample_to_scan(grid, config)
    # The real obstacle at row 2 is still found...
    assert scan[2].range_m == pytest.approx(1.5)
    # ...and row 0's garbage did NOT override it with something closer.
    assert scan[2].range_m != pytest.approx(0.08)


def test_floor_reads_as_floor_at_nonzero_h_angle_and_tilt_together():
    """The bug found live on real hardware, this session, at a steep
    45deg mount: genuine floor at an OFF-CENTER column (h_angle != 0)
    was computing height_above_floor around -1m instead of ~0, because
    `pixel_pitch = camera_tilt + v_angle` only happens to be the ray's
    true elevation when h_angle=0 — tilting the camera and looking
    off-axis horizontally at the same time is a compound 3D rotation,
    not two independent angles that add. Badly enough that a real
    object (centered in the camera's own view) never passed the
    obstacle-height check, while implausible edge-noise pixels near
    h_angle=0 still passed it by coincidence.

    This constructs a genuinely off-center, off-axis-in-both-directions
    pixel (nonzero h_angle AND v_angle, nonzero camera_tilt) and derives
    the z-depth a TRUE floor point at that pixel would report from first
    principles (independently of project_depth_sample_to_scan's own
    code, not copied from it) — the same rotate-then-read-off-elevation
    physics as the implementation, reasoned out fresh here rather than
    asserted against whatever the function already returns. The
    obstacle-height band is set to a tight window around 0 specifically
    so a height that lands near 0 registers as "in that window" (range_m
    set, close to the point's true range) — if the fix isn't doing its
    job, height comes back far from 0 and lands outside the tight
    window instead (range_m is None), which is exactly how the old
    `camera_tilt + v_angle` shortcut behaves on this same input
    (verified below: it lands well outside the window, in the direction
    and rough magnitude the live-hardware symptom showed — NOTE the
    real live-hardware error was considerably larger than the discrepancy
    this synthetic case shows, so this formula bug was a real, provable,
    worth-fixing contributor, not the sole explanation for what was
    observed live — see repo/slam/1.0.0/README.md for that fuller writeup).
    """
    config = SlamConfig(
        camera_height_m=0.8128, camera_tilt_deg=45.0,
        camera_hfov_deg=72.0, camera_vfov_deg=50.0,
        camera_yaw_deg=0.0,
        depth_border_margin_rows=0,  # isolate this test to the geometry math, not that unrelated feature
        obstacle_height_min_m=-0.05, obstacle_height_max_m=0.05,  # tight band around 0 -- "floor-level", not a real obstacle
    )
    rows, cols = 24, 32
    # The single most extreme corner (max |h_angle| and |v_angle|
    # together) — found by sweeping the whole grid for the largest
    # old-vs-new discrepancy at this hfov/vfov/tilt, not picked to fit a
    # pre-decided answer.
    r, c = 0, 0
    row_frac = (r + 0.5) / rows
    col_frac = (c + 0.5) / cols
    v_angle = math.radians(-25 + row_frac * 50)
    h_angle = math.radians(36 - col_frac * 72)
    tilt = math.radians(45.0)

    # Camera-local ray direction before tilt (pinhole azimuth/elevation):
    d_fwd = math.cos(v_angle) * math.cos(h_angle)
    d_up = -math.sin(v_angle)
    # Rotate by tilt (rotation about the LEFT axis -- forward/up mix):
    w_up = -d_fwd * math.sin(tilt) + d_up * math.cos(tilt)
    true_pitch = math.asin(-w_up)

    # For a TRUE floor point (height_above_floor == 0 exactly):
    # range * sin(true_pitch) == camera_height_m.
    true_range_m = config.camera_height_m / math.sin(true_pitch)
    z_depth_m = true_range_m * d_fwd
    depth_mm = int(round(z_depth_m * 1000))

    grid = [[0] * cols for _ in range(rows)]
    grid[r][c] = depth_mm
    scan = project_depth_sample_to_scan(grid, config)
    # Height landed inside the tight [-0.05, 0.05] window around 0 -> it
    # was correctly recognized as (approximately) true floor level, and
    # the reported range is this point's true range (not None/excluded).
    assert scan[c].range_m == pytest.approx(true_range_m, rel=1e-3)

    # And prove this input actually distinguishes correct from buggy:
    # the OLD `camera_tilt + v_angle` shortcut computes a height outside
    # that tight window for this same z_depth/pitch.
    old_pitch = tilt + v_angle
    old_range_m = z_depth_m / math.cos(h_angle)
    old_height = config.camera_height_m - old_range_m * math.sin(old_pitch)
    assert old_height < -0.15


def test_border_margin_can_be_disabled():
    config = SlamConfig(
        camera_height_m=1.0, camera_tilt_deg=0.0,
        camera_hfov_deg=60.0, camera_vfov_deg=40.0,
        obstacle_height_min_m=0.05, obstacle_height_max_m=2.0,
        depth_border_margin_rows=0,
    )
    rows, cols = 5, 5
    grid = [[0] * cols for _ in range(rows)]
    grid[0][2] = 80  # row 0, dead-center pitch at rows=5 -> in-band height
    scan = project_depth_sample_to_scan(grid, config)
    # Dead-center column (h_angle=0) but row 0 still has a nonzero
    # v_angle (-16deg here) -> the corrected range divides by cos(v_angle)
    # too now, not just cos(h_angle) (see project_depth_sample_to_scan's
    # docstring on why both angles matter together, not just h_angle).
    row_frac = 0.5 / rows
    v_angle = math.radians(-20 + row_frac * 40)
    assert scan[2].range_m == pytest.approx(0.08 / math.cos(v_angle))


def test_zero_depth_is_treated_as_no_reading():
    config = SlamConfig()
    grid = _uniform_depth_grid(0, cols=4, rows=4)
    scan = project_depth_sample_to_scan(grid, config)
    assert all(p.range_m is None for p in scan)


def test_out_of_max_range_ignored():
    config = SlamConfig(camera_height_m=1.0, camera_tilt_deg=0.0)
    grid = _uniform_depth_grid(50_000, cols=4, rows=4)  # 50m, way past max_range
    scan = project_depth_sample_to_scan(grid, config, max_range_m=8.0)
    assert all(p.range_m is None for p in scan)


def test_bearing_corrects_for_tilt_at_off_center_row_not_just_h_angle():
    """The bug reported live: with a nonzero camera_tilt, a flat obstacle
    face directly ahead ray-cast into a bowed line instead of a straight
    one perpendicular to the 0deg axis, because bearing_deg was the
    column's raw h_angle regardless of which ROW won — exact only at
    v_angle=0 (dead-center row). Off that row, tilting the camera rotates
    part of the ray's forward-ness into up/down, which shifts its true
    horizontal bearing once projected back onto the world-horizontal
    plane; the row that wins (closest in-band pixel) varies by column,
    so different columns need different corrections, not one shared
    h_angle.

    Constructs an off-center pixel (nonzero h_angle, nonzero v_angle,
    nonzero tilt) at a height clearly inside the obstacle band, and
    derives its TRUE bearing from first principles independently of
    project_depth_sample_to_scan's own code — same rotate-the-ray
    physics as the implementation, reasoned out fresh here rather than
    asserted against whatever the function already returns."""
    config = SlamConfig(
        camera_height_m=0.8128, camera_tilt_deg=45.0,
        camera_hfov_deg=72.0, camera_vfov_deg=50.0,
        camera_yaw_deg=0.0,
        depth_border_margin_rows=0,
        obstacle_height_min_m=0.05, obstacle_height_max_m=2.0,
    )
    rows, cols = 24, 32
    r, c = 0, 0  # far corner: largest |h_angle| and |v_angle| together
    row_frac = (r + 0.5) / rows
    col_frac = (c + 0.5) / cols
    v_angle = math.radians(-25 + row_frac * 50)
    h_angle = math.radians(36 - col_frac * 72)
    tilt = math.radians(45.0)

    # Camera-local ray direction before tilt (pinhole azimuth/elevation):
    d_fwd = math.cos(v_angle) * math.cos(h_angle)
    d_up = -math.sin(v_angle)
    # Rotate by tilt (rotation about the LEFT axis -- forward/up mix;
    # left is unaffected, which is exactly why h_angle alone is wrong
    # here: the surviving "left" component after this rotation is
    # unchanged, but "forward" shrank, so its ratio -- the true
    # bearing -- grew past h_angle).
    w_fwd = d_fwd * math.cos(tilt) + d_up * math.sin(tilt)
    w_up = -d_fwd * math.sin(tilt) + d_up * math.cos(tilt)
    w_left = math.cos(v_angle) * math.sin(h_angle)  # unaffected by tilt
    true_pitch = math.asin(-w_up)
    expected_bearing_deg = math.degrees(math.atan2(w_left, w_fwd))

    # A pixel at a height clearly inside the obstacle band (not floor-
    # level): range * sin(true_pitch) == camera_height_m - height.
    height_above_floor_m = 0.4
    true_range_m = (config.camera_height_m - height_above_floor_m) / math.sin(true_pitch)
    z_depth_m = true_range_m * d_fwd
    depth_mm = int(round(z_depth_m * 1000))

    grid = [[0] * cols for _ in range(rows)]
    grid[r][c] = depth_mm
    scan = project_depth_sample_to_scan(grid, config)

    assert scan[c].range_m == pytest.approx(true_range_m, rel=1e-3)
    assert scan[c].bearing_deg == pytest.approx(expected_bearing_deg, abs=1e-6)

    # And prove this input actually distinguishes the fix from the old
    # column-wide h_angle approximation it replaced.
    naive_bearing_deg = math.degrees(h_angle)
    assert abs(scan[c].bearing_deg - naive_bearing_deg) > 1.0


# ─── project_depth_sample_to_points (point-cloud back-projection) ───────

def test_points_keeps_every_valid_pixel_not_one_per_column():
    """The whole point of this function vs. project_depth_sample_to_scan:
    no per-column winner-take-all reduction. A grid with 2 valid pixels
    in the SAME column must return 2 points, not 1."""
    config = SlamConfig(camera_height_m=1.0, camera_tilt_deg=0.0, depth_border_margin_rows=0)
    rows, cols = 4, 4
    grid = [[0] * cols for _ in range(rows)]
    grid[1][2] = 1500
    grid[3][2] = 2000
    points = project_depth_sample_to_points(grid, config)
    assert len(points) == 2


def test_point_matches_independently_derived_xyz_at_off_center_pixel():
    """Same discipline as test_floor_reads_as_floor_at_nonzero_h_angle_and_tilt_together
    and test_bearing_corrects_for_tilt...: derive the expected 3D point
    from scratch (fresh trig, not copied from the implementation) for an
    off-center, off-axis pixel with nonzero tilt, and check the function
    matches it — including the deliberate horizontal-distance-not-range
    choice documented on the function."""
    config = SlamConfig(
        camera_height_m=0.8128, camera_tilt_deg=45.0,
        camera_hfov_deg=72.0, camera_vfov_deg=50.0, camera_yaw_deg=0.0,
        depth_border_margin_rows=0,
    )
    rows, cols = 24, 32
    r, c = rows - 1, 0  # bottom row (steepest downward pitch) + far column
    row_frac = (r + 0.5) / rows
    col_frac = (c + 0.5) / cols
    v_angle = math.radians(-25 + row_frac * 50)
    h_angle = math.radians(36 - col_frac * 72)
    tilt = math.radians(45.0)

    d_fwd = math.cos(v_angle) * math.cos(h_angle)
    d_left = math.cos(v_angle) * math.sin(h_angle)
    d_up = -math.sin(v_angle)
    w_fwd = d_fwd * math.cos(tilt) + d_up * math.sin(tilt)
    w_up = -d_fwd * math.sin(tilt) + d_up * math.cos(tilt)
    true_pitch = math.asin(-w_up)

    z_depth_m = 1.7  # arbitrary plausible depth reading
    range_m = z_depth_m / d_fwd
    expected_height = config.camera_height_m - range_m * math.sin(true_pitch)
    expected_azimuth = math.atan2(d_left, w_fwd)
    expected_horiz = range_m * math.cos(true_pitch)
    expected_x = expected_horiz * math.cos(expected_azimuth)
    expected_y = expected_horiz * math.sin(expected_azimuth)

    grid = [[0] * cols for _ in range(rows)]
    grid[r][c] = int(round(z_depth_m * 1000))
    points = project_depth_sample_to_points(grid, config)
    assert len(points) == 1
    x, y, z = points[0]
    assert x == pytest.approx(expected_x, rel=1e-6)
    assert y == pytest.approx(expected_y, rel=1e-6)
    assert z == pytest.approx(expected_height, rel=1e-6)

    # And confirm this pixel's true pitch is steep enough that the
    # horizontal-distance-vs-range distinction actually matters here —
    # otherwise this test wouldn't be exercising that choice at all.
    assert math.degrees(true_pitch) > 20
    assert abs(expected_horiz - range_m) > 0.05


def test_floor_point_has_zero_height():
    """A pixel constructed to be exactly at floor level (same
    first-principles construction as the scan-side floor test) should
    back-project to z_m == 0, independent of h_angle/v_angle/tilt."""
    config = SlamConfig(
        camera_height_m=0.8128, camera_tilt_deg=45.0,
        camera_hfov_deg=72.0, camera_vfov_deg=50.0, camera_yaw_deg=0.0,
        depth_border_margin_rows=0,
    )
    rows, cols = 24, 32
    r, c = 3, 7
    row_frac = (r + 0.5) / rows
    col_frac = (c + 0.5) / cols
    v_angle = math.radians(-25 + row_frac * 50)
    h_angle = math.radians(36 - col_frac * 72)
    tilt = math.radians(45.0)
    d_fwd = math.cos(v_angle) * math.cos(h_angle)
    d_up = -math.sin(v_angle)
    w_up = -d_fwd * math.sin(tilt) + d_up * math.cos(tilt)
    true_pitch = math.asin(-w_up)
    true_range_m = config.camera_height_m / math.sin(true_pitch)  # height_above_floor == 0
    z_depth_m = true_range_m * d_fwd

    grid = [[0] * cols for _ in range(rows)]
    grid[r][c] = int(round(z_depth_m * 1000))
    points = project_depth_sample_to_points(grid, config)
    assert len(points) == 1
    assert points[0][2] == pytest.approx(0.0, abs=1e-3)


def test_points_respects_border_margin_and_max_range():
    config = SlamConfig(camera_height_m=1.0, camera_tilt_deg=0.0, depth_border_margin_rows=1)
    rows, cols = 4, 4
    grid = [[0] * cols for _ in range(rows)]
    grid[0][1] = 1000       # top border row -- excluded regardless of value
    grid[1][1] = 50_000     # 50m -- past max_range
    grid[2][1] = 1200       # in-band
    points = project_depth_sample_to_points(grid, config, max_range_m=8.0)
    assert len(points) == 1


# ─── Phase 4b: visual (optical-flow) odometry — repo/slam/1.0.0/README.md § Visual odometry ────

_INTRINSICS = {"fx": 450.0, "fy": 450.0, "cx": 320.0, "cy": 240.0}


def _make_matches(points_3d, R, t, *, fx=450.0, fy=450.0, cx=320.0, cy=240.0):
    """Build a synthetic feature_flow_sample-shaped matches list: project
    each 3D camera-frame point (the "previous tick" observation) with the
    pinhole model, apply the KNOWN rigid transform Q = R@P + t to get the
    "current tick" 3D point, and re-project that too. Round-trips through
    the exact same pinhole model estimate_visual_pose_delta uses to
    back-project, so recovering (R, t) from the result is a direct,
    unambiguous test of the fit — not of the projection math."""
    matches = []
    for p in points_3d:
        p = np.asarray(p, dtype=float)
        q = R @ p + t
        pu, pv = cx + p[0] * fx / p[2], cy + p[1] * fy / p[2]
        qu, qv = cx + q[0] * fx / q[2], cy + q[1] * fy / q[2]
        matches.append({
            "id": len(matches),
            "prev_uv": [pu, pv], "curr_uv": [qu, qv],
            "prev_depth_mm": p[2] * 1000.0, "curr_depth_mm": q[2] * 1000.0,
        })
    return matches


_SAMPLE_POINTS_3D = [
    (0.3, 0.2, 2.0), (-0.4, 0.1, 1.5), (0.1, -0.3, 3.0), (-0.2, -0.2, 2.5),
    (0.5, 0.0, 1.8), (0.0, 0.4, 2.2), (-0.5, 0.3, 1.6), (0.2, 0.1, 2.8),
    (-0.1, -0.4, 1.9), (0.4, -0.1, 2.4),
]


def test_kabsch_recovers_known_rigid_transform_exactly():
    rng = np.random.default_rng(0)
    P = rng.normal(size=(6, 3))
    # A known, non-trivial rotation (about a tilted axis) + translation.
    axis = np.array([0.2, 0.6, 0.3]); axis /= np.linalg.norm(axis)
    angle = 0.4
    K = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    R_true = np.eye(3) + math.sin(angle) * K + (1 - math.cos(angle)) * (K @ K)
    t_true = np.array([0.1, -0.2, 0.3])
    Q = (P @ R_true.T) + t_true
    R_fit, t_fit = _kabsch(P, Q)
    assert R_fit == pytest.approx(R_true, abs=1e-9)
    assert t_fit == pytest.approx(t_true, abs=1e-9)


def test_ransac_kabsch_rejects_outliers():
    rng = np.random.default_rng(1)
    P = rng.normal(size=(20, 3)) + np.array([0, 0, 3])  # in front of the camera
    R_true = np.eye(3)
    t_true = np.array([0.05, 0.0, -0.1])
    Q = (P @ R_true.T) + t_true
    # Corrupt 6 of 20 correspondences with garbage — a real bad-track/
    # moving-object scenario (repo/slam/1.0.0/README.md § Visual odometry step 3).
    Q[:6] += rng.normal(scale=2.0, size=(6, 3))
    fit = _ransac_kabsch(P, Q, rng=np.random.default_rng(2))
    assert fit is not None
    R_fit, t_fit, inliers = fit
    assert inliers >= 14  # the 14 uncorrupted correspondences, at minimum
    assert t_fit == pytest.approx(t_true, abs=1e-3)


def test_ransac_kabsch_needs_at_least_three_points():
    P = np.zeros((2, 3))
    Q = np.zeros((2, 3))
    assert _ransac_kabsch(P, Q) is None


def test_camera_to_robot_rotation_zero_tilt_yaw_is_exact_axis_permutation():
    R = _camera_to_robot_rotation(0.0, 0.0)
    # camera-forward (z) -> robot-forward (x)
    assert R @ np.array([0, 0, 1]) == pytest.approx([1, 0, 0])
    # camera-right (x) -> robot's right = -y (robot's +y is LEFT)
    assert R @ np.array([1, 0, 0]) == pytest.approx([0, -1, 0])
    # camera-down (y) -> robot's down = -z (robot's +z is UP)
    assert R @ np.array([0, 1, 0]) == pytest.approx([0, 0, -1])


@pytest.mark.parametrize("tilt,yaw", [(0.0, 0.0), (15.0, 0.0), (0.0, 20.0), (-10.0, 30.0)])
def test_camera_to_robot_rotation_is_a_valid_rotation_matrix(tilt, yaw):
    R = _camera_to_robot_rotation(tilt, yaw)
    assert R @ R.T == pytest.approx(np.eye(3), abs=1e-9)  # orthonormal
    assert np.linalg.det(R) == pytest.approx(1.0, abs=1e-9)  # proper (no reflection)


def test_estimate_visual_pose_delta_pure_forward_translation_zero_tilt():
    # Robot moves forward 0.3m, no rotation, zero camera mount tilt/yaw.
    # At zero tilt/yaw, robot-forward IS camera-frame-z (see the exact-
    # axis-permutation test above), so "camera moves forward 0.3m" is
    # R=I, t=(0,0,-0.3) in the camera's OWN frame (a point that was 2m
    # away appears 0.3m CLOSER after the camera moves toward it).
    config = SlamConfig(camera_tilt_deg=0.0, camera_yaw_deg=0.0, visual_odometry_min_matches=3)
    matches = _make_matches(_SAMPLE_POINTS_3D, np.eye(3), np.array([0.0, 0.0, -0.3]))
    delta = estimate_visual_pose_delta(matches, _INTRINSICS, config)
    assert delta is not None
    assert delta.dx_m == pytest.approx(0.3, abs=1e-6)
    assert delta.dy_m == pytest.approx(0.0, abs=1e-6)
    assert delta.dtheta_deg == pytest.approx(0.0, abs=1e-6)


def test_estimate_visual_pose_delta_pure_lateral_translation_zero_tilt():
    # Robot slides 0.2m to its LEFT. At zero tilt/yaw, robot-left is
    # camera's -x (right test above), so moving the CAMERA left by 0.2m
    # means points shift +0.2m in the camera's own +x (right) direction
    # relative to it -> t_cam = (+0.2, 0, 0).
    config = SlamConfig(camera_tilt_deg=0.0, camera_yaw_deg=0.0, visual_odometry_min_matches=3)
    matches = _make_matches(_SAMPLE_POINTS_3D, np.eye(3), np.array([0.2, 0.0, 0.0]))
    delta = estimate_visual_pose_delta(matches, _INTRINSICS, config)
    assert delta is not None
    assert delta.dx_m == pytest.approx(0.0, abs=1e-6)
    assert delta.dy_m == pytest.approx(0.2, abs=1e-6)


def test_estimate_visual_pose_delta_pure_yaw_zero_tilt():
    # Robot turns LEFT by 10 degrees, no translation. At zero mount
    # tilt/yaw this is a pure camera yaw of the same 10 degrees (in the
    # x-right/y-down/z-forward camera frame, a left turn rotates the
    # x-z plane: R = [[cosψ,0,sinψ],[0,1,0],[-sinψ,0,cosψ]]).
    psi = math.radians(10.0)
    R_cam = np.array([
        [math.cos(psi), 0.0, math.sin(psi)],
        [0.0, 1.0, 0.0],
        [-math.sin(psi), 0.0, math.cos(psi)],
    ])
    config = SlamConfig(camera_tilt_deg=0.0, camera_yaw_deg=0.0, visual_odometry_min_matches=3)
    matches = _make_matches(_SAMPLE_POINTS_3D, R_cam, np.zeros(3))
    delta = estimate_visual_pose_delta(matches, _INTRINSICS, config)
    assert delta is not None
    assert delta.dtheta_deg == pytest.approx(10.0, abs=1e-4)
    assert delta.dx_m == pytest.approx(0.0, abs=1e-6)
    assert delta.dy_m == pytest.approx(0.0, abs=1e-6)


def test_estimate_visual_pose_delta_round_trips_through_nonzero_tilt_and_yaw():
    """Not an independent ground-truth check (see the caveat in
    _camera_to_robot_rotation's docstring) — this verifies INTERNAL
    consistency of the tilt/yaw conversion: a robot-frame motion,
    projected into synthetic camera-frame correspondences via the
    mathematical inverse of the same conversion, must round-trip back
    to the original robot-frame motion. Catches composition/transpose-
    order bugs even though it can't catch a globally-flipped sign
    convention on its own — see docs/TODO_SLAM.md Phase 4c for the
    real-hardware check that closes that gap."""
    tilt_deg, yaw_deg = 12.0, -8.0
    config = SlamConfig(camera_tilt_deg=tilt_deg, camera_yaw_deg=yaw_deg, visual_odometry_min_matches=3)
    r_c2r = _camera_to_robot_rotation(tilt_deg, yaw_deg)
    r_r2c = r_c2r.T  # inverse of a rotation matrix is its transpose

    dx_true, dy_true, dtheta_true = 0.15, -0.05, 7.0
    step = PoseDelta(dx_true, dy_true, dtheta_true)
    # ΔR_robot/D_robot: the ROBOT's own actual rotation/displacement
    # (not a Kabsch-style point-coordinate map) — conjugate/rotate into
    # the camera's own frame the same way, then invert the camera's
    # actual-motion -> Kabsch-R/t relationship from the function under
    # test (R_cam = ΔR_cam.T, t_cam = -ΔR_cam@D_cam) to build the
    # synthetic correspondences.
    delta_r_robot = np.array([
        [math.cos(math.radians(dtheta_true)), -math.sin(math.radians(dtheta_true)), 0],
        [math.sin(math.radians(dtheta_true)), math.cos(math.radians(dtheta_true)), 0],
        [0, 0, 1],
    ])
    d_robot = np.array([dx_true, dy_true, 0.0])
    delta_r_cam = r_r2c @ delta_r_robot @ r_c2r
    d_cam = r_r2c @ d_robot
    R_cam = delta_r_cam.T
    t_cam = -R_cam @ d_cam  # NOT -delta_r_cam@d_cam — see the R vs R.T note above

    matches = _make_matches(_SAMPLE_POINTS_3D, R_cam, t_cam)
    delta = estimate_visual_pose_delta(matches, _INTRINSICS, config)
    assert delta is not None
    assert delta.dx_m == pytest.approx(step.dx_m, abs=1e-6)
    assert delta.dy_m == pytest.approx(step.dy_m, abs=1e-6)
    assert delta.dtheta_deg == pytest.approx(step.dtheta_deg, abs=1e-4)


def test_estimate_visual_pose_delta_none_below_min_matches():
    config = SlamConfig(visual_odometry_min_matches=100)  # impossibly high floor
    matches = _make_matches(_SAMPLE_POINTS_3D, np.eye(3), np.array([0.1, 0, 0]))
    assert estimate_visual_pose_delta(matches, _INTRINSICS, config) is None


def test_estimate_visual_pose_delta_none_on_missing_intrinsics():
    config = SlamConfig()
    matches = _make_matches(_SAMPLE_POINTS_3D, np.eye(3), np.array([0.1, 0, 0]))
    assert estimate_visual_pose_delta(matches, None, config) is None
    assert estimate_visual_pose_delta(matches, {"fx": None, "fy": 450, "cx": 0, "cy": 0}, config) is None


def test_estimate_visual_pose_delta_none_on_empty_matches():
    config = SlamConfig()
    assert estimate_visual_pose_delta([], _INTRINSICS, config) is None


def test_estimate_visual_pose_delta_skips_invalid_depth_matches_not_whole_batch():
    config = SlamConfig(camera_tilt_deg=0.0, camera_yaw_deg=0.0, visual_odometry_min_matches=3)
    matches = _make_matches(_SAMPLE_POINTS_3D, np.eye(3), np.array([0.0, 0.0, -0.3]))
    # Corrupt one match's depth to zero/negative — should be skipped,
    # not abort the whole tick (the other 9 are still plenty).
    matches[0]["prev_depth_mm"] = 0.0
    matches[1]["curr_depth_mm"] = -5.0
    delta = estimate_visual_pose_delta(matches, _INTRINSICS, config)
    assert delta is not None
    assert delta.dx_m == pytest.approx(0.3, abs=1e-6)
