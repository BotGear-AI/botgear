# slam — cheap 2D occupancy mapping

Builds a top-down occupancy grid of the room from an `oakd` depth camera
plus odometry, and tracks the robot's pose on it. **"Cheap" is the
point:** no loop closure, no drift correction, no relocalization — the
map drifts on long or complex runs. Pure numpy + trigonometry; no OpenCV,
no depthai dependency.

```
type:     slam
version:  1.0.0
language: python (subprocess)
status:   development
```

## Ownership split

`oakd` stays a generic sensor: it publishes a small metric depth grid and
(optionally) tracked feature correspondences, and knows nothing about the
robot. `slam` owns everything robot-specific: footprint, camera mount
extrinsics, wheel geometry, obstacle thresholds, odometry.

Inputs:
- `/oakd/<id>/depth_sample` — `{cols, rows, depth_mm: [[…]], ts}` (default
  32×24 at 2 Hz), from `StereoDepth.depth` (already metric, so no stereo
  calibration crosses the wire). Enable on oakd with
  `publish_depth_samples` (+ `depth_sample_rate_hz`, `depth_sample_cols/rows`).
- `/motor_control/<id>/state` — commanded left/right output (dead-reckoning).
- `/oakd/<id>/feature_flow_sample` — optional visual odometry (below).

## Pipeline

1. **Odometry.** Differential-drive dead-reckoning from commanded output via
   `track_width_m` and `wheel_speed_calibration_m_per_s`, accumulated into a
   pending pose delta.
2. **Scan extraction** (`geometry.project_depth_sample_to_scan`). Each depth
   column is projected through the fixed camera extrinsics into a
   robot-frame bearing + range and classified by the obstacle-height band:
   floor (keep looking along the ray), obstacle (record range), or nothing
   within `max_scan_range_m`.
3. **Backend update.** The robot-frame scan + pose delta go to a pluggable
   `MappingBackend`, which returns the pose estimate and owns the grid.
4. **Publish** on `/slam/<id>/…`.

| Topic (all retained) | Payload |
|---|---|
| `map` | occupancy grid, quantized to 1 byte/cell and base64'd |
| `pose` | `(x, y, theta)` in the map frame |
| `points` | optional world-frame voxel point cloud (`point_cloud_enabled`) |
| `landmarks` | manually marked reference points |
| `state` | service status |

**Actions:** `reset_map` (e.g. after the robot was picked up and moved),
`add_landmark(name, range_m, bearing_deg, width_m?, depth_m?)` — marks a
measured reference point using the current pose (robot frame: bearing 0 =
ahead, + = robot's left).

**Frames:** standard right-handed math convention throughout — world +x
right / +y up on the canvas, decreasing `theta_deg` is a clockwise turn.

### Mapping backends

`mapping_backend: "hand_rolled"` (the only one): integrates the pose delta
as-is and ray-casts each scan into a log-odds grid. A `BreezySlamBackend`
(scan-matching pose correction — partial drift mitigation) is the intended
swap-in, kept out for now because BreezySLAM is source-only (a compiler at
install time on the robot), unverified on ARM, and tuned for dense 2D
LIDAR rather than a sparse 32-column depth scan.

## Visual odometry (`odometry_source: visual_flow`)

Wheel encoders would be blind to wheel slip; tracking real points in the
scene isn't. With `odometry_source: visual_flow`:

- `oakd` (`publish_feature_flow`, `feature_flow_rate_hz`) runs a
  `FeatureTracker` on `mono_left` — on the VPU, with persistent track ids —
  and publishes id-matched correspondences
  `{id, prev_uv, curr_uv, prev_depth_mm, curr_depth_mm}`. `mono_left` is the
  frame depth is already defined in, so no depth alignment is needed. The
  camera intrinsics (`fx, fy, cx, cy`) ride on oakd's retained `/state`.
- `slam` back-projects both point sets, fits the rigid transform with Kabsch
  inside RANSAC (`visual_odometry_min_matches` inliers or the sample is
  rejected), inverts it into the camera's own motion, and converts that into
  a planar robot delta via the mount extrinsics. It feeds the **same**
  pending-delta accumulator dead-reckoning uses — nothing downstream changes.
- **Fallback:** when a sample is rejected (blank walls, darkness), that tick
  uses the motor_control dead-reckoning delta instead.
- **Precondition:** hold the pan/tilt head still near neutral while mapping —
  the camera→robot extrinsic is a fixed constant, not compensated per frame.

## Configuration

| Field(s) | Purpose |
|---|---|
| `oakd_proxy_id`, `depth_sample_topic`, `feature_flow_topic` | which oakd to consume |
| `camera_x_m`, `camera_y_m`, `camera_height_m`, `camera_tilt_deg` (+ = down), `camera_yaw_deg` (+ = left) | mount extrinsics |
| `camera_hfov_deg`, `camera_vfov_deg` | depth (mono) sensor field of view |
| `footprint_radius_m`, `track_width_m` | circular footprint; wheel spacing |
| `motor_control_proxy_id`, `left_channel`, `right_channel`, `wheel_speed_calibration_m_per_s` | odometry source; the calibration **must be measured** (drive a known distance at a known output) |
| `obstacle_height_min_m` (0.05) / `obstacle_height_max_m` (2.0) | floor-texture vs obstacle band; above max is ignored (lintels) |
| `max_scan_range_m` | discard far readings before classification |
| `depth_border_margin_rows` | decimated rows dropped at the top of the grid |
| `map_size_m`, `map_resolution_m` | fixed grid centred on the start pose |
| `odometry_source`, `visual_odometry_min_matches`, `mapping_backend` | as above |
| `point_cloud_enabled`, `point_voxel_size_m`, `point_cloud_max_voxels` | optional voxel cloud (LRU-capped) |

## Calibrating the camera mount (lessons from `worke`)

- **Solve tilt/vFOV from floor data with height fixed.** Measure
  `camera_height_m` with a tape; fit `camera_tilt_deg`/`camera_vfov_deg` to
  live floor readings. Letting all three float is underdetermined (floor-only
  data can't separate them). On `worke` the fit converged consistently at
  tilt ≈ 16–19° (applied 16.4°, vFOV 51.5°, height 0.8128 m) — far from the
  "~45°" eyeball estimate, which was probably measuring the bracket rather
  than the optical axis.
- **Verify with the live `max_scan_range_m`.** Calling
  `project_depth_sample_to_scan` without it falls back to 8 m and makes a
  correct fit look broken.
- A walled room should trace as straight lines; arcs bowing toward the robot
  or a false occupied band at the top of the map mean the projection or the
  border-row margin is off. Residual false positives at the extreme edge
  columns (±30°) are known and not yet root-caused.

## UI

`ui/View.tsx` draws the grid on a `<canvas>` with a bulk `putImageData`
write (grids reach 400×400), the robot marker + heading arrow from `pose`,
a status strip and a Reset map button.

## Limitations

No loop closure or drift correction; open-loop odometry unless visual flow
is on (accuracy bounded by the wheel-speed calibration and slip); circular
footprint; fixed, pre-allocated map bounds. Path planning is out of scope —
`slam` produces the map, it doesn't drive.
