# botgear `slam` Service — Cheap 2D Occupancy Mapping for `worke`

Status: **implemented (Phases 0–3) — awaiting real-hardware smoke test.**
Branch B of `docs/TODO_OAKD_AWARENESS.md`'s awareness ladder. All four
phases are built and unit-tested (§8); the one thing that couldn't
happen in this session is running it against the actual `worke` +
OAK-D Lite (needs the physical robot moving, and a hand-measured
`wheel_speed_calibration_m_per_s`) — that's the concrete next step.

---

## 1. Motivation

`worke` should be able to trace the outline of open floor space in the
room it's in — "where can I go" — without turning that into a
CPU-hungry vision problem that competes with the robot's safety
watchdog. A surprisingly small amount of information, combined with
physical priors that are true of any wheeled indoor robot, gets you
most of the way there:

- Gravity exists, and the floor is (mostly) flat.
- The camera is rigidly mounted to the robot at a *fixed, known*
  position and angle — it doesn't need to be re-discovered every frame.
- `worke` has known physical dimensions.

Given those, a single depth image collapses into a 2D range scan —
structurally identical to what a 2D LIDAR would produce — via fixed
geometry, no per-frame floor detection and no neural network. That scan
accumulates over time (given a pose estimate) into a classic occupancy
grid. This is the same category of technique as ROS's
`depthimage_to_laserscan` + basic grid mapping, deliberately **not**
full visual SLAM (no dense feature tracking across frames, no loop
closure, no drift correction) — that's the whole reason it's cheap.

---

## 2. Research grounding

**Odometry reality check** (Explore agent, read `sabertooth`'s and
`motor_control`'s actual implementations in full):

- `sabertooth_service/service.py`'s `_snapshot()` publishes
  `"has_feedback": False` explicitly; the module docstring states
  outright: *"whether the controller reports position feedback
  (Sabertooth does not)."* `sabertooth_service/protocol.py` only
  encodes **outbound** packets (`drive_packet`, `stop_packet`, etc.) —
  no read-command exists anywhere in the implementation.
- `motor_control.py`'s `_snapshot()` publishes a per-channel `output`
  field, but it's the host's own slew-limited *model* of what it's
  currently commanding (ramped via `slew_rate`), not anything measured
  back from real hardware.
- The `motor_controller` capability contract itself (AGENTS.md) is
  write-only: `motor_set`/`motor_stop`/`motor_stop_all` — no read-back
  action exists in the interface.
- No existing odometry/encoder/pose/occupancy-grid code exists anywhere
  in this repo (grepped `repo/` + `packages/` for all the obvious
  terms). The only "pose"/"encoder" hits are unrelated: IK arm
  end-effector pose, a hobby servo's single-write angle estimate, STT
  audio codecs.

**Conclusion: there is no wheel encoder or any other real motion
feedback on this robot today.** Odometry for `slam` has to be pure
open-loop dead-reckoning — integrate `motor_control`'s *commanded*
output over elapsed time, scaled by a manually-measured speed constant.
Real drift should be expected. This is a named, accepted limitation
(§6), not something the design pretends away. Adding real encoder
feedback later is a valuable, explicitly out-of-v1 fast-follow.

**`oakd`'s current shape**: ships exactly two MJPEG streams (`rgb`,
colorized `depth`) — no raw numeric depth ever leaves its capture loop
today. `docs/TODO_OAKD.md` (now folded into `repo/oakd/1.0.0/README.md`)
already named this gap ("raw depth data access... needs a new low-rate
raw-sample topic") as deferred future work; §4 below is that work,
specified concretely. Note: `repo/oakd/1.0.0/README.md` now documents a
`publish_depth_samples` option, so re-check whether this gap has since
closed before treating §4 as still open.

---

## 3. Naming and ownership split

**Service name: `slam`.** The domain-standard term for exactly what
this service does — fuse localization and mapping — and the term the
robot's owner used when scoping this. Single recognizable word,
matches this codebase's naming convention (`video`, `oakd`, `chat`).
The real risk in choosing this name — implying full-SLAM guarantees
(loop closure, relocalization, drift correction) this v1 doesn't have —
is handled the same way `oakd`/`chat`/`google_chat` already handle
scope honesty: a prominent, explicit disclaimer in `package.yml`'s
description and the module docstring, not a vaguer name.

**`oakd` stays hardware-only. `slam` owns all robot-specific spatial
reasoning.** `oakd` doesn't know `worke`'s dimensions or how its own
camera is mounted on the chassis — it just produces depth data (and
could, in principle, support a different depth camera later without
`slam` changing). `slam` owns robot footprint, camera mount extrinsics,
wheel geometry, and obstacle thresholds, and consumes only a generic
small depth-sample message + a generic motor-state message. This keeps
`slam` genuinely lightweight: **no OpenCV, no depthai dependency at
all** — pure numpy + trigonometry. Almost certainly the cheapest
service in the whole repo, computationally.

---

## 4. New `oakd` addition — low-rate raw depth-sample topic

New `OakdConfig` fields (all opt-in, zero cost to existing users who
leave them off):
- `publish_depth_samples: bool = False`
- `depth_sample_rate_hz: float = 2.0`
- `depth_sample_cols: int = 32`, `depth_sample_rows: int = 24`
  (decimation grid — deliberately coarse; room-outline tracing doesn't
  need per-pixel resolution)

**Correction caught while designing `slam`'s geometry math**: raw
disparity values (what `_colorize_depth` already reads) can't be
converted to real-world range without the stereo pair's calibration
intrinsics (baseline + focal length) — `max_disparity` alone isn't
enough, and the payload as first sketched didn't carry the rest.
`StereoDepth` has a **separate** `.depth` output that's already metric
millimeters, computed on-device from those intrinsics — using that
instead sidesteps the problem entirely and needs no calibration data on
the wire. `oakd`'s pipeline currently only links `.disparity` (for the
existing colorized MJPEG preview); Phase 0 needs to link `.depth` too,
to a second `XLinkOut`, alongside it — not instead of it.

In the capture loop, throttled to the configured rate: pool/resize the
depth-in-mm array (the new `.depth` output — a resize, same cheap
operation `_colorize_depth` already does on the disparity array, just a
different source array) to the small grid, and publish it as a plain
JSON int array:

```jsonc
// oakd/<id>/depth_sample
{
  "cols": 32, "rows": 24,
  "depth_mm": [[...], ...],   // small 2D int array, already metric —
                                // no calibration data needed downstream
  "ts": 1234567.8
}
```

Matches this codebase's existing "JSON-over-WebSocket, no compiled
`.msg` step" convention (AGENTS.md's Discovery section) — no new wire
format needed.

---

## 5. `slam` service design

### 5.1 Config (`SlamConfig`)

This is where robot dimensions, camera mount geometry, and odometry
source live — the "enrich the localization" config the whole design is
organized around:

| Field | Purpose |
|---|---|
| `oakd_proxy_id`, `depth_sample_topic` | Which `oakd` instance to consume — reuses the existing federation/multi-proxy addressing, no bespoke discovery needed. |
| `camera_x_m`, `camera_y_m`, `camera_height_m` | Camera mount position relative to robot center. |
| `camera_tilt_deg`, `camera_yaw_deg` | Camera mount angle relative to robot forward/level. |
| `camera_hfov_deg`, `camera_vfov_deg` | The depth sensor's (mono cameras', not RGB's) field of view — needed to turn a `depth_sample` grid column/row index into an angle. Defaults are OAK-D Lite mono-camera approximations; override for other cameras. |
| `footprint_radius_m` | Circular footprint approximation (§6) — used for obstacle inflation. |
| `track_width_m` | Distance between drive wheels — needed to turn left/right commanded output into linear + angular velocity for a differential-drive base. |
| `motor_control_proxy_id`, `left_channel`, `right_channel` | Which `motor_control` instance/channels are the drive wheels. |
| `wheel_speed_calibration_m_per_s` | **Must be physically measured** — drive a known distance at a known commanded output, divide. The software cannot infer this without additional sensing (§6). |
| `obstacle_height_min_m`, `obstacle_height_max_m` | The band separating "floor texture" (ignorable) from "a real obstacle." |
| `map_size_m`, `map_resolution_m` | Fixed-size grid, centered on the robot's pose at startup. Default generous (e.g. 20m × 20m at 5cm/cell = 400×400 cells — trivial memory footprint). |

### 5.2 Pipeline (pure numpy — no image library needed)

1. **Odometry**: subscribe to `motor_control`'s `/state`. Convert
   left/right commanded `output` → linear + angular velocity via
   `track_width_m` and `wheel_speed_calibration_m_per_s`, then integrate
   pose deltas with standard differential-drive Euler dead-reckoning on
   every update — this produces the `pose_change` fed to the backend
   (§5.2a), not the final pose directly (a correcting backend gets the
   final say on pose; the hand-rolled v1 backend just accepts it as-is).
2. **Scan extraction**: on each `oakd` `depth_sample` message, project
   each decimated column through the fixed camera-extrinsics transform
   (§5.1) into a bearing + range in the *robot's own frame* (not world
   frame — world-frame placement is the backend's job, so the scan
   itself stays reusable across backends), classifying via the
   obstacle-height band: floor (keep looking further along that ray),
   obstacle (record range, stop), or nothing-in-range (open up to max
   range).
3. **Backend update**: hand the robot-frame scan + the odometry
   `pose_change` to the pluggable mapping backend (§5.2a) each cycle.
   It returns the current best pose estimate and owns the occupancy
   grid.
4. **Publish**: the occupancy grid (retained, low rate — on-change or
   ~1Hz) on `slam/<id>/map`, and the current pose estimate (small
   payload, can run faster — ~5-10Hz) on `slam/<id>/pose`.

### 5.2a Pluggable mapping backend — build hand-rolled first, keep BreezySLAM swappable

Reuse-vs-build was weighed explicitly (not a hasty call) — recorded here
so the reasoning survives, not just the conclusion:

**Spiked `BreezySLAM`** ([simondlevy/BreezySLAM](https://github.com/simondlevy/BreezySLAM))
before deciding anything: not on PyPI (source-only install via
`pip install git+https://github.com/simondlevy/BreezySLAM.git#subdirectory=python`),
but actively maintained (commit history into Feb 2026) and it builds
cleanly from source on Python 3.12 with no compiler friction — confirmed
by actually building and running it, not assumed. Its
`RMHC_SLAM.update(scans_mm, pose_change=...)` / `.getpos()` / `.getmap()`
API is a close match for the pipeline shape above, and it's fast in
practice (~1ms/update against a synthetic scan). It would get real
scan-matching pose correction essentially for free — the thing the
hand-rolled design explicitly does *not* attempt (§6).

**Decided to start hand-rolled anyway**, because the honest cons outweigh
the "free" win for a v1 that hasn't touched real hardware yet:
- Source-only install is a materially bigger ask of botgear's
  install-wizard flow (which builds per-type venvs *on the robot's own
  hardware*) than a normal pinned PyPI dependency — a new compiler-
  toolchain requirement + GitHub-availability dependency at install time.
- **Unverified on ARM.** Only built/tested on an x86 dev laptop so far.
  `video`'s own `pyproject.toml` explicitly targets Raspberry Pi 4/5
  (aarch64) as a real platform for this codebase — a from-source C
  extension is meaningfully more likely to hit friction there than a
  package shipping prebuilt wheels, and that hasn't been tested.
- Its accuracy is unvalidated **on our kind of input** — built and
  tuned against real 2D LIDAR (dense, clean, ~360 points), not a sparse
  (32-column), noisier depth-camera-derived scan. Tuning it well for our
  data might cost real effort of its own, eating into the "saved" work.
- The genuinely differentiated, `worke`-specific work (camera-geometry
  projection, odometry fusion, calibration) has to be built regardless
  of which backend does grid accumulation — so the backend choice only
  affects one bounded piece of the system, not the whole design.

**So**: a small `MappingBackend` interface sits between the pipeline
(steps 1-2 above) and grid accumulation, so a `BreezySlamBackend` can be
added later as a pure swap-in — once the ARM/install-wizard question has
a real answer — without touching the geometry/odometry code at all.

```python
class MappingBackend(ABC):
    @abstractmethod
    def update(
        self, scan_ranges_mm: List[Optional[float]], scan_bearings_deg: List[float],
        pose_change: Tuple[float, float, float],   # (dx_mm, dy_mm, dtheta_deg), robot frame
    ) -> Tuple[float, float, float]:
        """One cycle: robot-frame scan + odometry delta in. Returns the
        backend's current best (x_mm, y_mm, theta_deg) in the map frame —
        dead-reckoning-only for HandRolledBackend, scan-matching-corrected
        for a future BreezySlamBackend."""

    @abstractmethod
    def occupancy_grid(self) -> np.ndarray:
        """Current grid, normalized to a common encoding at the
        SlamService layer regardless of backend-internal representation."""
```

v1 ships exactly one implementation, `HandRolledBackend`: `update()`
integrates `pose_change` directly (no correction — the honest v1
limitation, §6) and ray-casts the scan into a log-odds grid (old §5.2
steps 3-4, unchanged, now living inside this backend instead of loosely
in the service). `SlamConfig.mapping_backend: str = "hand_rolled"`
selects the backend, matching this codebase's existing enum-config-field
+ small-registry pattern (`video`/`oakd`'s `filters/__init__.py`).

### 5.3 UI

New pattern for this codebase — every service UI so far (`video`,
`oakd`, and the messaging services) centers on an `<img>` against an
MJPEG endpoint. `slam` instead needs a live top-down 2D render: a
`<canvas>` drawing the occupancy grid as cells plus a robot icon with a
heading arrow at the current pose estimate. Same modular-bundle
mechanism as every other service UI, just a different core visual
primitive.

---

## 6. Explicitly named limitations

Stated plainly, matching the honesty precedent every doc this session
has followed (`chat`'s inert `channel` param, `google_chat`'s
send-only banner, `oakd`'s USB3/IMU caveats):

- **No loop closure or drift correction.** This is the entire "cheap"
  in "cheap SLAM." The map will drift on long or complex traversals.
  This is the core tradeoff the whole approach accepts, not an
  oversight to fix later within this design.
- **Odometry is open-loop.** No wheel encoders exist on `worke` today
  (§2). Accuracy is bounded by how well `wheel_speed_calibration_m_per_s`
  is tuned and will degrade with wheel slip. **§9 supersedes the
  originally-sketched "real encoder feedback" fast-follow** with a
  better fix already available on this robot's existing sensor: optical
  flow off the OAK-D's on-device feature tracker. Not yet implemented.
- **Circular footprint approximation**, not `worke`'s true outline —
  simpler inflation math, a conscious simplification, not an oversight.
- **Fixed-size, pre-allocated map.** No dynamic growth or
  relocalization if the robot travels beyond the configured bounds.

---

## 7. Explicitly out of scope for v1

- Branch A (on-device object/person detection) — independent work,
  tracked in `docs/TODO_OAKD_AWARENESS.md`, not a prerequisite for this.
- Real encoder-based odometry — **superseded**, see §9: this robot has
  no encoders and none are planned; optical-flow visual odometry off
  the already-mounted OAK-D is the designed replacement instead.
- Loop closure / SLAM-proper drift correction (§6) — a `BreezySlamBackend`
  (§5.2a) is the documented path to partial drift mitigation later, once
  its from-source install is verified on ARM target hardware.
- Path planning / autonomous navigation — `slam` produces a map +
  inflated obstacle grid a planner could consume later; it doesn't plan
  or drive anything itself.
- Rectangular (vs. circular) footprint modeling.
- Dynamic/growing map bounds.

---

## 8. Phased implementation plan

- [x] **Phase 0 — `oakd` addition**
  - [x] `depth_sample` topic per §4 (using `.depth`, metric mm — the
        disparity→calibration gap caught while writing §5.2's geometry
        math, corrected before any code was written against it),
        opt-in config, throttled publish in the existing capture loop.
  - [x] Validated against the real `depthai` SDK (not just syntax):
        pipeline builds cleanly with the new `.depth` XLinkOut linked
        alongside the existing `.disparity` one, both with and without
        `publish_depth_samples` enabled; the resize/decimation logic
        verified against a synthetic depth array with a planted
        "obstacle" region, correct values at the correct decimated
        cells. Real end-to-end hardware confirmation (comparing decimated
        samples against the live device) is still open — needs the
        actual OAK-D Lite plugged in and a running backend.

- [x] **Phase 1 — core algorithm layer** (`repo/slam/1.0.0/`, no bus
      wiring yet — pure functions + classes, unit-testable without
      hardware or a running backend):
  - [x] `SlamConfig` per §5.1.
  - [x] Differential-drive dead-reckoning odometry integration (§5.2
        step 1).
  - [x] Camera-extrinsics depth-sample → robot-frame bearing/range
        projection (§5.2 step 2).
  - [x] `MappingBackend` interface + `HandRolledBackend` (log-odds grid,
        ray-casting, inflation) per §5.2a.
  - [x] Unit tests against synthetic scans/odometry (no hardware
        needed — mirrors how `oakd`'s pipeline math was validated
        against synthetic frames before real hardware was available).

- [x] **Phase 2 — bus wiring**
  - [x] `SlamService(SubprocessService)` wiring Phase 1's pipeline to
        `motor_control`/`oakd` bus subscriptions (topics built directly
        from `motor_control_proxy_id`/`oakd_proxy_id` — no bespoke
        discovery needed) + `slam/<id>/map` + `slam/<id>/pose`
        publishes. Odometry ticks and depth samples are decoupled: each
        `motor_control` tick composes a small local delta into a
        pending accumulator (reusing `apply_pose_delta`, same function
        Phase 1's tests already cover), flushed into the backend on
        each depth sample.
  - [x] `motor_control`'s actual `/state` wire shape verified by
        reading `motor_control.py`'s `_snapshot()` directly (a LIST of
        per-channel dicts keyed by `id`, not the dict-keyed-by-name
        shape first assumed) before writing the parser against it.
  - [x] `/map` is quantized to 1 byte/cell + base64'd (not the raw
        float32 grid) — keeps the retained message small at the
        cost of losing precision the UI/consumers don't need anyway.
  - [x] `reset_map` control action, for recovering from "the robot was
        picked up and moved" (which dead-reckoning has no way to detect
        on its own).
  - [x] Unit tests (`_FakeBus` seam, no real websocket) covering
        subscription wiring, the odometry-accumulator no-op-until-flush
        behavior, malformed-input handling, the map wire shape, and
        `reset_map`.
  - [ ] Manual smoke test against the real OAK-D Lite + `worke`: push
        it around a real room by hand first (motors off, isolating
        mapping from odometry-drift for this first check), confirm the
        traced outline roughly matches the room; then a driven test.
        **Still open** — needs the physical robot + a
        `wheel_speed_calibration_m_per_s` measurement, neither
        available from this session alone.
  - [x] **Rotation-direction bug, found live on real hardware and fixed
        in three separate spots** — worth recording together since each
        one looked like the same symptom (the map/robot marker turning
        the "wrong way") but had a different, unrelated root cause:
        1. `project_depth_sample_to_scan`'s column→bearing mapping
           (`geometry.py`) had column 0 (the depth image's left edge)
           mapped to the wrong sign of `h_angle`, so obstacle cells
           swept the wrong way around the robot **in the robot's own
           frame** as it rotated — confirmed by comparing against the
           already-correct pose heading arrow.
        2. The UI's heading-arrow *line* (`View.tsx`) needed a
           mirror-compensating sign flip because the canvas draws with
           +row = downward while the backend's `_world_to_cell` mapped
           world +y to +row with no flip — confirmed against real
           hardware by watching `theta_deg` trend 316→311 while
           physically rotating right (i.e. decreasing theta_deg is a
           real clockwise turn, the correct standard-math-convention
           behavior) and observing the arrow itself still rendering the
           turn as if it were left.
        3. **The actual map cells were still wrong even after both of
           the above** — (1) and (2) fixed the local scan-bearing
           convention and the arrow's own rotation, but the occupancy
           grid itself is generated server-side by `hand_rolled.py`
           from world-frame (x, y), using that same unflipped
           `origin_cell + y_m/resolution` row mapping (2) had to work
           around rather than correct. Root-caused and fixed at the
           source instead of patching around it again: `_world_to_cell`
           now maps world +y to a **smaller** row (`origin_cell -
           y_m/resolution`), making the canvas a normal right-handed
           frame (x=right, y=up) with no mirror anywhere. The UI's `py`
           was updated to match, and the arrow line's existing
           `cos`/`-sin` formula — written as a mirror-compensation hack
           for (2) — turned out to already be the mathematically
           correct formula for this now-consistent convention, so it
           needed no further change, just a comment correction. No
           existing test exercised the y/row axis (all of
           `test_slam_backend.py`'s scans use `bearing_deg=0.0`, i.e.
           the x/col axis only), so this shipped with zero test
           coverage of the very axis that was broken — a gap worth
           closing before calling the smoke test done.
  - [x] **Straight walls rendering as arcs bowed toward the robot,
        found live on real hardware and fixed in `geometry.py`'s
        `project_depth_sample_to_scan`.** Root cause, independent of
        all three rotation-direction fixes above: `depth_mm` is
        Z-depth (perpendicular distance to the image plane — the
        standard depth-camera convention, same as RealSense/Kinect/
        OAK-D), not the Euclidean range along each ray. A flat wall at
        a constant true distance reads the SAME z-depth in every
        column, but an off-center column's ray travels a longer
        diagonal path to reach that same wall — its TRUE range is
        `z_depth / cos(h_angle)`, larger than z-depth away from the
        center column. The function was using raw z-depth directly as
        the scan's range with no correction, which plots a straight
        wall as a circular arc (`x=z*cos(h), y=z*sin(h)` traces a
        circle of radius z, not the line the wall actually is) bowed
        toward the robot at the edges — exactly the reported symptom.
        Fixed by dividing z-depth by `cos(h_angle)` before it's used as
        range (benefits the height-band classification too, for free,
        since it reads the same corrected `range_m`). The vertical
        counterpart (`cos(v_angle)`) is deliberately still not
        corrected — unlike the horizontal case it doesn't distort the
        2D map's shape, only the height threshold's sensitivity at
        extreme pitch, matching the "cheap, not precise" design goal.
        Guarded against a misconfigured `camera_hfov_deg` pushing
        `h_angle` to ±90° (division blowup) by treating that column as
        "nothing seen" rather than raising. New test
        (`test_flat_wall_z_depth_corrected_to_true_range_off_center`)
        constructs a synthetic flat wall (constant z-depth across
        columns) and asserts off-center range grows by exactly
        `1/cos(h_angle)` — would have failed against the pre-fix code,
        which returned the same (uncorrected) range at every column.
  - [x] **False "occupied" band at the top of the map, found live on
        real hardware and fixed with a new `SlamConfig.depth_border_
        margin_rows` field (default 1).** Captured and inspected a live
        `depth_sample` frame (camera level, pointed at open floor) via a
        one-off bus-client script rather than guessing: the topmost
        decimated row was 28/32 invalid, and the 4 survivors read
        25-220mm — physically implausible next to the 300-2900mm real
        floor/wall readings one row below. Depth-camera stereo matching
        commonly can't produce a valid result right at the frame edge;
        the surprise was that this camera's invalid top-edge pixels
        aren't all clean zero (which `depth<=0` already filters
        harmlessly), so a few slipped through as spurious very-close
        readings and got misread as an obstacle right next to the
        robot. The right/left edges have the same invalid-border
        behavior but read clean zeros there in this capture, so they
        were already excluded for free — only the top needed a fix.
        `project_depth_sample_to_scan` now skips
        `depth_border_margin_rows` rows at the top of every
        `depth_sample` grid before any classification, clamped so a
        misconfigured margin can't skip the whole grid. Bottom rows
        weren't observed to have this problem (and are useful — the
        closest floor in front of the robot), so this is deliberately
        top-only, not a symmetric margin. Two new tests: the default
        margin suppresses a synthetic garbage top-row reading while
        still finding a real obstacle in an unaffected row, and
        `depth_border_margin_rows=0` proves the garbage reading would
        have registered without the fix.
  - [x] **`max_scan_range_m` (new `SlamConfig` field) + a much smaller,
        finer-grained default map — worked out from live testing on
        `worke`, not assumed.** Two separate gaps, found together:
        1. Scan-range trust was a hardcoded module constant in
           `service.py` (`_MAX_SCAN_RANGE_M = 8.0`) — **8.0 meters**
           (26ft), not feet. Confirmed live: at this mount (45deg tilt,
           32in height), depth quality degrades well before the
           sensor's nominal range — readings past roughly 8**ft**
           (~2.5m) trend toward reporting "floor" regardless of what's
           actually there. Promoted to a real `SlamConfig` field
           (`max_scan_range_m`, default 2.5) so it's tunable per-mount
           instead of buried in code.
        2. `map_size_m`/`map_resolution_m` defaults (20m / 5cm-per-cell)
           were sized for a sensor range this mount doesn't reach —
           most of a 20m-wide grid sits permanently unknown, at a
           resolution too coarse to usefully distinguish nearby
           objects a few inches apart. Real usage is close-in (most
           common 0-5ft, occasionally out to ~15ft) — new defaults
           (`map_size_m=6.0`, `map_resolution_m=0.02`) trade unused far
           range for meaningfully finer near-field detail at roughly
           the same total cell count (300x300 vs 400x400). Changing
           either field rebuilds the backend (`_ensure_backend`),
           discarding the accumulated map — an accepted, pre-existing
           tradeoff for a config change (see that method's docstring),
           not new behavior.
  - [x] **"free" cells were nearly indistinguishable from "unknown"
        ones in the UI** — `85` (free/scanned-clear) was slate-900
        `[15,23,42]`, next to `unknown`'s `[30,30,36]`: both read as
        "almost black" at a glance, burying the one thing an operator
        most wants to see immediately — "this floor has actually been
        scanned and is clear" — inside a wall of same-looking dark
        cells. Changed to a clearly-distinct teal (`[13,71,74]`); the
        legend label now says "free — seen, unobstructed" instead of
        the vaguer "free (scanned, clear)".
  - [x] **Map render orientation was "forward is screen-right," not
        "forward is up."** `hand_rolled.py`'s `_world_to_cell` mapped
        world +x (the robot's own forward direction) to +col (screen-
        right) — confirmed live, real hardware: the underlying theta/
        bearing math was internally consistent (a fresh scan/pose
        cross-check showed obstacles landing exactly where they should
        given that convention), but every operator reading "top of map
        = forward" — the same convention every other top-down robot/
        game map uses — read the map as 90 degrees wrong. Rotated the
        whole convention 90deg CCW (world +x -> smaller row/"up", world
        +y -> smaller col/"left") in both `_world_to_cell` and the
        matching frontend math (`slam/1.0.0/ui/View.tsx`'s px/py, the
        heading-arrow trig, and the landmark/bounding-box corner
        conversion) — a ROTATION of the earlier mirror fix, not a
        reflection (determinant +1, verified, so it does not reintroduce
        that earlier bug). New tests cover both axes
        (`test_forward_bearing_obstacle_maps_to_smaller_row_not_larger`,
        `test_left_bearing_obstacle_maps_to_smaller_col_not_larger`).
  - [x] **Real geometric bug found chasing "the box is centered in RGB/
        depth but doesn't show up in the SLAM map at all"**: height-
        above-floor used `pixel_pitch = camera_tilt + v_angle`, which
        is only the ray's true elevation from world-horizontal when
        `h_angle == 0` (dead center column). Tilting the camera and
        looking off-axis horizontally at the same time is a compound 3D
        rotation, not two independent angles that add — confirmed live:
        genuine floor at off-center columns was computing
        height_above_floor around -0.5 to -1.6m instead of ~0, badly
        enough that a real object (centered in the camera's own view)
        never passed the obstacle-height check while implausible edge-
        noise pixels near h_angle=0 still passed it by coincidence.
        Fixed by building the camera-local ray as a proper unit vector
        (azimuth h_angle, elevation v_angle) and rotating it by
        camera_tilt before reading off both the true range (now
        divided by the combined off-axis cosine, not just
        `cos(h_angle)`) and the true elevation — see
        `project_depth_sample_to_scan`'s docstring for the full
        derivation. New regression test
        (`test_floor_reads_as_floor_at_nonzero_h_angle_and_tilt_together`)
        derives a synthetic floor point from first principles
        (independently of the implementation) and confirms it reads
        back near 0, while the old shortcut does not on the same input.

        **Important, live-hardware-confirmed caveat**: this fix is real
        and worth keeping (up to ~0.3m of provable error at extreme
        corners at this camera's FOV), but it is NOT the full
        explanation for the live symptom's full magnitude — reproducing
        the actual observed camera_tilt/hfov/vfov/column combinations
        analytically only accounts for a fraction of the -0.5 to -1.6m
        error actually seen live. After shipping this fix, a live
        re-test showed real improvement (the detected obstacle moved
        from clearly off to one side of the scan cone to straddling the
        center, matching the box's true position) but not a fully clean
        single solid detection — two smaller fragments with a gap
        between them, not one contiguous blob. The most likely remaining
        contributors, not yet investigated: `camera_height_m`/
        `camera_tilt_deg` are hand-measured ("~32 inches", "~45
        degrees"), not precision-instrumented, and this design's
        genuine sensor-noise/multipath behavior at steep grazing angles
        (see §6's already-accepted "cheap, not precise" limitations) —
        neither ruled in nor out here, left as an open thread rather
        than a closed one.
  - [x] **`oakd`'s depth-decimation step was fabricating close-range
        noise, not just passing through sensor noise** — root cause of
        a chunk of the "edge noise" this whole session had been
        filtering around. `_publish_depth_sample` downsized the native
        depth array with a plain `cv2.resize(..., INTER_AREA)`, which
        has no concept of "0 means invalid" — an area-average blends
        invalid source pixels into the mean as if they were real
        near-zero depth readings. A decimated cell that's mostly
        invalid (common near frame borders) gets pulled toward 0,
        fabricating a plausible-looking "very close object" from what's
        actually mostly missing data — confirmed with a synthetic case
        (20% invalid pixels dragged a true 2000mm reading down to a
        reported 1600mm with the old code) and live (the previously-
        noisy top border, e.g., is now clean zeros with no fabricated
        readings scattered through it). Fixed with
        `_decimate_depth_masked`: two plain `INTER_AREA` resizes (one
        of the depth values, one of a 0/1 validity mask) recover the
        true average among only the valid source pixels; a cell backed
        by too little valid coverage (<30%, `_MIN_VALID_FRACTION`)
        reports invalid rather than a low-confidence average from a
        couple of stray readings. No pytest coverage — `oakd_service`
        needs `cv2`, not installed in the main app venv, no existing
        test infra for this module — validated instead with a
        standalone synthetic check plus a live before/after capture,
        matching how this file's other real-hardware-dependent oakd
        work has been verified all along.

        **Confirmed this fixes the noise half of the problem, and
        cleanly isolates what's left**: with the fabricated edge noise
        gone, the box STILL doesn't cross the obstacle-height
        threshold — but comparing the box's column against a clean
        floor column at the same rows shows the box consistently
        reading 0.19-0.51m HIGHER than the floor, exactly like a real
        raised object should (the RELATIVE signal is correct). Both
        columns share a large COMMON negative offset (-0.4 to -1.0m,
        growing with row) that isn't explained by the compound-rotation
        fix above (it doesn't vary much with column/h_angle, which the
        compound-rotation error does) — pointing at `camera_tilt_deg`/
        `camera_height_m`/`camera_vfov_deg` being measurably off, not
        merely "roughly" imprecise. Not yet solved for — a clean next
        step, not investigated this session.
  - [x] **Solved for the actual `camera_tilt_deg`/`camera_vfov_deg`
        from live floor data, `camera_height_m` held fixed** (a tape
        measure is far more reliable than eyeballing a tilt angle, so
        it anchors the fit rather than floating free). A first attempt
        letting all three params float was UNDERDETERMINED — floor-only
        data can't uniquely separate height/tilt/vfov, and the search
        drifted to an implausible answer (tilt=14deg) despite a
        deceptively good residual; a second attempt anchoring height
        converged CONSISTENTLY to tilt~16-19deg across several
        different point selections (narrow/wide column ranges, floor-
        only vs floor+known-box-height, close-range-only) — that
        consistency, not any single run, is what makes this
        trustworthy, not a fitting fluke.

        **Applying it live at first LOOKED like a regression** (31/32
        columns misclassified as obstacle, including open floor) —
        turned out to be a bug in the verification script, not the fit:
        it called `project_depth_sample_to_scan` without passing
        `max_range_m`, silently defaulting to the function's own 8.0m
        instead of the live system's actual `config.max_scan_range_m`
        (2.5m, only wired in at the service layer in `service.py`).
        Rerun with the correct max range: 14/32 columns (down from
        31/32), and the box's full 7-column width now detects as one
        consistent, contiguous obstacle (~1.17-1.19m range, tight
        spread) — versus 1/32 (the box essentially invisible) at the
        original tilt=45deg/vfov=50deg assumption. Confirmed on the
        live map render too: what had been 2-4 scattered occupied
        cells across the session became a solid ~19-cell contiguous
        blob directly ahead of the robot, matching where it's actually
        sitting. Applied to `slam-1` live via config_patch:
        `camera_tilt_deg=16.4`, `camera_vfov_deg=51.5`
        (`camera_height_m` unchanged at 0.8128).

        A couple of residual false positives remain at the far left/
        right edge columns (bearings near +/-30deg) — not yet
        root-caused; worth a look if they turn out to matter in
        practice, but they're a small fraction of the frame and don't
        block normal operation the way the box being invisible did.

        Physical note for whoever remounts or re-measures this camera:
        the fitted tilt (~16-19deg) is dramatically shallower than the
        "~45 degrees" this session started with as a rough visual
        estimate — worth an actual protractor/level check against the
        real mount next time someone's at the robot, since a
        45-degree-vs-17-degree gap this large usually means the
        original estimate was measuring the wrong reference (e.g. the
        bracket's angle rather than the camera's actual optical axis),
        not that the camera drifted.

- [x] **Phase 3 — UI**
  - [x] Canvas-based live map view per §5.3: `putImageData` bulk pixel
        write (not per-cell `fillRect` — matters at up to 400×400
        cells), robot marker + heading arrow from `/pose`, connection/
        backend status strip, `reset_map` button. Typechecks and builds
        clean via the existing `build-service-ui.mjs` pipeline.

- [ ] **Phase 4 (future, not scheduled)** — optical-flow visual
      odometry (§9, design below — supersedes the originally-sketched
      real-encoder plan), loop closure, path planning integration,
      rectangular footprint, dynamic map bounds.

---

## 9. Phase 4 design addendum — optical-flow visual odometry

Status: **designed, not implemented.** Written up before code so the
approach gets reviewed before any of it is built — same discipline as
§2's odometry reality check before Phase 1 was written.

### 9.1 Why this instead of the originally-sketched "real encoders"

§6/§7 originally named real wheel encoders as the fast-follow for
open-loop dead-reckoning's drift. Encoders would still only measure
*wheel* rotation — blind to wheel slip, which is exactly the failure
mode dead-reckoning already has. Tracking real points in the
environment observes actual motion directly, sidestepping slip
entirely, and needs no new hardware: the OAK-D Lite already mounted on
`worke`'s pan/tilt head can do this in the same on-device pipeline that
already produces `depth_sample`.

### 9.2 Hardware/API grounding (verified against the actual installed SDK)

Checked directly against this repo's pinned `depthai==2.32.0.0`
(`repo/oakd/1.0.0/.venv`, same environment `oakd_service` actually
runs in — not just the PyPI changelog):

- `dai.node.FeatureTracker` exists, with an `inputImage` input and an
  `outputFeatures` output of type `dai.TrackedFeatures`.
- `TrackedFeatures.trackedFeatures` is a list of `TrackedFeature`,
  each carrying `.id` (a **persistent track id** — DepthAI does
  frame-to-frame correspondence matching on-device; this design does
  NOT need to re-implement point matching), `.position` (a
  `dai.Point2f` with `.x`/`.y` in pixel coordinates), `.age`,
  `.harrisScore`, `.trackingError`.
- The node runs on the Myriad X VPU (`FeatureTracker.Properties`
  exposes `numShaves`/`numMemorySlices` — on-device resource
  allocation knobs), not the host CPU — consistent with this whole
  service's "cheap" philosophy (§1). The host-side work this design
  adds (matching by id — free, DepthAI already did it — plus a
  closed-form rigid-motion fit over a few dozen points, §9.5) is
  microseconds, not a vision workload competing with the safety
  watchdog.
- Both `oakd` and `slam` already depend on `numpy` (`pyproject.toml`,
  both services) — the rigid-motion fit needs nothing beyond that.

### 9.3 Where FeatureTracker attaches in the existing pipeline

Attach `FeatureTracker` to `mono_left` (`CAM_B`), **not** the RGB
camera. Reasoning: `stereo.depth`/`stereo.disparity` are already built
from `mono_left`/`mono_right` (`_build_pipeline`, existing code) with
no `setDepthAlign()` call anywhere in `oakd_service` today — so depth
is already defined in the mono-left rectified frame. Tracking features
on that same frame means a tracked pixel's depth is a direct lookup
into the existing `.depth` output with no alignment step to add. Using
RGB instead would require aligning depth to the RGB frame first (a
real DepthAI feature, but an extra pipeline node and an extra thing to
get wrong for zero benefit here).

### 9.4 New data path

**`oakd` side** (`repo/oakd/1.0.0/src/oakd_service/service.py`):

- New config field `publish_feature_flow: bool = False` — opt-in,
  same precedent as `publish_depth_samples` (§4's original design):
  zero cost to consumers that don't need it, and it's a second
  independent flag so a deployment can run depth samples without
  feature flow or vice versa.
- New `XLinkOut` for `FeatureTracker.outputFeatures`, gated behind
  `publish_feature_flow` as `_q_features`, built alongside the
  existing `_q_depth_mm` gating in `_build_pipeline`/`_open_device`.
- Fetch camera intrinsics **once**, at device-open time, via
  `device.readCalibration().getCameraIntrinsics(dai.CameraBoardSocket.CAM_B, ...)`
  — this is new (`oakd_service` doesn't call `readCalibration()`
  today). Publish `fx/fy/cx/cy` on the existing retained `/state`
  topic (semi-static device info, same bucket as `mxid`/`usb_speed`)
  rather than repeating it on every flow sample.
- New retained-false topic `/oakd/<id>/feature_flow_sample`, published
  at a throttled rate (mirrors `depth_sample_rate_hz`'s
  `_last_depth_sample_publish` pattern — reuse the same throttle
  idiom, a new `feature_flow_rate_hz: float = 5.0` config field).
  Payload, keeping `oakd` a dumb generic sensor publisher (it has no
  idea what "the robot" or "SLAM" is — same separation of concerns as
  `depth_sample`):
  ```json
  {
    "ts": 1735689600.123,
    "matches": [
      {"id": 17, "prev_uv": [412.0, 201.5], "curr_uv": [409.0, 198.0],
       "prev_depth_mm": 1832.0, "curr_depth_mm": 1811.0},
      ...
    ]
  }
  ```
  `oakd` keeps the previous frame's `{id: (uv, depth_mm)}` map
  in-process and emits only ids present in both frames — the "did
  these ids survive frame to frame" filtering is mechanical
  bookkeeping, not SLAM-specific, so it belongs in `oakd`. The rigid-
  motion math from these correspondences to a robot-frame pose delta
  is SLAM-specific and belongs in `slam` (§9.5), matching the existing
  split where `oakd` publishes raw metric depth and `slam` (not
  `oakd`) does the depth→scan geometry (§5.2 step 2).

**`slam` side** (`repo/slam/1.0.0/src/slam_service/`):

- New config field `odometry_source: Literal["motor_control", "visual_flow"] = "motor_control"`
  — defaults to today's behavior, so this is an opt-in migration, not
  a breaking change to existing deployments/tests.
- New pure function in `geometry.py`, next to `differential_drive_delta`:
  `estimate_visual_pose_delta(matches, intrinsics, camera_height_m, camera_tilt_deg, dt) -> Optional[PoseDelta]`
  (§9.5) — returns `None` on insufficient/degenerate input so the
  caller can fall back (§9.6). Pure function, unit-testable against
  synthetic point sets with zero hardware, same precedent as every
  other function in this module (§8 Phase 1).
- `SlamService` subscribes to `/oakd/<oakd_proxy_id>/feature_flow_sample`
  when `odometry_source == "visual_flow"`, instead of (not in addition
  to, for v1 — see §9.8 for the stretch fused mode) `motor_control`'s
  `/state`. The handler composes the returned `PoseDelta` into
  `self._pending_dx_m/dy_m/dtheta_deg` — **the exact same accumulator
  `_on_motor_state` already writes into today.** This is the key scope-
  limiter: the mapping backend, the accumulator, `apply_pose_delta`,
  `_publish_pose`/`_publish_map` don't change at all. Only the
  *source* feeding the accumulator changes.

### 9.5 Algorithm: matched 3D points → a pose delta

For each `feature_flow_sample` with intrinsics `(fx, fy, cx, cy)`:

1. Back-project every match's `(prev_uv, prev_depth_mm)` and
   `(curr_uv, curr_depth_mm)` into two 3D point sets `P` (previous) and
   `Q` (current), in the mono-left camera's own frame, via the
   standard pinhole inverse: `x = (u - cx) * depth / fx`,
   `y = (v - cy) * depth / fy`, `z = depth`.
2. Find the rigid transform `(R, t)` minimizing `Σ‖R·Pᵢ + t − Qᵢ‖²` —
   the Kabsch algorithm (SVD of the cross-covariance matrix of the two
   centered point sets). Closed-form, a handful of numpy calls, no
   iterative solver.
3. Robustness: fit inside a RANSAC loop (sample 3 correspondences,
   fit, count inliers by reprojection residual, repeat) before
   accepting `(R, t)` — some matches will be on moving objects, people
   walking past, or just bad tracks, and Kabsch on a plain least-
   squares fit has no rejection ability on its own. Reject the whole
   sample (return `None`) if the best RANSAC inlier count is below a
   configurable floor (new `slam` config `visual_odometry_min_matches`,
   default TBD from real data during Phase 4c below — start around 8).
4. `(R, t)` is camera-frame ego-motion. Because the camera is rigidly
   mounted at the already-known, already-used `camera_height_m`/
   `camera_tilt_deg` (§5.2's existing fixed extrinsics — reused, not
   new), convert into the robot's planar frame: take `t`'s
   forward/lateral components (rotated by `camera_tilt_deg`) for
   `dx_m`/`dy_m`, and `R`'s yaw component for `dtheta_deg`. Roll/pitch/
   vertical noise in the fit is expected sensor noise, not
   accumulated — this design only ever writes to the same 2D
   `PoseDelta` (§5.2) the dead-reckoning path already produces.

### 9.6 Hard precondition: pan/tilt held static during SLAM

The camera→robot extrinsic (`camera_height_m`/`camera_tilt_deg`) is a
**fixed constant** in §5.2's math today, and stays a fixed constant
here — this design adds no per-frame pan/tilt-angle compensation.
Operating convention (confirmed as the intended usage, not a
limitation being papered over): **hold the pan/tilt at a fixed neutral
position — near the robot's physical middle — whenever `slam` is
mapping**, the same way `reset_map` already exists for "the robot was
picked up and moved" as an explicit operator action rather than
something the system tries to detect automatically. If pan/tilt motion
during active mapping is ever wanted, that's a distinct future
extension (feed the commanded servo angle into the camera→robot
transform per-frame) — explicitly out of scope here.

### 9.7 Fallback behavior (not a fused mode — see §9.9 for that stretch goal)

When `estimate_visual_pose_delta` returns `None` (too few RANSAC
inliers — blank walls, darkness, camera pointed at a featureless
ceiling), `slam` falls back to the existing `motor_control`-derived
dead-reckoning **for that tick only** — requires also subscribing to
`motor_control` even in `visual_flow` mode, just as a standby source.
This directly answers the "what about low-texture environments"
question a visual-only design would otherwise dodge; it degrades to
exactly today's behavior rather than freezing the pose estimate.

### 9.8 Testing plan

Same discipline as Phase 1 (§8): `estimate_visual_pose_delta` is a
pure function, tested first against synthetic data — construct a known
`(R, t)`, transform a synthetic point set by it, feed both point sets
through the function, assert the recovered delta matches the applied
one within tolerance, including a synthetic outlier-contaminated case
to exercise the RANSAC rejection path. No hardware needed for this
layer, matching how the existing dead-reckoning geometry was validated
before real-hardware access existed.

### 9.9 Phased implementation plan

- [x] **Phase 4a — `oakd`**: `FeatureTracker` node on `mono_left`,
      `publish_feature_flow`/`feature_flow_rate_hz` config + throttled
      `feature_flow_sample` topic, intrinsics fetched via
      `readCalibration()` and added to `/state`. `depth_mm` XLinkOut is
      now shared between `publish_depth_samples` and
      `publish_feature_flow` (previously only the former) — the capture
      loop fetches it at most once per tick and hands the same packet
      to whichever consumer(s) are due, instead of each flag calling
      `tryGet()` independently and silently splitting the packet stream.
      Pipeline build validated (no device needed) across all four
      flag combinations — node counts confirm `depth_mm`'s XLinkOut is
      built exactly once even when both flags are on. Match/depth-
      lookup logic in `_publish_feature_flow` unit-tested against
      synthetic `TrackedFeature`-shaped objects: confirms no matches on
      the very first tick (nothing to compare against), a moved point
      correctly reported once seen on two consecutive ticks, a
      zero-depth point correctly excluded even though tracked on both
      ticks, and a dropped/newly-appeared id correctly absent.
      **Confirmed against the real OAK-D Lite on `worke`, same session:**
      `TrackedFeatures` messages arrive and the corner detector finds
      real features (57–73 tracked, climbing as more of the room comes
      into view) — `FeatureTracker`'s on-device tracking + the bus
      plumbing both work end-to-end. Live testing also caught one real
      bug fixed before landing: `feature.position` is in `mono_left`'s
      native 640×480 pixel space, but `stereo.depth`'s actual output
      resolution is **320×240** on this hardware (confirmed by array
      size, not assumed) — naively indexing `depth_mm[v, u]` without
      scaling would have silently discarded most features as
      "out of bounds" instead of correctly locating them. Fixed by
      scaling into `depth_mm`'s actual shape before indexing (verified
      against a synthetic 320×240 case); the published `matches` stay
      in the original unscaled mono-space uv, consistent with the
      intrinsics already fetched at 640×480. Separately observed live
      (not a bug — same behavior confirmed on the pre-existing,
      untouched `depth_sample` path too): `stereo.depth` was reporting
      **all-zero depth** for the room/scene in front of the camera at
      the time of testing — 0 matches published as a direct, correct
      consequence of the prev/curr depth-validity filter (§9.4) doing
      its job, not a matching-logic failure. Real matches with nonzero
      depth are the one thing this session couldn't produce (needs the
      camera actually pointed at something in-range with texture) —
      that's now folded into Phase 4c's real-hardware smoke test below
      rather than being a separate open item.
- [x] **Phase 4b — `slam`**: `estimate_visual_pose_delta` +
      `_kabsch`/`_ransac_kabsch`/`_camera_to_robot_rotation` added to
      `geometry.py`; `odometry_source`/`visual_odometry_min_matches`/
      `feature_flow_topic` added to `SlamConfig`; `SlamService` wired
      per §9.4 — `_on_feature_flow` drives the SAME pending-delta
      accumulator `_on_motor_state` already fed, `_on_motor_state` now
      only integrates directly in `"motor_control"` mode (in
      `"visual_flow"` mode it just caches the commanded speed as the
      §9.7 fallback's input), `_on_oakd_state` caches intrinsics from
      oakd's retained `/state`. 30 new tests (19 pure-math in
      `test_slam_geometry.py`, 11 wiring in `test_slam_service.py`),
      970 total app tests pass, no regressions.

      **The synthetic tests earned their keep — caught a real bug
      before it ever ran against hardware.** First pass had every
      recovered translation and turn direction exactly *negated* on
      independently-reasoned pure-motion tests (pure forward, pure
      lateral, pure yaw at zero mount tilt — the one case checkable by
      direct physical reasoning, not just self-consistency). Root
      cause: `_kabsch` fits `(R, t)` such that
      `q = R·p + t` — a *point-coordinate* map from the old tick's
      camera frame to the new tick's — which is the mathematical
      **inverse** of the camera's own actual rotation/displacement
      `(ΔR, D)` (standard extrinsics: `q = ΔR.T·(p − D)`, so
      `R = ΔR.T`, `D = −ΔR·t`). The first implementation fed `R`/`t`
      directly into the camera-to-robot conversion instead of
      recovering `ΔR`/`D` first — every axis and the turn direction
      came out flipped. A second, smaller (~5%, not a sign flip) error
      surfaced only in the *combined* rotation+translation test: the
      test's own synthetic-data encoder used `ΔR_cam` where it needed
      `R_cam` (`=ΔR_cam.T`) in the translation formula — visible only
      at nonzero rotation since `R ≈ R.T` for small angles, exactly
      why the isolated pure-translation-only test didn't catch it.
      Both are fixed and covered by the current test suite.

      **What's independently verified vs. self-consistency-only:**
      the zero-tilt/zero-yaw case (pure forward, pure lateral, pure
      yaw) is checked against physical reasoning worked out
      independently of the implementation — this is also `worke`'s
      actual near-term operating convention (§9.6: pan/tilt held near
      neutral). Nonzero tilt/yaw is currently only round-trip-verified
      (encode robot motion → synthetic camera correspondences → decode
      — internally consistent, catches composition/transpose bugs, but
      can't catch a *globally* flipped tilt/yaw sign convention on its
      own, as noted in `_camera_to_robot_rotation`'s docstring). Real-
      hardware confirmation of the absolute sign at nonzero mounting
      angles is Phase 4c's job, not assumed here.

      **Not yet done — deliberately left for Phase 4c**: this was
      developed and verified entirely against synthetic data; it
      hasn't run against a live `feature_flow_sample` stream at all.
      The last real-hardware check (Phase 4a, same session) found
      `stereo.depth` reporting all-zero depth for whatever was in front
      of the camera at the time — environmental (camera
      pointing/lighting), not a code issue, but it means an actual
      live `odometry_source: visual_flow` run still needs real depth
      to test against, which didn't materialize this session.
- [ ] **Phase 4c — real-hardware smoke test**: push `worke` by hand
      over a measured path with pan/tilt held at the documented neutral
      position (§9.6), motors off — isolates visual-odometry accuracy
      from any dead-reckoning question entirely, same isolation
      strategy Phase 2's still-open manual smoke test already
      specified. Compare the recovered trajectory against a tape-
      measured ground truth, then repeat with real driving. This is
      also where `visual_odometry_min_matches` gets tuned from real
      numbers instead of a guess.
- [ ] **Phase 4d (stretch, not required for the above)** — fused mode:
      weight visual vs. commanded deltas by RANSAC inlier confidence
      instead of a hard fallback switch.

### 9.10 Open questions (deliberately unresolved until Phase 4a/4b land)

- Real RANSAC inlier-count/threshold tuning needs actual `worke`
  hardware data — the "start around 8" in §9.5 is a placeholder, not a
  researched constant.
- `feature_flow_rate_hz` default (sketched at 5.0 in §9.4) trades
  odometry smoothness against host-side fit cost and bus traffic —
  needs the same kind of real-data tuning pass `wheel_speed_calibration_m_per_s`
  already went through for the dead-reckoning path.
- Whether `LUCAS_KANADE_OPTICAL_FLOW` or `HW_MOTION_ESTIMATION` (both
  confirmed-real values of `dai.RawFeatureTrackerConfig.MotionEstimator.Type`,
  set via `FeatureTracker.initialConfig.setMotionEstimator(...)`) gives
  better matches on `worke`'s actual mono camera feed is an empirical
  question, not decided here — expose it as a config knob rather than
  hardcoding one.
