# unmanaged
"""SlamService — bus wiring for the slam_service core algorithm layer.

See docs/TODO_SLAM.md §5.2/§8 Phase 2. Subscribes to `motor_control`'s
`/state` for odometry and `oakd`'s `/depth_sample` for scans, drives
the pluggable `MappingBackend` (backends/), publishes `/pose` (small,
frequent) + `/map` (throttled, quantized+base64) for the UI.

Odometry integration is deliberately decoupled from when a scan
arrives: every `motor_control` state tick composes a small local-frame
delta into a "pending since last backend update" accumulator (via the
same `apply_pose_delta` used everywhere else in this package — treating
the accumulator as its own tiny pose starting at the origin). Each
`depth_sample` flushes that accumulator into the backend as one
`PoseDelta` alongside the scan, then resets it. This means odometry
ticks and depth samples can arrive at different, independent rates
without losing motion between samples.

Wire shape for everything published (``SlamState``, ``PosePayload``,
``MapPayload``, ``PointsPayload``, ``LandmarksPayload``) is declared
below via ``state_schema``/``topic_schemas`` and introspected into
``/runtime/runtime/types/slam``. Inbound topics are the configured
motor_control/oakd instances' own published topics — see those types'
descriptors for their shapes.
"""
from __future__ import annotations

import base64
import logging
import math
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from pydantic import BaseModel, Field
from rlx_bus import SubprocessService, service_method

from .backends import MappingBackend, build_backend
from .config import SlamConfig
from .geometry import (
    PoseDelta,
    apply_pose_delta,
    differential_drive_delta,
    estimate_visual_pose_delta,
    project_depth_sample_to_points,
    project_depth_sample_to_points_colored,
    project_depth_sample_to_scan,
)


logger = logging.getLogger(__name__)

_MAP_PUBLISH_INTERVAL_S = 1.0
# A motor_control tick further apart than this is treated as a gap
# (reconnect, service restart, first tick) rather than integrated —
# guards against a stale timestamp producing a huge, garbage delta.
_MAX_ODOMETRY_DT_S = 2.0


# ── wire schemas for what this service PUBLISHES ──────────────────────

class SlamState(BaseModel):
    """Retained on ``/slam/{id}/state``."""
    motor_control_connected: bool
    oakd_connected: bool
    mapping_backend: str
    map_size_m: float
    map_resolution_m: float
    odometry_source: str


class PosePayload(BaseModel):
    """Retained on ``/slam/{id}/pose`` — small, frequent."""
    x_m: float
    y_m: float
    theta_deg: float
    ts: float


class MapPayload(BaseModel):
    """Retained on ``/slam/{id}/map`` (throttled) — occupancy grid, 1
    byte/cell row-major: 0 unknown, 85 free, 170 inflated, 255 occupied."""
    cells: int = Field(description="Grid is cells x cells")
    resolution_m: float
    origin_cell: int = Field(description="Cell index of the map origin on both axes")
    grid_b64: str
    ts: float


class PointsPayload(BaseModel):
    """Retained on ``/slam/{id}/points`` — voxel point cloud."""
    voxel_size_m: float
    count: int
    voxels_b64: str = Field(description="int16 (vx, vy, vz) voxel-grid triples; multiply by voxel_size_m for metres")
    ts: float
    colors_b64: Optional[str] = Field(None, description="uint8 RGB triples, same order; only when any voxel has color")


class Landmark(BaseModel):
    name: str
    x_m: float
    y_m: float
    width_m: Optional[float] = None
    depth_m: Optional[float] = None
    heading_deg: Optional[float] = None


class LandmarksPayload(BaseModel):
    """Retained on ``/slam/{id}/landmarks``."""
    landmarks: List[Landmark]
    ts: float


class SlamService(SubprocessService):
    type_name = "slam"
    config_class = SlamConfig
    publishes = ["state", "pose", "map", "points", "landmarks"]
    state_schema = SlamState
    topic_schemas = {
        "pose": PosePayload,
        "map": MapPayload,
        "points": PointsPayload,
        "landmarks": LandmarksPayload,
    }
    heartbeat_interval_s = 5.0

    def __init__(self, proxy_id: str, bus) -> None:
        super().__init__(proxy_id, bus)
        self._backend: Optional[MappingBackend] = None
        self._backend_key: Optional[tuple] = None
        self._pending_dx_m = 0.0
        self._pending_dy_m = 0.0
        self._pending_dtheta_deg = 0.0
        self._last_motor_ts: Optional[float] = None
        self._last_map_publish = 0.0
        self._pose = (0.0, 0.0, 0.0)
        self._motor_control_connected = False
        self._oakd_connected = False
        # Phase 4b (docs/TODO_SLAM.md §9) — visual odometry state.
        # _last_motor_speeds is cached regardless of odometry_source:
        # in "visual_flow" mode it's the fallback input (§9.7) for any
        # tick where the visual fit comes back unreliable.
        self._last_feature_flow_ts: Optional[float] = None
        self._last_motor_speeds: Tuple[float, float] = (0.0, 0.0)
        self._oakd_intrinsics: Optional[Dict[str, float]] = None
        # Manually-placed reference points (docs/TODO_SLAM.md's
        # calibration-target workflow) — a name -> world (x_m, y_m), not
        # part of the occupancy grid itself. Keyed by name so re-adding
        # the same name moves it rather than accumulating duplicates.
        self._landmarks: Dict[str, Dict[str, float]] = {}
        # World-frame point-cloud map — every valid depth pixel per tick
        # (not just the 2D grid's one-winner-per-column), voxel-bucketed
        # and LRU-capped (config.point_cloud_max_voxels). Key = integer
        # (vx, vy, vz) at config.point_voxel_size_m; value is the last
        # RGB color observed for that voxel (None when depth_sample
        # arrived with no matching color — oakd's depth_align_to_rgb is
        # off, or the voxel was captured before it was turned on). "Last
        # observed" rather than an average — cheap, and a voxel's real
        # color doesn't change tick to tick, so the newest reading is as
        # good as any blend. See _accumulate_points and
        # project_depth_sample_to_points_colored.
        self._voxels: "OrderedDict[Tuple[int, int, int], Optional[Tuple[int, int, int]]]" = OrderedDict()
        self._last_points_publish = 0.0

    # ─── lifecycle ────────────────────────────────────────────────────

    async def on_start(self) -> None:
        self._ensure_backend()
        await self._subscribe_inputs()
        await self._publish_state()
        await self._publish_landmarks()

    async def on_stop(self) -> None:
        pass  # no device handles owned here — pure consumer of bus data

    def _ensure_backend(self) -> None:
        """(Re)build the backend if its config-relevant identity
        changed — the backend owns a fixed-size grid allocated at
        construction (docs/TODO_SLAM.md §6), so a map-size/resolution/
        backend-choice change means starting a fresh map, not resizing
        one in place. Rebuilding loses the existing map — acceptable
        for a config change, which should be rare mid-session."""
        key = (self.config.mapping_backend, self.config.map_size_m, self.config.map_resolution_m)
        if self._backend is not None and key == self._backend_key:
            return
        self._backend = build_backend(self.config)
        self._backend_key = key
        logger.info("%s: mapping backend (re)built: %s", self.proxy_id, key)

    async def _subscribe_inputs(self) -> None:
        if self.config.motor_control_proxy_id:
            topic = f"/motor_control/{self.config.motor_control_proxy_id}/state"
            await self.subscribe(topic, self._on_motor_state)
            logger.info("%s: subscribed to odometry source %s", self.proxy_id, topic)
        else:
            logger.warning("%s: no motor_control_proxy_id configured — no odometry input", self.proxy_id)

        depth_topic = self.config.depth_sample_topic or (
            f"/oakd/{self.config.oakd_proxy_id}/depth_sample" if self.config.oakd_proxy_id else None
        )
        if depth_topic:
            await self.subscribe(depth_topic, self._on_depth_sample)
            logger.info("%s: subscribed to depth source %s", self.proxy_id, depth_topic)
        else:
            logger.warning(
                "%s: no oakd_proxy_id/depth_sample_topic configured — no scan input", self.proxy_id
            )

        # Phase 4b (docs/TODO_SLAM.md §9) — visual odometry input.
        # motor_control stays subscribed above regardless of mode: in
        # "visual_flow" mode it's the §9.7 fallback's input, not the
        # primary source.
        if self.config.odometry_source == "visual_flow":
            flow_topic = self.config.feature_flow_topic or (
                f"/oakd/{self.config.oakd_proxy_id}/feature_flow_sample"
                if self.config.oakd_proxy_id else None
            )
            if flow_topic:
                await self.subscribe(flow_topic, self._on_feature_flow)
                logger.info("%s: subscribed to visual odometry source %s", self.proxy_id, flow_topic)
            else:
                logger.warning(
                    "%s: odometry_source=visual_flow but no oakd_proxy_id/feature_flow_topic "
                    "configured — every tick will fall back to commanded dead-reckoning",
                    self.proxy_id,
                )
            if self.config.oakd_proxy_id:
                # Retained — subscribing gets the current intrinsics
                # immediately (published once by oakd at device-open,
                # docs/TODO_SLAM.md §9.4), no separate fetch needed.
                await self.subscribe(f"/oakd/{self.config.oakd_proxy_id}/state", self._on_oakd_state)

    # ─── odometry input ─────────────────────────────────────────────────

    async def _on_motor_state(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        if not self._motor_control_connected:
            # Rising edge only — _publish_state() on every tick would spam
            # the retained topic at the motor_control publish rate for no
            # benefit; the UI only needs to see the flag flip once.
            self._motor_control_connected = True
            await self._publish_state()
        left_out = _find_channel_output(payload.get("channels"), self.config.left_channel)
        right_out = _find_channel_output(payload.get("channels"), self.config.right_channel)
        if left_out is None or right_out is None:
            return  # configured channel id not present (yet) — skip this tick

        # Cache regardless of odometry_source — in "visual_flow" mode
        # this is the §9.7 fallback's input, not the primary source, but
        # it needs to stay current so the fallback isn't working off an
        # arbitrarily-stale commanded value.
        self._last_motor_speeds = (
            left_out * self.config.wheel_speed_calibration_m_per_s,
            right_out * self.config.wheel_speed_calibration_m_per_s,
        )
        if self.config.odometry_source != "motor_control":
            return  # visual_flow mode: _on_feature_flow drives the accumulator instead

        now = time.monotonic()
        if self._last_motor_ts is None:
            self._last_motor_ts = now
            return  # first tick — nothing to integrate yet, just establish dt baseline
        dt = now - self._last_motor_ts
        self._last_motor_ts = now
        if dt <= 0 or dt > _MAX_ODOMETRY_DT_S:
            return  # stale/reconnect gap — don't integrate a garbage delta

        left_speed, right_speed = self._last_motor_speeds
        step = differential_drive_delta(left_speed, right_speed, self.config.track_width_m, dt)
        self._pending_dx_m, self._pending_dy_m, self._pending_dtheta_deg = apply_pose_delta(
            self._pending_dx_m, self._pending_dy_m, self._pending_dtheta_deg, step,
        )

    async def _on_oakd_state(self, payload: Any) -> None:
        """Caches oakd's intrinsics (published once at device-open,
        docs/TODO_SLAM.md §9.4) for _on_feature_flow's back-projection.
        Retained topic — the first delivery on subscribe already has
        whatever oakd currently knows, this just keeps it current
        across an oakd reconnect."""
        if isinstance(payload, dict):
            self._oakd_intrinsics = payload.get("intrinsics")

    async def _on_feature_flow(self, payload: Any) -> None:
        """Visual-odometry tick (docs/TODO_SLAM.md §9.4/§9.5) — only
        wired up when odometry_source == "visual_flow" (_subscribe_inputs
        gates the subscription itself). Same accumulate-until-flush
        pattern as _on_motor_state: composes one PoseDelta into the
        SAME pending accumulator _on_depth_sample later flushes,
        regardless of which source produced it."""
        if not isinstance(payload, dict):
            return
        matches = payload.get("matches")
        if matches is None:
            return

        now = time.monotonic()
        if self._last_feature_flow_ts is None:
            self._last_feature_flow_ts = now
            return  # first tick — establish dt baseline only
        dt = now - self._last_feature_flow_ts
        self._last_feature_flow_ts = now
        if dt <= 0 or dt > _MAX_ODOMETRY_DT_S:
            return  # stale/reconnect gap — same guard as _on_motor_state

        step = estimate_visual_pose_delta(matches, self._oakd_intrinsics, self.config)
        if step is None:
            # Fallback (§9.7): too few reliable matches this tick (blank
            # wall, darkness, camera moved off anything trackable) —
            # degrade to commanded dead-reckoning for JUST this tick,
            # using the last known commanded speeds, rather than
            # freezing the pose estimate.
            left_speed, right_speed = self._last_motor_speeds
            step = differential_drive_delta(left_speed, right_speed, self.config.track_width_m, dt)

        self._pending_dx_m, self._pending_dy_m, self._pending_dtheta_deg = apply_pose_delta(
            self._pending_dx_m, self._pending_dy_m, self._pending_dtheta_deg, step,
        )

    # ─── depth input ────────────────────────────────────────────────────

    async def _on_depth_sample(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        depth_mm = payload.get("depth_mm")
        if not depth_mm:
            return
        if not self._oakd_connected:
            # Rising edge only — see the matching comment in _on_motor_state.
            self._oakd_connected = True
            await self._publish_state()
        self._ensure_backend()
        assert self._backend is not None

        scan = project_depth_sample_to_scan(depth_mm, self.config, max_range_m=self.config.max_scan_range_m)
        pose_change = PoseDelta(self._pending_dx_m, self._pending_dy_m, self._pending_dtheta_deg)
        self._pending_dx_m = self._pending_dy_m = self._pending_dtheta_deg = 0.0

        self._pose = self._backend.update(scan, pose_change)
        await self._publish_pose()

        if self.config.point_cloud_enabled:
            self._accumulate_points(depth_mm, payload.get("rgb"))

        now = time.monotonic()
        if now - self._last_map_publish >= _MAP_PUBLISH_INTERVAL_S:
            await self._publish_map()
            self._last_map_publish = now
        if self.config.point_cloud_enabled and now - self._last_points_publish >= _MAP_PUBLISH_INTERVAL_S:
            await self._publish_points()
            self._last_points_publish = now

    def _accumulate_points(self, depth_mm: Any, rgb_grid: Any = None) -> None:
        """Back-project this tick's depth sample to robot-frame points
        (project_depth_sample_to_points[_colored] — every valid pixel,
        not the 2D grid's one-winner-per-column), rotate/translate each
        into world frame using the pose this tick was just integrated
        to (same composition apply_pose_delta uses elsewhere), and
        insert into the voxel map. An already-seen voxel moves to the
        end of the LRU order instead of double-inserting — a voxel that
        keeps getting reobserved should be the LAST thing evicted, not
        eligible for eviction just because it happened to enter early
        in the session.

        ``rgb_grid`` is depth_sample's optional same-shape color grid
        (oakd's depth_align_to_rgb) — when present, the colored
        projection is used instead and each voxel's color is updated to
        the freshest reading; when absent (or malformed — defensively
        falls back rather than raising, in case of a misconfigured
        depth_sample_topic override), voxels are inserted/refreshed with
        no color, same as before this field existed.

        Reuses obstacle_height_min_m/obstacle_height_max_m (the SAME
        band the 2D grid already classifies obstacles with) to drop
        floor and implausible-height points before they ever reach a
        voxel — NOT project_depth_sample_to_points itself, which stays
        the complete, unfiltered primitive (see its docstring). This is
        a storage/display policy, not a geometry concern: floor is the
        dominant point count in any real scene (most of what a depth
        camera sees IS floor) and isn't shape-interesting on its own —
        the existing 2D grid's free/teal cells already cover "has this
        floor been seen," so dropping it here loses no information the
        rest of the map doesn't already carry, while freeing the voxel
        cap for what's actually worth looking at. Confirmed live this
        session: with floor points included, the point-cloud view was
        one dense dark mass with real objects barely distinguishable."""
        colored_points: list
        if rgb_grid:
            try:
                colored_points = project_depth_sample_to_points_colored(
                    depth_mm, rgb_grid, self.config, max_range_m=self.config.max_scan_range_m,
                )
            except (IndexError, TypeError, ValueError):
                logger.warning("%s: depth_sample.rgb malformed/mismatched shape — dropping color this tick", self.proxy_id)
                colored_points = [
                    (p, None) for p in project_depth_sample_to_points(
                        depth_mm, self.config, max_range_m=self.config.max_scan_range_m,
                    )
                ]
        else:
            colored_points = [
                (p, None) for p in project_depth_sample_to_points(
                    depth_mm, self.config, max_range_m=self.config.max_scan_range_m,
                )
            ]
        if not colored_points:
            return
        x0, y0, theta_deg = self._pose
        theta = math.radians(theta_deg)
        ct, st = math.cos(theta), math.sin(theta)
        vsize = self.config.point_voxel_size_m
        cap = self.config.point_cloud_max_voxels
        z_min = self.config.obstacle_height_min_m
        z_max = self.config.obstacle_height_max_m
        voxels = self._voxels
        for (x_r, y_r, z_r), color in colored_points:
            if z_r < z_min or z_r > z_max:
                continue
            world_x = x0 + x_r * ct - y_r * st
            world_y = y0 + x_r * st + y_r * ct
            key = (
                int(round(world_x / vsize)),
                int(round(world_y / vsize)),
                int(round(z_r / vsize)),
            )
            if key in voxels:
                voxels.move_to_end(key)
                if color is not None:
                    voxels[key] = color
            else:
                voxels[key] = color
                if len(voxels) > cap:
                    voxels.popitem(last=False)

    # ─── publish ────────────────────────────────────────────────────────

    async def _publish_state(self) -> None:
        state = SlamState(
            motor_control_connected=self._motor_control_connected,
            oakd_connected=self._oakd_connected,
            mapping_backend=self.config.mapping_backend,
            map_size_m=self.config.map_size_m,
            map_resolution_m=self.config.map_resolution_m,
            odometry_source=self.config.odometry_source,
        )
        await self.publish("state", state.model_dump(), retained=True)

    async def _publish_pose(self) -> None:
        x, y, theta = self._pose
        pose = PosePayload(x_m=x, y_m=y, theta_deg=theta, ts=time.time())
        await self.publish("pose", pose.model_dump(), retained=True)

    async def _publish_map(self) -> None:
        assert self._backend is not None
        grid = self._backend.occupancy_grid()
        inflated = self._backend.inflated_grid()
        cells = grid.shape[0]
        origin_cell = cells // 2
        packed = _quantize_grid(grid, inflated)
        grid_msg = MapPayload(
            cells=cells,
            resolution_m=self.config.map_resolution_m,
            origin_cell=origin_cell,
            grid_b64=base64.b64encode(packed.tobytes()).decode("ascii"),
            ts=time.time(),
        )
        await self.publish("map", grid_msg.model_dump(), retained=True)

    async def _publish_points(self, force: bool = False) -> None:
        """Publish the current voxel point-cloud map — packed as int16
        (vx, vy, vz) triples (voxel-grid coordinates, not meters; the
        UI multiplies by voxel_size_m), matching /map's
        base64-packed-array convention. Skipped when empty (a
        fresh-start session has nothing worth publishing yet) UNLESS
        `force` — m_reset_map needs the empty state to actually
        overwrite the old retained message, not silently no-op and
        leave a stale point cloud visible after a reset.

        ``colors_b64`` (uint8 RGB triples, SAME order/count as
        voxels_b64) is only included when at least one voxel actually
        has a color — i.e. oakd's depth_align_to_rgb has been on for at
        least one tick — so a deployment that never enables it doesn't
        pay for a same-size-as-voxels field of all-zeros every publish.
        A voxel with no color of its own (captured before depth_align_to_rgb
        was turned on, or if it's off) reports black (0,0,0) in that
        array — a known, minor cosmetic edge case, not a validity
        bitmap per point; documented rather than solved since it
        self-heals the next time that voxel is reobserved."""
        if not self._voxels and not force:
            return
        has_color = False
        if self._voxels:
            # int16 covers +/-32767 voxels — at the default 0.03m voxel
            # size that's +/-983m, far past any plausible map_size_m,
            # so clip rather than let a pathological config silently wrap.
            arr = np.array(list(self._voxels.keys()), dtype=np.int32)
            arr = np.clip(arr, -32767, 32767).astype(np.int16)
            colors = np.zeros((arr.shape[0], 3), dtype=np.uint8)
            for i, color in enumerate(self._voxels.values()):
                if color is not None:
                    colors[i] = color
                    has_color = True
        else:
            arr = np.zeros((0, 3), dtype=np.int16)  # force=True, post-reset — explicit empty shape
            colors = np.zeros((0, 3), dtype=np.uint8)
        payload = PointsPayload(
            voxel_size_m=self.config.point_voxel_size_m,
            count=int(arr.shape[0]),
            voxels_b64=base64.b64encode(arr.tobytes()).decode("ascii"),
            ts=time.time(),
            colors_b64=base64.b64encode(colors.tobytes()).decode("ascii") if has_color else None,
        )
        await self.publish("points", payload.model_dump(exclude_none=True), retained=True)

    async def _publish_landmarks(self) -> None:
        msg = LandmarksPayload(
            landmarks=[Landmark(name=name, **pos) for name, pos in self._landmarks.items()],
            ts=time.time(),
        )
        await self.publish("landmarks", msg.model_dump(exclude_none=True), retained=True)

    def meta_topics(self) -> Dict[str, str]:
        return {
            "pose": self.resolve_topic(self.topic("pose")),
            "map": self.resolve_topic(self.topic("map")),
            "landmarks": self.resolve_topic(self.topic("landmarks")),
            "points": self.resolve_topic(self.topic("points")),
        }

    def _apply_config_state(self, raw: Any) -> None:
        super()._apply_config_state(raw)
        self._ensure_backend()

    # ─── control actions ────────────────────────────────────────────────

    @service_method("reset_map")
    async def m_reset_map(self) -> Dict[str, Any]:
        """Discard the accumulated map + pose and start fresh from the
        origin — useful after a bad run (e.g. the robot was picked up
        and moved, which dead-reckoning has no way to detect).

        Landmarks are DELIBERATELY left alone. They're world (x_m, y_m)
        positions relative to the pose origin this resets, so in the
        "picked up and moved" case they do go stale — but in the FAR
        more common real usage (clearing accumulated map noise/log-odds
        while the robot hasn't actually moved, e.g. mid-calibration) a
        landmark like a placed reference target is exactly the thing an
        operator wants to SURVIVE a reset, not silently vanish — this
        flipped after repeatedly losing target-1 to resets during a
        live calibration session with the robot stationary throughout.
        Re-add (same name moves it, doesn't duplicate) if a real
        moved-robot reset does make one stale."""
        self._backend = build_backend(self.config)
        self._backend_key = (self.config.mapping_backend, self.config.map_size_m, self.config.map_resolution_m)
        self._pending_dx_m = self._pending_dy_m = self._pending_dtheta_deg = 0.0
        self._pose = (0.0, 0.0, 0.0)
        self._voxels.clear()
        await self._publish_pose()
        await self._publish_map()
        await self._publish_points(force=True)
        return {"reset": True}

    @service_method("add_landmark")
    async def m_add_landmark(
        self, name: str, range_m: float, bearing_deg: float = 0.0,
        width_m: Optional[float] = None, depth_m: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Manually mark a known reference point on the map — e.g. a
        measured calibration target placed in front of the camera.
        ``range_m``/``bearing_deg`` are ROBOT-FRAME (bearing: 0=ahead,
        + = the robot's own left, same convention as ScanPoint/PoseDelta),
        converted to a world (x_m, y_m) using the CURRENT pose, the same
        composition `hand_rolled.py`'s `_cast_ray` uses for a scan hit.
        Re-adding an existing ``name`` moves it rather than duplicating it.

        ``width_m``/``depth_m`` (both optional, both required together):
        the target's known FLOOR FOOTPRINT — ``depth_m`` along the
        bearing ray (the target's own front-to-back extent, facing the
        camera), ``width_m`` perpendicular to it — renders as a bounding
        box instead of a point marker. ``(range_m, bearing_deg)`` is
        taken as the box's CENTER, not a near/far face — the caller
        measures to whichever reference point makes sense and this just
        places a box of the given size around it. The box's world
        orientation is recorded as the robot's heading at THIS moment
        (`theta + bearing_deg`) — a snapshot, not re-derived later, same
        "known once" precedent as the camera mount extrinsics
        (docs/TODO_SLAM.md §1) since a landmark has no pose of its own to
        track. Omit both for a plain point marker (unchanged behavior).
        """
        x, y, theta = self._pose
        heading_deg = theta + bearing_deg
        heading = math.radians(heading_deg)
        landmark: Dict[str, float] = {
            "x_m": x + range_m * math.cos(heading),
            "y_m": y + range_m * math.sin(heading),
        }
        if width_m is not None and depth_m is not None:
            landmark["width_m"] = width_m
            landmark["depth_m"] = depth_m
            landmark["heading_deg"] = heading_deg % 360.0
        self._landmarks[name] = landmark
        await self._publish_landmarks()
        return {"name": name, **landmark}


def _find_channel_output(channels: Any, channel_id: str) -> Optional[float]:
    """motor_control's /state publishes `channels` as a LIST of
    per-channel dicts (each with an `id` + `output`), not a dict keyed
    by id — confirmed by reading motor_control.py's `_snapshot()`
    directly rather than assumed."""
    if not isinstance(channels, list):
        return None
    for entry in channels:
        if isinstance(entry, dict) and entry.get("id") == channel_id:
            try:
                return float(entry.get("output"))
            except (TypeError, ValueError):
                return None
    return None


def _quantize_grid(log_odds: np.ndarray, inflated: np.ndarray) -> np.ndarray:
    """Pack the grid into 1 byte/cell for the wire: 0=unknown,
    85=free, 170=inflated-but-not-raw-occupied, 255=occupied. Keeps the
    retained /map message small (cells^2 bytes, base64'd) instead of a
    full float32 array — see docs/TODO_SLAM.md's low-resource goal."""
    out = np.zeros(log_odds.shape, dtype=np.uint8)
    out[log_odds < -0.5] = 85
    out[inflated] = 170
    out[log_odds > 1.0] = 255
    return out
