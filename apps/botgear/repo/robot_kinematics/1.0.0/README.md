# robot_kinematics — whole-body multi-chain IK

Solves a whole robot at once. `ik_solver` handles one planar chain; a
humanoid like InMoov is a **tree** — torso → shared waist → both arms, and
torso → neck → head → eyes — with several end-effectors, shared joints and
mixed goals (hands reach, head looks at). This service coordinates all of
them in one solve and fans the result out to servos.

```
type:       robot_kinematics
version:    1.0.0
language:   python (subprocess; pinocchio + pin-pink + quadprog in its own venv, no ROS)
implements: [ik_provider, servo_controller, scene_source]
requires:   [servo_controller]
```

## Engine

**Pinocchio + Pink** (`solver.py`): Pinocchio loads the URDF and computes
kinematics/Jacobians; Pink solves weighted **tasks** simultaneously as a QP
— one `FrameTask` per enabled end-effector plus a `PostureTask` — so the
shared waist serves both arms and joint limits hold. (ikpy was rejected: it
solves chains independently and can't coordinate the waist.)

Two InMoov realities are handled at load:
- **reversed limits** — some joints store `lower > upper`; they're swapped;
- **invalid neutral pose** — the start configuration is clamped into limits.

On top of that: collision-aware IK against scene obstacles (barriers built
from the rig's table props; `set_obstacle_avoidance`), joint locking and
per-joint clamp bands, and finger-curl presets (fingers aren't IK).

## Rigs

A rig binds one flat URDF to botgear (`rig.py`):

```jsonc
{
  "rig_id": "inmoov_wholebody",
  "title": "InMoov — Whole Body",
  "urdf": "inmoov.urdf",           // beside the rig, or an absolute path
  "base_link": "world",
  "end_effectors": [
    { "name": "right_hand", "link": "r_hand_link", "mode": "position", "position_cost": 1.0 },
    { "name": "left_hand",  "link": "l_hand_link", "mode": "position", "position_cost": 1.0 },
    { "name": "head",       "link": "head_link",   "mode": "look_at",  "orientation_cost": 0.5 }
  ],
  "posture_cost": 0.01,
  "calibration": [ /* rlx_servo_cal.JointCalibration per actuated joint */ ]
}
```

End-effector modes: `position` (reach a point — right for InMoov's
< 6-DOF arms), `pose` (full 6-DOF), `look_at` (aim the link's
`forward_axis` at the point — head/eye tracking). Pointing the head's
look-at and a hand's reach at the same target gives "look at what you're
grabbing". Arms with fewer than 6 DOF can't hit arbitrary orientations, so
weighting is position-primary.

Same two-root layout as the ik_solver Model Library (`rig_lib.py`):
bundled rigs in `src/robot_kinematics_service/examples/`
(`inmoov_wholebody.json` + `inmoov.urdf`, visual `assets/inmoov.glb`) and
user rigs under the shared data dir.

## Bus actions (`/robot_kinematics/<id>/control`)

| Group | Actions |
|---|---|
| Rig | `list_rigs`, `load_rig`, `reset` |
| Targets / solve | `set_target(ee, x, y, z)`, `solve(iters?)` (to convergence), `step(dt?)` (one differential step — smooth dragging), `set_ee_enabled`, `set_hand` (finger curl) |
| Joints | `set_joint`, `release_joint`, `set_locked_joints`, `set_joint_clamps`, `set_tuning`, `set_obstacle_avoidance` |
| Servos | `set_calibration`, `link_servo`, `unlink_servo`, `auto_calibrate`, `send_to_servos`, `set_follow_servos`, `set_actuate_hardware` |
| Virtual `servo_controller` | `servo_attach`, `servo_write`, `servo_detach` — see `repo/servo/1.0.0/README.md` |

State (retained) carries joint angles, per-link world positions/poses (what
the UI, the WebXR `scene` panel and `calibrate_teleop` render from), EE
poses and targets, per-EE reach error, and limit flags.

## Servos

Per-joint `JointCalibration` (shared `packages/rlx_servo_cal`) maps solver
angles to servo angles. `send_to_servos` publishes `write` to each linked
servo proxy (joint → servo); `follow_servos` mirrors live servo angles back
onto the model (servo → joint), and servos attached to this service as a
virtual controller update their joints via `servo_write`.

## UI

`ui/View.tsx` — react-three-fiber whole-robot view (skinned InMoov GLB, Z-up),
a draggable target gizmo per end-effector (the "pendant", like an RViz
interactive marker), mode toggles and servo controls.

## Out of scope

Self-collision between arms/torso (Pinocchio collision) and a dedicated
eye-gaze chain (a second `look_at` EE) are not built.
