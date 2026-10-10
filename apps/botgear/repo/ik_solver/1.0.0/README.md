# ik_solver — inverse kinematics service

Subprocess service that turns a target `(x, y, z)` end-effector pose into
joint angles for a servo-driven arm. Deterministic, stateless, and
hardware-free: it doesn't drive servos or generate PWM, it computes angles
and (optionally) publishes them to linked `servo@1.0.0` proxies.

```
type:      ik_solver
version:   1.0.0
language:  python (subprocess — numpy/scipy dependency footprint)
```

## Bus actions (`/ik_solver/<id>/control`)

```
solve(target={x,y,z})                          → joint angles (math + servo), publishes /solution
fk(joint_angles={...})                         → (x, y, z, roll, pitch, yaw)
set_model(joints=[...], links=[...])           → replaces robot model, persists, republishes /state
set_calibration(joint, zero_offset_deg, direction, servo_proxy_id?)
link_servo(joint, proxy_id) / unlink_servo(joint)
send_to_servos(joint_angles=?)                 → defaults to last solution; publishes write to each linked servo
auto_calibrate(...)
list_models / load_model(id) / save_model(id, title?, include_current_pose?)
delete_model(id) / export_model(id)            → Model Library (below)
save_pose(name, set_initial?) / apply_pose(name) / delete_pose(name)
servo_attach / servo_write / servo_detach      → virtual servo_controller (repo/servo/1.0.0/README.md)
```

Topics: `/state` (retained model + last solution), `/solution` (non-retained,
one event per `solve()`, success or failure), `/heartbeat`, `/meta`.

## Model Library

Robot models are portable JSON files (`schema_version: 1`, schema in
[`models_lib.py`](src/ik_solver_service/models_lib.py)):

```jsonc
{
  "schema_version": 1,
  "id": "inmoov_right_arm",
  "title": "InMoov — Right Arm",
  "source": "inmoov_ros (URDF) -> ros.convert",
  "units": { "length": "mm", "angle": "deg" },
  "ik_model": { "joints": [/*JointSpec*/], "links": [/*LinkSpec*/] },  // what the solver runs
  "chain":    [ /* per-joint origin/axis/limits */ ],                  // for a future 3-D solver
  "calibration_template": [ /* JointCalibration without servo_proxy_id */ ],
  "poses": [ { "name": "home", "is_initial": true, "angles": {"base": 0} } ]
}
```

Two roots, merged like brain's bundled + workspace workflows — a user model
with the same id shadows the bundled one:

| Root | Location | Writable |
|---|---|---|
| Bundled examples | `src/ik_solver_service/examples/*.json` (InMoov left + right arm) | no — ships in the package, found via `__file__` |
| User models | `<data_dir>/ik_solver/models/*.json` | yes — shared by every ik_solver instance, on the persistent data volume |

The subprocess can't resolve the relative `BOTGEAR_DATA_DIR` from its own
cwd, so the process manager passes an absolute `BOTGEAR_DATA_DIR_ABS` to
every subprocess service.

**Working state vs library.** The config holds the live working model
(joints, links, calibration, `model_id`/`title`/`source`, `chain`, `poses`)
and persists across restarts; library files are snapshots. `load_model`
copies a file into the config, merging per-rig servo bindings back in by
joint name, and applies the initial pose; `save_model` writes the config
to the user root. The live pose is tracked from `solve`, `fk` and
`apply_pose`; `save_pose` captures it, and one pose per model is flagged
initial.

`ros.convert` (`repo/ros/1.0.0`, `urdf_export.py`) emits this schema, so a
URDF import drops straight into the library.

## Coordinate system

Right-handed, +Z up, origin at the robot's mounting point. Internally mm +
radians; degrees + millimetres at the API surface.

## Calibration

`servo_angle = (math_angle * direction * scale) + zero_offset_deg` (see
[`calibration.py`](src/ik_solver_service/calibration.py)). An unlinked joint
is treated as `direction=1, scale=1, offset=0` (identity). The servo proxy
applies its own `min_angle`/`max_angle` clamp on top — two layers protect
the hardware.

## Failure reasons (`ik.py`)

`solve`/`fk` never raise for a bad target — they return
`{"reachable": false, "reason": "...", "detail": "..."}`. Current reason
vocabulary: `unreachable_workspace`, `joint_limit_violation`,
`solver_did_not_converge`.

## UI

`ui/View.tsx` — dual side (XZ) / top (XY) SVG projections, target x/y/z
inputs, joint table with link/unlink, Solve + Send-to-servos buttons. The
Model Library panel lists bundled examples and your models, and offers
load / save / save-as / capture pose / download.
