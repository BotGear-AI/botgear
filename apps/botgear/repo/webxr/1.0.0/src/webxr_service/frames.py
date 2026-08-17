"""Coordinate-frame conversion: WebXR → botgear.

WebXR poses are **X-right, Y-up, −Z-forward, metres, right-handed** in
the session's reference space. botgear / the URDF world frame is the
ROS convention **X-forward, Y-left, Z-up, millimetres, right-handed**
(verified against the InMoov rig: l/r shoulders sit at ±Y, the hands
hang slightly +X of the torso, the head is −X). The mapping is the fixed
rotation that carries the operator's body axes onto the robot's:

    x_rob = -z_xr        (operator forward −Z  → robot forward +X)
    y_rob = -x_xr        (operator right   +X  → robot right   −Y)
    z_rob =  y_xr        (operator up      +Y  → robot up      +Z)
                         (then × 1000 for mm)

Orientation is the same basis change applied as a quaternion
pre-multiply. Recentering is a translation offset captured at
"set origin" time; ``origin_yaw_deg`` then rotates the horizontal plane
so the operator's heading maps to robot-forward regardless of facing.

NB (historical bug): this used to be a bare +90°-about-X
(``x_rob=x_xr, y_rob=-z_xr``), which produced X-right/Y-forward — a 90°
yaw off the URDF frame. Head tracking hid it (head uses a *separate*
euler decode of the raw quat), but hand IK targets came out sideways:
extending a hand forward drove the robot hand left. Fixed to the full
WebXR→ROS rotation below.

Pure-Python (quaternions as ``[x, y, z, w]``) so the service needs no
numpy — the math is tiny and runs per input frame at ~60 Hz.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

# WebXR (X-right, Y-up, −Z-fwd) → URDF/ROS (X-fwd, Y-left, Z-up). [x, y, z, w].
# Proper rotation (det +1); verified: −Z→+X, +X→−Y, +Y→+Z.
Q_XR_TO_ROBOT = (0.5, -0.5, -0.5, 0.5)


def quat_mul(a: Sequence[float], b: Sequence[float]) -> List[float]:
    """Hamilton product a·b, quaternions as [x, y, z, w]."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    ]


def quat_rotate(q: Sequence[float], v: Sequence[float]) -> List[float]:
    """Rotate vector ``v`` by quaternion ``q`` ([x,y,z,w])."""
    qx, qy, qz, qw = q
    vx, vy, vz = v
    # t = 2 * cross(q.xyz, v)
    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)
    # v' = v + qw * t + cross(q.xyz, t)
    return [
        vx + qw * tx + (qy * tz - qz * ty),
        vy + qw * ty + (qz * tx - qx * tz),
        vz + qw * tz + (qx * ty - qy * tx),
    ]


def point_to_robot_mm(p_xr_m: Sequence[float]) -> List[float]:
    """XR position (metres, Y-up) → robot position (mm, Z-up)."""
    r = quat_rotate(Q_XR_TO_ROBOT, p_xr_m)
    return [r[0] * 1000.0, r[1] * 1000.0, r[2] * 1000.0]


def quat_to_robot(q_xr: Sequence[float]) -> List[float]:
    """XR orientation → robot orientation (same basis change)."""
    return quat_mul(Q_XR_TO_ROBOT, q_xr)


def _cross(a: Sequence[float], b: Sequence[float]) -> List[float]:
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def head_euler_deg(q_xr: Sequence[float]) -> Dict[str, float]:
    """RAW WebXR orientation (Y-up quat ``[x,y,z,w]``) → head joint angles in
    DEGREES, named for the InMoov joints they drive:

      pan  — yaw about Y   → head_pan   (look left/right)
      tilt — pitch about X → head_tilt  (look up/down)
      roll — twist about the view axis → head_roll / wrist roll

    Computed from the forward (−Z) and up (+Y) vectors, so it is gimbal-robust
    and exactly ZERO at the neutral forward-looking pose (the recenter aligns
    position; orientation is taken relative to looking straight ahead). Decode
    in the WebXR frame, NOT the robot frame — the Y-up→Z-up basis change adds a
    spurious constant roll if you decompose the converted quat. Per-joint
    scale (±1)/offset/limits in the mapping handle sign + range."""
    f = quat_rotate(q_xr, (0.0, 0.0, -1.0))   # forward
    u = quat_rotate(q_xr, (0.0, 1.0, 0.0))    # up
    deg = 180.0 / math.pi
    pan = math.atan2(f[0], -f[2]) * deg
    tilt = math.asin(max(-1.0, min(1.0, f[1]))) * deg
    right = _cross(f, (0.0, 1.0, 0.0))
    if _dot(right, right) < 1e-9:             # looking straight up/down → roll undefined
        roll = 0.0
    else:
        n = math.sqrt(_dot(right, right))
        right = [right[0] / n, right[1] / n, right[2] / n]
        true_up = _cross(right, f)
        roll = math.atan2(_dot(u, right), _dot(u, true_up)) * deg
    return {"pan": round(pan, 3), "tilt": round(tilt, 3), "roll": round(roll, 3)}


def transform_pose(
    pose: Dict[str, Sequence[float]],
    origin_mm: Optional[Sequence[float]] = None,
    origin_yaw_deg: Optional[float] = None,
) -> Dict[str, List[float]]:
    """Convert one ``{position:[x,y,z]_m, orientation:[x,y,z,w]}`` XR pose
    into robot frame ``{pos:[x,y,z]_mm, quat:[x,y,z,w]}``. Subtracts
    ``origin_mm`` (a recenter offset already in robot mm) from the position,
    then — if ``origin_yaw_deg`` is set — rotates the position about Z by
    −origin_yaw so the operator's heading at recenter maps to robot forward
    (+X). Without this, teleop is skewed unless the operator faces the robot."""
    pos = point_to_robot_mm(pose.get("position", (0.0, 0.0, 0.0)))
    if origin_mm is not None:
        pos = [pos[i] - origin_mm[i] for i in range(3)]
    if origin_yaw_deg:
        th = math.radians(origin_yaw_deg)
        c, s = math.cos(th), math.sin(th)
        # rotate (x,y) by −th about Z: [x·c + y·s, −x·s + y·c]
        pos = [pos[0] * c + pos[1] * s, -pos[0] * s + pos[1] * c, pos[2]]
    quat = quat_to_robot(pose.get("orientation", (0.0, 0.0, 0.0, 1.0)))
    return {"pos": [round(c, 2) for c in pos], "quat": [round(c, 5) for c in quat]}
