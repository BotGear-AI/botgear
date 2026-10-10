# servo — protocol-agnostic servo positioner

Stores which controller(s) drive it, the pin on each, the current angle,
limits and speed — and publishes standardized commands to each
controller's control topic. It knows nothing about Arduino, Firmata or
PCA9685; the controller translates commands into hardware writes (or,
for a virtual controller, into a joint angle in a 3-D model).

```
type:     servo
version:  1.0.0
requires: [servo_controller]
```

## The `servo_controller` interface

Any service that declares `implements: [servo_controller]` in its
`package.yml` must accept these on `/{type}/{id}/control`:

```
{"action": "servo_attach", "pin": int?, "servo_id": str}
{"action": "servo_write",  "pin": int?, "angle": int, "servo_id": str}   # 0..180
{"action": "servo_detach", "pin": int?, "servo_id": str}
```

Every message carries `servo_id` (the sending servo's proxy id). `pin` is
only for **real** controllers.

| Kind | Implementations | How a write lands |
|---|---|---|
| Real | `arduino` (reference) | `pin` → PWM output |
| Virtual | `robot_kinematics`, `ik_solver` | no pin — `servo_id` is looked up in the rig's calibration (`calibration[].servo_proxy_id`, set by `link_servo`) and the angle is mapped to that joint via `rlx_servo_cal.servo_to_math` |

The attach dropdown lists every running service that implements the
interface, so virtual controllers appear next to Arduinos.

## Several controllers per servo

`config.controllers` is a list of bindings
(`{controller_type, controller_id, pin?}`) and the source of truth for
fan-out: one `write` sends `servo_write` to **every** binding. So a servo
can drive a physical joint on `arduino-1` and the matching joint of the 3-D
model on `robot_kinematics-1` at the same time — or drive only the model
today and gain a real Arduino later without re-linking.

```
attach(controller_type="arduino", controller_id="arduino-1", pin=9)       # add a real binding
attach(controller_type="robot_kinematics", controller_id="robot_kinematics-1")  # add a virtual one
attach()                                                                   # resume all bindings
detach(controller_type=..., controller_id=...)                             # drop one
```

Re-attaching the same controller updates its pin. The legacy single
`controller_type` / `controller_id` / `pin` fields are folded into
`controllers` on load and kept mirrored to `controllers[0]` for older
readers.

**No feedback loop:** a virtual controller only sets the joint and
re-renders — it never writes back to the servo. Driving servos from the
model (`robot_kinematics.send_to_servos`, joint → servo `write`) is a
separate, one-directional path.

## Actions

`attach`, `detach`, `write`, `set_limits`, `set_speed`,
`set_speed_control_enabled`, `stop`, `sweep`, `stop_sweep` — signatures
and the `state` / `motion_events` wire shapes are introspected into
`/runtime/runtime/types/servo`.

Speed control has its own switch: `speed_control_enabled` decides whether
writes interpolate, and `speed_deg_per_s` is always a real rate (≥ 1).
Old configs that used `speed_deg_per_s = 0` to mean "snap" are migrated.
