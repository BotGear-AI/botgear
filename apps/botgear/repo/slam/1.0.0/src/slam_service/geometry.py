# unmanaged
"""Pure-math geometry: differential-drive odometry integration +
camera depth-sample -> robot-frame range-scan projection. See
README.md § Pipeline steps 1-2. No image library needed — plain
trig + numpy.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .config import SlamConfig


@dataclass(frozen=True)
class PoseDelta:
    """A LOCAL (robot-frame) motion delta for one odometry update —
    dx/dy in meters using the robot's own +x=forward, +y=left
    convention, dtheta in degrees (+ = counterclockwise/left turn).
    This is what feeds MappingBackend.update() as `pose_change` — not a
    world-frame delta; placing it in the world frame is the backend's
    job (see README.md § Mapping backends), so a correcting backend can
    override it."""
    dx_m: float
    dy_m: float
    dtheta_deg: float


def differential_drive_delta(
    left_speed_m_s: float, right_speed_m_s: float, track_width_m: float, dt_s: float,
) -> PoseDelta:
    """Exact-arc local-frame odometry delta for one timestep, given
    left/right wheel speeds (m/s — already converted from commanded
    motor output via SlamConfig.wheel_speed_calibration_m_per_s) and
    the elapsed time.

    Standard unicycle/differential-drive integration. Falls back to
    straight-line motion when angular velocity is ~0 (the arc formula
    has a removable singularity there)."""
    v = (left_speed_m_s + right_speed_m_s) / 2.0
    w = (right_speed_m_s - left_speed_m_s) / track_width_m  # rad/s
    dtheta = w * dt_s
    if abs(w) < 1e-9:
        return PoseDelta(dx_m=v * dt_s, dy_m=0.0, dtheta_deg=math.degrees(dtheta))
    radius = v / w
    dx = radius * math.sin(dtheta)
    dy = radius * (1.0 - math.cos(dtheta))
    return PoseDelta(dx_m=dx, dy_m=dy, dtheta_deg=math.degrees(dtheta))


def apply_pose_delta(x_m: float, y_m: float, theta_deg: float, delta: PoseDelta) -> Tuple[float, float, float]:
    """World-frame pose update given a previous world pose + a local
    (robot-frame) delta — standard 2D rigid-body composition."""
    theta = math.radians(theta_deg)
    x = x_m + delta.dx_m * math.cos(theta) - delta.dy_m * math.sin(theta)
    y = y_m + delta.dx_m * math.sin(theta) + delta.dy_m * math.cos(theta)
    new_theta = (theta_deg + delta.dtheta_deg) % 360.0
    return x, y, new_theta


@dataclass(frozen=True)
class ScanPoint:
    """One bearing of a 2D range scan, in the ROBOT's own frame (0 deg
    = straight ahead, + = toward the robot's left — standard math
    convention). `range_m=None` means no obstacle was seen along this
    bearing within the configured max range (open/floor only)."""
    bearing_deg: float
    range_m: Optional[float]


def project_depth_sample_to_scan(
    depth_mm: Sequence[Sequence[int]], config: SlamConfig, max_range_m: float = 8.0,
) -> List[ScanPoint]:
    """Collapse an oakd depth_sample grid (already metric millimeters —
    see README.md § Ownership split's correction from raw disparity) into a 2D
    range scan in the robot's own frame, one ScanPoint per column.

    For each column, scans down through its rows (skipping
    `config.depth_border_margin_rows` invalid-sensor-border rows at the
    top — see that field's docstring) and reports the range to the
    CLOSEST pixel classified as an obstacle — height above the floor
    plane within [obstacle_height_min_m, obstacle_height_max_m].
    Floor-height and out-of-band-high pixels (doorway lintels, etc.) are
    skipped. A column with no qualifying pixel reports range_m=None.

    `depth_mm` is Z-depth (perpendicular distance to the image plane,
    the standard depth-camera convention — same as RealSense/Kinect/
    OAK-D), NOT the Euclidean range along each ray, and NOT the ray's
    true elevation angle either — both need the pixel's full angular
    offset from the optical axis (h_angle AND v_angle TOGETHER), not
    just one or the other. A flat surface at a constant true distance
    reads the SAME z-depth in every column even though an edge column's
    ray travels a longer diagonal path to reach it, so z-depth is
    corrected to true range via the pixel's combined off-axis angle
    (confirmed live on real hardware: without any correction, a flat
    wall rendered as an arc bowed toward the robot). Height above the
    floor needs the ray's TRUE elevation from world-horizontal, which
    at nonzero h_angle is NOT simply `camera_tilt + v_angle` — tilting
    the camera and looking off-axis horizontally at the same time is a
    compound 3D rotation, and naively adding the angles only happens to
    be correct at h_angle=0 (dead center column). Confirmed live on
    real hardware, this session, at a steep 45deg mount: genuine floor
    readings at moderate h_angle were computing height_above_floor
    around -1m instead of ~0 from exactly this shortcut — badly enough
    that a real object (a box, centered in the camera's own view) never
    passed the obstacle-height check at all, while implausible edge-
    noise pixels near h_angle=0 still passed it by coincidence. Fixed by
    building the camera-local ray as a proper unit vector (azimuth
    h_angle, elevation v_angle, standard pinhole convention) and
    rotating it by camera_tilt before reading off both the true range
    and the true elevation — see the per-pixel comments below for the
    derivation.

    bearing_deg is the TRUE tilt-rotated azimuth of whichever pixel won
    (set best_range_m), not the column's raw h_angle — h_angle alone is
    only exact at v_angle=0 (dead-center row) AND h_angle=0 together;
    off either, camera_tilt rotates part of the ray's "forward-ness"
    into "up/down," which changes its apparent horizontal bearing once
    projected back onto the world-horizontal plane. This used to be
    left as the simpler per-column h_angle-only approximation
    (revisited per that plan, below); confirmed live it's measurably
    off enough to matter: a flat obstacle face directly ahead — which
    should ray-cast into a straight line perpendicular to the 0deg axis
    — instead bowed, because each column's closest in-band row sits at
    a different v_angle and each therefore needs a DIFFERENT bearing
    correction, not the one constant h_angle the column shares. Still
    one ScanPoint per column (unchanged shape) — only the bearing
    attached to that column's winning pixel is now that pixel's own
    true azimuth rather than the column-wide approximation. A column
    with no qualifying pixel (best_range_m stays None) has no winning
    pixel to correct from, so it falls back to the column's h_angle-only
    bearing — consistent with the past behavior for "nothing seen"
    columns, and the only case ScanPoint.range_m is None.
    """
    rows = len(depth_mm)
    if rows == 0:
        return []
    cols = len(depth_mm[0])
    if cols == 0:
        return []
    scan: List[ScanPoint] = []

    # Rows 0..margin-1 are the decimated grid's TOP border — see
    # SlamConfig.depth_border_margin_rows's docstring for why this is
    # skipped rather than trusted like the rest of the grid. Clamped so a
    # misconfigured margin >= rows can't skip the whole grid silently.
    row_start = max(0, min(rows, config.depth_border_margin_rows))

    cam_tilt = math.radians(config.camera_tilt_deg)
    cam_yaw = math.radians(config.camera_yaw_deg)
    hfov = math.radians(config.camera_hfov_deg)
    vfov = math.radians(config.camera_vfov_deg)

    for c in range(cols):
        col_frac = (c + 0.5) / cols
        # Column 0 is the LEFT edge of the depth image (standard
        # row-major image convention — no flip anywhere in oakd's
        # capture/decimation pipeline), which for a normal forward-
        # facing camera is the scene to the camera's own LEFT. That
        # must map to the MOST POSITIVE h_angle to match this file's
        # "0=ahead, +=robot's own left" bearing convention (ScanPoint's
        # docstring, matches PoseDelta's). Confirmed backwards (live,
        # real hardware, this session): with the previous sign here,
        # scanned obstacle cells swept the wrong way around the robot
        # as it rotated, even after the pose heading arrow itself was
        # independently confirmed correct via the same rotation.
        h_angle = hfov / 2 - col_frac * hfov
        bearing = cam_yaw + h_angle  # fallback for a column with no winning pixel — see docstring

        # cos/sin(h_angle) are constant for this column — hoisted out of
        # the row loop below rather than recomputed per row. ch guarded
        # away from 0 (a |h_angle| >= 90 deg column, only reachable with
        # a misconfigured camera_hfov_deg well past any real camera's
        # actual field of view) so a bad config degrades this column to
        # "nothing seen" instead of a division blowup.
        ch = math.cos(h_angle)
        sh = math.sin(h_angle)
        if ch < 1e-3:
            scan.append(ScanPoint(bearing_deg=math.degrees(bearing), range_m=None))
            continue

        ct, st = math.cos(cam_tilt), math.sin(cam_tilt)

        best_range_m: Optional[float] = None
        best_bearing = bearing
        for r in range(row_start, rows):
            depth = depth_mm[r][c]
            if depth is None or depth <= 0:
                continue  # invalid/no reading
            z_depth_m = depth / 1000.0

            row_frac = (r + 0.5) / rows
            v_angle = -vfov / 2 + row_frac * vfov  # camera-local elevation offset, + = down
            cv, sv = math.cos(v_angle), math.sin(v_angle)

            # Camera-local unit ray direction BEFORE tilt (pinhole
            # azimuth/elevation convention): forward shrinks off-axis in
            # EITHER direction, left depends only on h_angle, up only on
            # v_angle. See the docstring above for why h_angle and
            # v_angle can't be corrected for independently once tilt
            # mixes them.
            d_fwd = cv * ch
            d_up = -sv
            # Apply camera_tilt — a rotation about the LEFT axis, so it
            # leaves the left component alone and only mixes forward/up.
            w_fwd = d_fwd * ct + d_up * st
            w_up = -d_fwd * st + d_up * ct
            if d_fwd < 1e-3:
                continue  # ray this far off-axis — treat as no reading, not a division blowup

            # z_depth is the point's distance along the camera's OWN
            # optical axis (standard depth-camera definition) — d_fwd
            # (this ray's forward component BEFORE tilt) is exactly the
            # cosine of its total angular offset from that axis, so
            # dividing recovers the true Euclidean range.
            range_m = z_depth_m / d_fwd
            if range_m > max_range_m:
                continue

            # True elevation from WORLD-horizontal (not camera_tilt +
            # v_angle — see docstring): w_up is the ray's up-component
            # in world axes after tilt, clamped for float safety at the
            # +/-1 boundary.
            true_pitch = math.asin(max(-1.0, min(1.0, -w_up)))

            # Height above the FLOOR (not the camera) that this pixel's
            # 3D point sits at: camera height, minus however far the ray
            # has dropped by `range_m` at its TRUE pitch.
            height_above_floor = config.camera_height_m - range_m * math.sin(true_pitch)

            if config.obstacle_height_min_m <= height_above_floor <= config.obstacle_height_max_m:
                if best_range_m is None or range_m < best_range_m:
                    best_range_m = range_m
                    # This winning pixel's TRUE azimuth: w_left is the
                    # pre-tilt "left" component (cv*sh — camera_tilt
                    # rotates about the left axis, so it's unaffected by
                    # tilt, same reasoning as w_fwd/w_up above), and
                    # w_fwd is that same true-forward component already
                    # computed for the range correction. atan2 of the
                    # two is this ray's actual horizontal bearing after
                    # projecting onto the world-horizontal plane — h_angle
                    # only coincides with this at v_angle=0 (see docstring).
                    w_left = cv * sh
                    best_bearing = cam_yaw + math.atan2(w_left, w_fwd)

        scan.append(ScanPoint(bearing_deg=math.degrees(best_bearing), range_m=best_range_m))

    return scan


def _iter_depth_sample_points(
    depth_mm: Sequence[Sequence[int]], config: SlamConfig, max_range_m: float,
):
    """Shared per-pixel back-projection loop behind both
    ``project_depth_sample_to_points`` and
    ``project_depth_sample_to_points_colored`` — yields
    ``(r, c, x_m, y_m, z_m)`` for every valid pixel, robot-frame, same
    derivation as ``project_depth_sample_to_scan`` (see its docstring
    for why true range/pitch/azimuth rather than the naive per-axis
    shortcuts). Extracted so the colored variant can pair each point
    with a same-``(r, c)`` lookup into a separately-decimated RGB grid
    without re-deriving or risking drift from this math. Yields the
    grid indices too (not just x/y/z) for exactly that lookup — the
    plain (uncolored) function just discards them.
    """
    rows = len(depth_mm)
    if rows == 0:
        return
    cols = len(depth_mm[0])
    if cols == 0:
        return
    row_start = max(0, min(rows, config.depth_border_margin_rows))

    cam_tilt = math.radians(config.camera_tilt_deg)
    cam_yaw = math.radians(config.camera_yaw_deg)
    hfov = math.radians(config.camera_hfov_deg)
    vfov = math.radians(config.camera_vfov_deg)
    ct, st = math.cos(cam_tilt), math.sin(cam_tilt)

    for c in range(cols):
        col_frac = (c + 0.5) / cols
        h_angle = hfov / 2 - col_frac * hfov
        ch, sh = math.cos(h_angle), math.sin(h_angle)
        if ch < 1e-3:
            continue

        for r in range(row_start, rows):
            depth = depth_mm[r][c]
            if depth is None or depth <= 0:
                continue
            z_depth_m = depth / 1000.0

            row_frac = (r + 0.5) / rows
            v_angle = -vfov / 2 + row_frac * vfov
            cv, sv = math.cos(v_angle), math.sin(v_angle)

            d_fwd = cv * ch
            d_left = cv * sh   # pre-tilt "left" component — unaffected by tilt (rotation about the left axis)
            d_up = -sv
            if d_fwd < 1e-3:
                continue

            range_m = z_depth_m / d_fwd
            if range_m > max_range_m:
                continue

            w_fwd = d_fwd * ct + d_up * st
            w_up = -d_fwd * st + d_up * ct

            true_pitch = math.asin(max(-1.0, min(1.0, -w_up)))
            height_m = config.camera_height_m - range_m * math.sin(true_pitch)
            azimuth = cam_yaw + math.atan2(d_left, w_fwd)
            horiz_m = range_m * math.cos(true_pitch)

            yield r, c, horiz_m * math.cos(azimuth), horiz_m * math.sin(azimuth), height_m


def project_depth_sample_to_points(
    depth_mm: Sequence[Sequence[int]], config: SlamConfig, max_range_m: float = 8.0,
) -> List[Tuple[float, float, float]]:
    """Back-project EVERY valid depth pixel into a robot-frame 3D point
    ``(x_m forward, y_m left, z_m height above the floor)`` — the full-
    fidelity counterpart to ``project_depth_sample_to_scan``'s one-
    winner-per-column reduction. That function keeps at most one point
    per column (cols/tick, e.g. 32) because the 2D occupancy grid only
    has room for "closest obstacle at this bearing"; this function
    keeps every valid pixel (up to rows*cols/tick) for a 3D point-cloud
    map, which has no such per-bearing limit. No height-band filtering
    here — floor points are kept too, since this feeds a shape/
    visualization map, not the height-filtered obstacle costmap.

    One deliberate difference from the 2D scan: this uses the ray's
    actual HORIZONTAL distance (range_m * cos(true_pitch)) to place
    x/y, not range_m directly — the 2D scan's consumer (HandRolledBackend's
    ray-casting) treats range_m as if it were already horizontal, a
    fine approximation for near-level rays but measurably wrong for
    the steep, near-floor rays this function also keeps (e.g. ~23% off
    at a 40deg true pitch), so it isn't reused here.
    """
    return [(x, y, z) for _r, _c, x, y, z in _iter_depth_sample_points(depth_mm, config, max_range_m)]


def project_depth_sample_to_points_colored(
    depth_mm: Sequence[Sequence[int]],
    rgb_grid: Sequence[Sequence[Sequence[int]]],
    config: SlamConfig,
    max_range_m: float = 8.0,
) -> List[Tuple[Tuple[float, float, float], Tuple[int, int, int]]]:
    """Same back-projection as ``project_depth_sample_to_points``, but
    each point is paired with the RGB color oakd sampled at the SAME
    grid cell (``depth_sample``'s optional ``rgb`` field — only present
    when the oakd proxy has ``depth_align_to_rgb`` enabled, see
    ``OakdConfig``). ``rgb_grid`` MUST be the same ``(rows, cols)`` shape
    as ``depth_mm`` — oakd decimates both to the SAME grid from an
    already depth-aligned-to-RGB source, so cell ``(r, c)`` in each is
    the same physical point with no additional alignment math needed
    here, just the lookup.
    """
    out: List[Tuple[Tuple[float, float, float], Tuple[int, int, int]]] = []
    for r, c, x, y, z in _iter_depth_sample_points(depth_mm, config, max_range_m):
        cr, cg, cb = rgb_grid[r][c]
        out.append(((x, y, z), (int(cr), int(cg), int(cb))))
    return out


# ─── visual (optical-flow) odometry — README.md § Visual odometry, Phase 4b ───────
#
# Turns oakd's feature_flow_sample point correspondences into a robot-frame
# PoseDelta, as a drop-in alternative odometry source to
# differential_drive_delta above — same output type, same accumulator in
# SlamService, only the SOURCE changes (§9.4). The math: back-project each
# matched pixel+depth pair into a 3D point in the camera's own frame at
# that tick, find the rigid transform (Kabsch algorithm, RANSAC-wrapped
# for outlier rejection — §9.5 steps 1-3) that best explains the "before"
# points turning into the "after" points, then convert that camera-frame
# ego-motion into the robot's frame using the camera's fixed mount
# tilt/yaw (§9.6: held static during SLAM operation, so this conversion
# is a per-session CONSTANT, not something recomputed per frame).

# RANSAC tuning — not exposed as SlamConfig fields (only
# visual_odometry_min_matches is, per README.md § Visual odometry): these are
# generic outlier-rejection knobs, not robot-specific calibration.
_RANSAC_ITERATIONS = 200
_RANSAC_INLIER_THRESHOLD_M = 0.03


def _kabsch(P: np.ndarray, Q: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Closed-form rigid transform (R, t) minimizing
    Σ‖R·Pᵢ + t − Qᵢ‖² — the Kabsch algorithm (SVD of the centered
    cross-covariance). P, Q: (N, 3) arrays of corresponding 3D points,
    N >= 3. Includes the reflection-correction step (the `d` sign flip)
    so a degenerate/near-planar point set can't return an improper
    (mirrored, det=-1) rotation."""
    p_mean = P.mean(axis=0)
    q_mean = Q.mean(axis=0)
    Pc = P - p_mean
    Qc = Q - q_mean
    H = Pc.T @ Qc
    U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T)) or 1.0
    D = np.diag([1.0, 1.0, d])
    R = Vt.T @ D @ U.T
    t = q_mean - R @ p_mean
    return R, t


def _ransac_kabsch(
    P: np.ndarray, Q: np.ndarray,
    *, iterations: int = _RANSAC_ITERATIONS, inlier_threshold_m: float = _RANSAC_INLIER_THRESHOLD_M,
    rng: Optional[np.random.Generator] = None,
) -> Optional[Tuple[np.ndarray, np.ndarray, int]]:
    """RANSAC wrapper around ``_kabsch`` (README.md § Visual odometry step 3)
    — some correspondences will be on moving objects, people walking
    past, or just bad tracks; a plain least-squares Kabsch fit has no
    rejection ability of its own. Repeatedly fits on a minimal 3-point
    sample, scores by inlier count (residual under
    ``inlier_threshold_m``), then refits on the winning sample's full
    inlier set for a less noisy final estimate than the minimal sample
    alone. Returns ``(R, t, inlier_count)``, or None if fewer than 3
    correspondences are given at all — never raises."""
    n = P.shape[0]
    if n < 3:
        return None
    rng = rng or np.random.default_rng()
    best: Optional[Tuple[np.ndarray, np.ndarray, int]] = None
    for _ in range(iterations):
        idx = rng.choice(n, size=3, replace=False)
        try:
            R, t = _kabsch(P[idx], Q[idx])
        except np.linalg.LinAlgError:
            continue
        residuals = np.linalg.norm((P @ R.T + t) - Q, axis=1)
        inliers = int(np.count_nonzero(residuals < inlier_threshold_m))
        if best is None or inliers > best[2]:
            best = (R, t, inliers)
    if best is None:
        return None
    R0, t0, _ = best
    residuals = np.linalg.norm((P @ R0.T + t0) - Q, axis=1)
    inlier_mask = residuals < inlier_threshold_m
    if int(np.count_nonzero(inlier_mask)) < 3:
        return best
    R1, t1 = _kabsch(P[inlier_mask], Q[inlier_mask])
    return R1, t1, int(np.count_nonzero(inlier_mask))


def _camera_to_robot_rotation(camera_tilt_deg: float, camera_yaw_deg: float) -> np.ndarray:
    """3×3 rotation matrix mapping a VECTOR (a displacement — no
    translation offset; camera_x_m/camera_y_m lever-arm is deliberately
    ignored here, same approximation level project_depth_sample_to_scan
    already accepts) from the camera's own frame (x=right, y=down,
    z=forward along the optical axis — standard computer-vision
    convention) into the robot's own frame (x=forward, y=left, z=up —
    matches PoseDelta's convention).

    The mount is modeled as a pan(yaw)-then-tilt gimbal, matching
    `worke`'s actual pan/tilt hardware — see README.md § Visual odometry:
    held static during SLAM operation, so this whole matrix is a
    per-session CONSTANT (computed once from config, not per-frame).

    Verified (this session, no hardware needed): the zero-tilt/
    zero-yaw case reduces to the expected exact axis permutation, and
    the general case round-trips (robot motion -> synthetic camera
    correspondences -> recovered robot motion) for representative
    nonzero tilt/yaw values. What's NOT independently verified without
    hardware: the ABSOLUTE sign of the tilt/yaw correction for large
    mounting angles — confirm empirically in Phase 4c (§9.9) by
    physically tilting the mount a known amount and checking the sign
    of the resulting position error, same discipline as this file's
    other physically-measured constants (wheel_speed_calibration_m_per_s).
    """
    tilt = math.radians(camera_tilt_deg)
    yaw = math.radians(camera_yaw_deg)

    # Baseline axis remap at zero tilt/zero yaw: camera-forward (z) ->
    # robot-forward (x); camera-right (x) -> robot's -y (robot's +y is
    # LEFT); camera-down (y) -> robot's -z (robot's +z is UP).
    baseline = np.array([
        [0.0, 0.0, 1.0],
        [-1.0, 0.0, 0.0],
        [0.0, -1.0, 0.0],
    ])
    ct, st = math.cos(tilt), math.sin(tilt)
    undo_tilt = np.array([
        [1.0, 0.0, 0.0],
        [0.0, ct, st],
        [0.0, -st, ct],
    ])
    cy, sy = math.cos(yaw), math.sin(yaw)
    undo_yaw = np.array([
        [cy, 0.0, -sy],
        [0.0, 1.0, 0.0],
        [sy, 0.0, cy],
    ])
    return baseline @ undo_tilt @ undo_yaw


def estimate_visual_pose_delta(
    matches: List[Dict[str, Any]],
    intrinsics: Optional[Dict[str, float]],
    config: SlamConfig,
    min_matches: Optional[int] = None,
) -> Optional[PoseDelta]:
    """Turn oakd's feature_flow_sample correspondences into a robot-
    frame PoseDelta (README.md § Visual odometry) — the visual-odometry
    counterpart to differential_drive_delta above, same output type,
    meant to feed the SAME pending-delta accumulator in SlamService.

    Returns None (never raises) on any insufficient/degenerate input —
    missing intrinsics, too few correspondences, or too few RANSAC
    inliers (below ``min_matches``, defaulting to
    ``config.visual_odometry_min_matches``) — so the caller can fall
    back to commanded dead-reckoning for that one tick (§9.7) instead
    of freezing or guessing.
    """
    if not matches or not intrinsics:
        return None
    fx = intrinsics.get("fx")
    fy = intrinsics.get("fy")
    cx = intrinsics.get("cx")
    cy = intrinsics.get("cy")
    if not fx or not fy or cx is None or cy is None:
        return None

    # oakd's own _publish_feature_flow already filters to matches with
    # valid (>0) depth on both ends — this loop skips (not aborts on) a
    # malformed entry defensively, in case of a misconfigured
    # feature_flow_topic override pointing at something else entirely.
    p_rows: List[Tuple[float, float, float]] = []
    q_rows: List[Tuple[float, float, float]] = []
    for m in matches:
        try:
            pu, pv = m["prev_uv"]
            qu, qv = m["curr_uv"]
            pd = float(m["prev_depth_mm"]) / 1000.0
            qd = float(m["curr_depth_mm"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            continue
        if pd <= 0 or qd <= 0:
            continue
        p_rows.append(((pu - cx) * pd / fx, (pv - cy) * pd / fy, pd))
        q_rows.append(((qu - cx) * qd / fx, (qv - cy) * qd / fy, qd))
    if not p_rows:
        return None
    P = np.array(p_rows)
    Q = np.array(q_rows)

    fit = _ransac_kabsch(P, Q)
    if fit is None:
        return None
    R_cam, t_cam, inliers = fit
    floor = min_matches if min_matches is not None else config.visual_odometry_min_matches
    if inliers < floor:
        return None

    # (R_cam, t_cam) map an OLD-tick point's camera-frame coordinates to
    # its NEW-tick camera-frame coordinates: q = R_cam@p + t_cam (that's
    # what _kabsch/_ransac_kabsch fit). That is the INVERSE of the
    # camera's own physical motion, not the motion itself: for a static
    # point and a camera that actually rotates by ΔR and displaces by D
    # (both expressed in the camera's OLD/pre-motion axes), the
    # standard extrinsics relation is q = ΔR.T@(p - D) = ΔR.T@p - ΔR.T@D
    # — i.e. R_cam = ΔR.T and t_cam = -ΔR.T@D. Solving back:
    # ΔR = R_cam.T and D = -ΔR@t_cam = -R_cam.T@t_cam. It's ΔR/D (the
    # camera's actual rotation/displacement) that a fixed camera-mount
    # rotation converts into the robot's own motion — using R_cam/t_cam
    # directly here would silently flip the sign of every axis and the
    # turn direction (caught by this session's synthetic pure-
    # translation/pure-yaw tests, each landing exactly negated before
    # this fix).
    delta_r_cam = R_cam.T
    d_cam = -delta_r_cam @ t_cam

    r_c2r = _camera_to_robot_rotation(config.camera_tilt_deg, config.camera_yaw_deg)
    r_robot = r_c2r @ delta_r_cam @ r_c2r.T
    t_robot = r_c2r @ d_cam
    dtheta_deg = math.degrees(math.atan2(r_robot[1, 0], r_robot[0, 0]))
    return PoseDelta(dx_m=float(t_robot[0]), dy_m=float(t_robot[1]), dtheta_deg=dtheta_deg)
