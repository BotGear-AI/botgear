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
```

Topics: `/state` (retained model + last solution), `/solution` (non-retained,
one event per `solve()`, success or failure), `/heartbeat`, `/meta`.

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
inputs, joint table with link/unlink, Solve + Send-to-servos buttons.
