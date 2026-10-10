# unmanaged
"""SlamConfig — robot dimensions, camera mount geometry, and odometry
source. See README.md § Configuration for the full field-by-field
rationale; this is deliberately where all the "enrich the localization"
knowledge lives, not in `oakd` (which stays camera-hardware-only)."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field
from rlx_bus import ServiceConfig


class SlamConfig(ServiceConfig):
    # ─── oakd depth source ──────────────────────────────────────────────
    oakd_proxy_id: Optional[str] = Field(
        None, description="Which oakd instance to consume depth_sample from."
    )
    depth_sample_topic: Optional[str] = Field(
        None, description="Override the derived oakd/<id>/depth_sample topic if needed."
    )
    feature_flow_topic: Optional[str] = Field(
        None, description="Override the derived oakd/<id>/feature_flow_sample topic if needed."
    )

    # ─── camera mount extrinsics, relative to robot center ─────────────
    # Meters/degrees. Must be physically measured once at mount time —
    # this is a one-time calibration, not something the software infers
    # (see README.md § Ownership split's "known once, not detected per-frame").
    camera_x_m: float = 0.0
    camera_y_m: float = 0.0
    camera_height_m: float = 0.2
    camera_tilt_deg: float = 0.0  # positive = tilted downward toward the floor
    camera_yaw_deg: float = 0.0   # positive = rotated toward the robot's left of forward

    # Depth sensor's (mono cameras', not RGB's) field of view — needed to
    # turn a depth_sample grid column/row index into an angle. Defaults
    # are OAK-D Lite mono-camera approximations; override for other
    # cameras/models.
    camera_hfov_deg: float = 72.0
    camera_vfov_deg: float = 50.0

    # ─── robot geometry ─────────────────────────────────────────────────
    footprint_radius_m: float = Field(
        0.25, description="Circular footprint approximation used for obstacle inflation — see README.md § Limitations"
    )
    track_width_m: float = Field(
        0.3, description="Distance between the two drive wheels — differential-drive kinematics."
    )

    # ─── odometry source ────────────────────────────────────────────────
    odometry_source: Literal["motor_control", "visual_flow"] = Field(
        "motor_control",
        description="'motor_control' (default, unchanged v1 behavior) integrates commanded "
                    "motor output — open-loop, no feedback, drifts on slip/stall (README.md "
                    "§2/§6). 'visual_flow' (README.md § Visual odometry, Phase 4b) instead estimates motion "
                    "from oakd's feature_flow_sample via a Kabsch/RANSAC rigid-motion fit, falling "
                    "back to commanded dead-reckoning for any single tick where too few reliable "
                    "matches are found (§9.7) — motor_control is still subscribed in this mode "
                    "purely as that fallback's input, not as the primary source.",
    )
    visual_odometry_min_matches: int = Field(
        8,
        description="RANSAC inlier floor below which a feature_flow_sample tick is treated as "
                    "unreliable and skipped in favor of the commanded-dead-reckoning fallback. "
                    "Placeholder default — docs/TODO_SLAM.md flags this as needing real-data "
                    "tuning, not a researched constant.",
    )
    motor_control_proxy_id: Optional[str] = None
    left_channel: str = "left"
    right_channel: str = "right"
    wheel_speed_calibration_m_per_s: float = Field(
        0.5,
        description="MUST be physically measured (drive a known distance at a known commanded "
                    "output, divide) — the software cannot infer this without additional sensing. "
                    "See README.md § Configuration Still used as the odometry source in "
                    "'motor_control' mode, and as the visual-flow fallback's input in "
                    "'visual_flow' mode (§9.7).",
    )

    # ─── depth-sample cleanup ───────────────────────────────────────────
    depth_border_margin_rows: int = Field(
        1,
        description="Decimated depth_sample rows to discard at the TOP of the grid (row index "
                    "0..margin-1), before any height/obstacle classification. Confirmed live on "
                    "worke's OAK-D Lite (camera level, pointed at open floor): the stereo depth "
                    "algorithm's invalid region at the very top edge of the frame isn't clean "
                    "zeros — most of it is, but a few pixels come back as small nonzero garbage "
                    "(tens of mm, physically implausible next to real 1-3m floor/wall readings in "
                    "the row right below), which slips past the depth<=0 'invalid' check and gets "
                    "misread as a very-close obstacle right in front of the robot. The right/left "
                    "edges have the same kind of invalid border but read clean zeros there, so "
                    "they're already excluded for free. Bottom rows weren't observed to have this "
                    "problem (and are useful — the closest floor in front of the robot), so this is "
                    "TOP-only rather than a symmetric margin on all four sides.",
    )

    # ─── obstacle classification ───────────────────────────────────────
    obstacle_height_min_m: float = Field(
        0.05, description="Below this height above the floor plane, a depth reading is floor texture, not an obstacle."
    )
    obstacle_height_max_m: float = Field(
        2.0, description="Above this height, a depth reading is ignored (e.g. a doorway lintel worke fits under)."
    )

    # ─── scan range ─────────────────────────────────────────────────────
    max_scan_range_m: float = Field(
        2.5,
        description="Depth readings beyond this range are discarded before classification/ray-"
                    "casting — not a map-size limit, a SENSOR-TRUST limit. Confirmed live on "
                    "worke's OAK-D Lite (steeply tilted down at 45deg): depth quality/confidence "
                    "degrades well before the sensor's nominal max range, and readings past "
                    "roughly 8ft (~2.5m) trend toward reporting 'floor' regardless of what's "
                    "actually there. Tune this down further if far readings still look untrustworthy, "
                    "or up if a less-steep mount angle turns out to see further reliably.",
    )

    # ─── map ────────────────────────────────────────────────────────────
    map_size_m: float = Field(
        6.0,
        description="Fixed-size square grid, centered on the robot's pose at startup — see "
                    "README.md § Limitations Sized to comfortably cover max_scan_range_m in every "
                    "direction plus room for the robot to move around within it before hitting the "
                    "(non-growing, by design) edge — not sized for the sensor's theoretical max "
                    "range, which this robot's mount angle doesn't actually reach usefully anyway.",
    )
    map_resolution_m: float = Field(
        0.02,
        description="Smaller cells than map_size_m's shrink alone would suggest — most real usage "
                    "is close-in (0-5ft), where finer resolution matters more than covering distance "
                    "the sensor can't usefully see (max_scan_range_m) anyway.",
    )

    # ─── backend selection ──────────────────────────────────────────────
    mapping_backend: str = Field(
        "hand_rolled",
        description="Which MappingBackend implementation to use — see README.md § Mapping backends "
                    "'hand_rolled' is the only implementation in v1.",
    )

    # ─── point cloud (shape/visualization map, separate from the 2D grid) ─
    point_cloud_enabled: bool = Field(
        True,
        description="Accumulate a world-frame voxel point cloud alongside the 2D occupancy grid, "
                    "from EVERY valid depth pixel (project_depth_sample_to_points), not just the "
                    "one winner-per-column the 2D grid keeps. Off saves the per-tick back-projection "
                    "+ voxel-insert cost for a deployment that only needs the 2D costmap.",
    )
    point_voxel_size_m: float = Field(
        0.03,
        description="Point-cloud voxel edge length. Coarser than map_resolution_m's 2D cells on "
                    "purpose — this map is for shape fidelity/visualization, not fine occupancy math, "
                    "and a coarser voxel means fewer, more meaningful points (repeated hits on the "
                    "same real surface collapse into one voxel instead of accumulating near-duplicates).",
    )
    point_cloud_max_voxels: int = Field(
        60_000,
        description="Cap on retained voxels, LRU-evicted (oldest NOT-recently-reobserved voxel first) "
                    "once exceeded — bounds both memory and the published payload size for a "
                    "long-running session, instead of growing without limit.",
    )
