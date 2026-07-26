"""RobotKinematicsService — whole-body multi-chain IK (full InMoov).

Subprocess service. Loads a rig (URDF + end-effector + servo map), then
solves all end-effectors together via Pinocchio + Pink so shared joints
(the waist) coordinate and limits hold. Each end-effector has a *pendant* —
a world-frame target point it chases (arms reach it; head looks at it).

Bus topics:
  /robot_kinematics/{id}/state      retained — rig + joint angles + EE poses + link positions
  /robot_kinematics/{id}/control    incoming actions
  /robot_kinematics/{id}/heartbeat  1Hz (base class)

Actions: list_rigs, load_rig, set_target, solve, step, set_calibration,
send_to_servos, reset.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from rlx_bus import ServiceConfig, SubprocessService, service_method
from rlx_servo_cal import auto_calibrate, math_to_servo, servo_to_math

from . import rig_lib
from .rig import EndEffector, JointCalibration, Prop, RigSpec, Target
from .solver import WholeBodySolver

logger = logging.getLogger(__name__)


class RobotKinematicsConfig(ServiceConfig):
    """Persisted working state. The rig (end_effectors/calibration/urdf) is
    hydrated from a rig file on load_rig; targets are the live pendants."""
    model_config = {"protected_namespaces": (), "extra": "allow"}

    rig_id: Optional[str] = None
    rig_title: Optional[str] = None
    urdf_path: str = ""
    visual: str = ""                    # bundle-relative GLB for the skinned viewer
    base_link: str = "world"
    posture_cost: float = 1e-2
    avoid_obstacles: bool = True        # collision-aware IK against scene obstacles (the table)
    follow_servos: bool = False         # mirror live servo angles onto the bound joints (display hardware)
    # Teleop hardware gate: when True, every solve / set_joint auto-fans the
    # solved angles out to the bound servos (drives the PHYSICAL robot). Default
    # False = on-screen model only, so a mapping can be validated before any
    # motor moves. The upstream deadman (webxr only sends while a grip is held)
    # is the per-command gate; this flag is the global arm/disarm.
    actuate_hardware: bool = False
    # Joints the IK must NOT move — pinned at their current value (e.g. a robot
    # with a FIXED torso: ["waist_pan_joint", "waist_roll_joint"]). Simplifies +
    # steadies the arm IK. Persisted, so it survives restart + rig reloads.
    locked_joints: List[str] = []
    # Soft range limits (degrees) applied ON TOP of the URDF limits to steer the
    # IK's redundant DOFs toward natural poses — e.g. bound the humeral roll
    # ({"r_upper_arm_roll_joint": [-35, 35], ...}) so a reach keeps the elbow
    # pointing DOWN instead of the QP swinging it into a contortion. Persisted.
    joint_clamps_deg: Dict[str, List[float]] = {}
    end_effectors: List[EndEffector] = []
    calibration: List[JointCalibration] = []
    targets: Dict[str, Target] = {}     # ee_name -> pendant
    props: List[Prop] = []              # scene objects (table, cup, …)
    hand_curl: Dict[str, float] = {}    # side ('r'/'l') -> 0 open .. 1 closed


class RobotKinematicsService(SubprocessService):
    type_name = "robot_kinematics"
    heartbeat_interval_s = 1.0
    config_class = RobotKinematicsConfig
    publishes = ["state"]

    def __init__(self, proxy_id, bus) -> None:
        super().__init__(proxy_id, bus)
        self._solver: Optional[WholeBodySolver] = None
        self._error: Optional[str] = None
        self._link_parents: Dict[str, str] = {}  # static per-rig bone list
        self._servo_subs: set = set()            # servo ids we've subscribed to
        self._servo_state: Dict[str, dict] = {}  # servo_id -> last /state (for range + follow)
        self._last_follow_pub: float = 0.0        # throttle state publishes in follow mode
        # Joints driven DIRECTLY (head pan/tilt/roll, wrist roll) via set_joint
        # rather than IK. Re-applied after every solve so IK posture
        # regularization can't drift them off the operator's command.
        self._held: Dict[str, float] = {}
        self._last_fanout: float = 0.0            # throttle physical servo writes
        self._last_hot_pub: float = 0.0           # coalesce per-frame teleop state publishes

    # ─── lifecycle ────────────────────────────────────────────────────
    async def on_start(self) -> None:
        if self.config.urdf_path:
            try:
                self._build_solver()
                self._apply_targets()
                if self._solver:
                    self._solver.solve(iters=150)
            except Exception as exc:  # noqa: BLE001 — surface as state, don't crash on boot
                self._error = f"solver init failed: {exc}"
                logger.exception("robot_kinematics %s: solver init failed", self.proxy_id)
        await self._ensure_servo_subs()
        await self._publish_state()

    # ─── servo feedback (Follow servos) ───────────────────────────────
    def _make_servo_handler(self, servo_id: str):
        async def _handler(payload):
            await self._on_servo_state(servo_id, payload)
        return _handler

    async def _ensure_servo_subs(self) -> None:
        """Subscribe to each bound servo's /state (the topic carries the id;
        the payload doesn't). Idempotent; safe to call whenever bindings
        change. The handler no-ops unless follow_servos is on."""
        for cal in self.config.calibration:
            sid = cal.servo_proxy_id
            if sid and sid not in self._servo_subs:
                self._servo_subs.add(sid)
                try:
                    await self.bus.subscribe(f"/servo/{sid}/state", self._make_servo_handler(sid))
                except Exception:  # noqa: BLE001
                    logger.exception("robot_kinematics %s: subscribe /servo/%s/state failed",
                                     self.proxy_id, sid)

    async def _on_servo_state(self, servo_id: str, payload) -> None:
        """Mirror a servo's live angle onto every joint bound to it
        (inverse calibration), then re-render. Throttled. Always caches
        the servo's last state (incl. min/max range) so auto-fit can read
        the real range even while Follow is off."""
        if isinstance(payload, dict):
            self._servo_state[servo_id] = payload
        if not self.config.follow_servos or self._solver is None:
            return
        if not isinstance(payload, dict):
            return
        cur = payload.get("current_angle", payload.get("angle"))
        if cur is None:
            return
        angles = {
            c.joint: servo_to_math(c, float(cur))
            for c in self.config.calibration if c.servo_proxy_id == servo_id
        }
        if not angles:
            return
        self._solver.set_joint_angles_deg(angles)
        now = time.time()
        if now - self._last_follow_pub > 0.066:   # ~15 Hz
            self._last_follow_pub = now
            await self._publish_state()

    # ─── solver wiring ────────────────────────────────────────────────
    def _build_solver(self) -> None:
        self._solver = WholeBodySolver(self.config.urdf_path)
        missing = self._solver.set_end_effectors(
            list(self.config.end_effectors), posture_cost=self.config.posture_cost,
        )
        # Pin fixed joints (e.g. a non-mobile torso) so the IK never moves them.
        if self.config.locked_joints:
            self._solver.lock_joints(list(self.config.locked_joints))
        # Soft-bound redundant DOFs (e.g. humeral roll) for natural poses.
        if self.config.joint_clamps_deg:
            self._solver.clamp_joints({k: tuple(v) for k, v in self.config.joint_clamps_deg.items()})
        self._error = f"end-effectors missing from URDF: {missing}" if missing else None
        self._link_parents = self._solver.link_parents()
        # Collision-aware IK: build obstacle barriers from bar_table props so
        # the arm avoids driving through the table when navigating to the cup.
        obstacles = []
        for p in self.config.props:
            if p.type == "bar_table":
                obstacles.append({
                    "cx": p.pose.get("x", 0.0) / 1000.0,
                    "cy": p.pose.get("y", 0.0) / 1000.0,
                    "R": (p.dims.get("top_d", 480.0) / 2.0) / 1000.0,
                    "zt": p.dims.get("height", 1050.0) / 1000.0,
                })
        ee_links = [e.link for e in self.config.end_effectors if e.mode != "look_at"]
        self._solver.set_obstacles(obstacles, ee_links, enabled=self.config.avoid_obstacles)
        for side, amt in (self.config.hand_curl or {}).items():   # restore hand pose
            self._solver.set_finger_curl(side, amt)

    def _apply_targets(self) -> None:
        if not self._solver:
            return
        for name, t in (self.config.targets or {}).items():
            try:
                self._solver.set_target(name, (t.x, t.y, t.z))
            except KeyError:
                continue

    # ─── rig library ──────────────────────────────────────────────────
    @service_method("list_rigs")
    async def m_list_rigs(self) -> Dict[str, Any]:
        return {"rigs": rig_lib.list_rigs()}

    @service_method("load_rig", publishes=["state"])
    async def m_load_rig(
        self, id: Optional[str] = None, urdf_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Load a rig by library id (resolves its URDF), or point straight at
        a URDF path (bare model, no end-effectors until set)."""
        if id:
            rig, urdf_abs = rig_lib.load(str(id))
            await self.update_config({
                "rig_id": rig.rig_id, "rig_title": rig.title, "urdf_path": urdf_abs,
                "visual": rig.visual, "base_link": rig.base_link, "posture_cost": rig.posture_cost,
                "end_effectors": [e.model_dump() for e in rig.end_effectors],
                "calibration": [c.model_dump() for c in rig.calibration],
                "props": [p.model_dump() for p in rig.props],
                "targets": {},
            })
        elif urdf_path:
            await self.update_config({"rig_id": None, "rig_title": None,
                                      "urdf_path": str(urdf_path), "targets": {}})
        else:
            raise ValueError("load_rig: pass id or urdf_path")
        self._build_solver()
        # Seed each EE's pendant at its current world position so the UI has
        # a marker to drag from.
        if self._solver:
            seeded: Dict[str, Any] = {}
            for ee in self.config.end_effectors:
                pos = self._solver.ee_world_pos(ee.name)
                if not pos:
                    continue
                if ee.mode == "look_at":
                    # A look_at target at the link's own position is degenerate
                    # (zero look direction) and pins the shared waist/neck —
                    # seed a point well in front (and a little down) instead.
                    seeded[ee.name] = {"x": pos[0] + 500.0, "y": pos[1], "z": pos[2] - 150.0}
                else:
                    seeded[ee.name] = {"x": pos[0], "y": pos[1], "z": pos[2]}
            await self.update_config({"targets": seeded})
            self._apply_targets()
        await self._ensure_servo_subs()
        await self._publish_state()
        return self._snapshot()

    # ─── targets / solving ────────────────────────────────────────────
    @service_method("set_target", publishes=["state"])
    async def m_set_target(
        self, ee: str, x: float, y: float, z: float, solve: bool = True,
        iters: int = 120, persist: bool = True,
    ) -> Dict[str, Any]:
        """Move an end-effector's pendant to (x,y,z) mm and (by default) solve.

        ``iters`` is the number of warm-started differential-IK steps. Full
        convergence (~120) is right for a one-shot manual move, but for
        continuous TELEOP a low count (~6) eases the arm a little toward the
        target each frame instead of snapping fully — snapping trips the
        collision barrier + posture regularization and makes the arm jump.

        ``persist=False`` (used by teleop) skips writing the pendant to the
        config every frame — a full backend yml round-trip per frame was a big
        latency/IO source — and coalesces the state publish. The live pendant is
        re-sent continuously, so it doesn't need to survive a restart."""
        if self._solver is None:
            raise RuntimeError("no rig loaded")
        self._solver.set_target(str(ee), (float(x), float(y), float(z)))
        self.config.targets[str(ee)] = Target(x=float(x), y=float(y), z=float(z))  # RAM
        if persist:
            await self.update_config({"targets": {k: v.model_dump() for k, v in self.config.targets.items()}})
        if solve:
            self._solver.solve(iters=max(1, int(iters)))
            self._apply_held()          # keep directly-driven joints fixed through IK
        await self._maybe_fanout()
        await self._publish_state(force=persist)
        return self._snapshot()

    @service_method("set_joint", publishes=["state"])
    async def m_set_joint(self, joint: str, angle: float) -> Dict[str, Any]:
        """Directly command one joint to ``angle`` (deg), bypassing IK — for
        head pan/tilt/roll + wrist roll driven straight from headset/controller
        orientation. Clamped to the joint's URDF limits. The joint is HELD
        (re-applied after every subsequent IK solve) so arm IK can't drift it.
        Updates the model; writes to hardware only when ``actuate_hardware``."""
        if self._solver is None:
            raise RuntimeError("no rig loaded")
        j = str(joint)
        self._held[j] = float(angle)
        self._solver.set_joint_angles_deg({j: float(angle)})
        await self._maybe_fanout()
        # Teleop drives ~5 set_joint (head pan/tilt/roll + 2 wrists) a frame;
        # coalesce their publishes with the hands' into one ~30 Hz snapshot.
        await self._publish_state(force=False)
        return {"joint": j, "angle": float(angle), "held": sorted(self._held)}

    @service_method("release_joint", publishes=["state"])
    async def m_release_joint(self, joint: str) -> Dict[str, Any]:
        """Stop holding a directly-driven joint so IK/posture governs it again."""
        self._held.pop(str(joint), None)
        await self._publish_state()
        return {"held": sorted(self._held)}

    def _apply_held(self) -> None:
        """Re-assert the directly-driven joints after an IK solve (no-op if
        none held). set_joint_angles_deg overrides only the named indices, so
        the IK-solved arm joints are preserved."""
        if self._held and self._solver is not None:
            self._solver.set_joint_angles_deg(self._held)

    async def _maybe_fanout(self) -> None:
        """Drive the physical servos with the current solved angles — only when
        actuate_hardware is armed. Lightly throttled so a fast solve loop can't
        spam servo writes. The upstream deadman already gates WHEN we get here."""
        if not self.config.actuate_hardware or self._solver is None:
            return
        now = time.time()
        if now - self._last_fanout < 0.04:        # cap ~25 Hz
            return
        self._last_fanout = now
        await self._fanout_to_servos()

    @service_method("solve", publishes=["state"])
    async def m_solve(self, iters: int = 200) -> Dict[str, Any]:
        """Solve all end-effectors to convergence from the current pose."""
        if self._solver is None:
            raise RuntimeError("no rig loaded")
        self._solver.solve(iters=int(iters))
        self._apply_held()
        await self._maybe_fanout()
        await self._publish_state()
        return self._snapshot()

    @service_method("step", publishes=["state"])
    async def m_step(self, dt: float = 0.05) -> Dict[str, Any]:
        """One differential-IK step — for smooth continuous dragging."""
        if self._solver is None:
            raise RuntimeError("no rig loaded")
        self._solver.step(float(dt))
        self._apply_held()
        await self._maybe_fanout()
        await self._publish_state()
        return self._snapshot()

    @service_method("set_obstacle_avoidance", publishes=["state"])
    async def m_set_obstacle_avoidance(self, enabled: bool) -> Dict[str, Any]:
        """Toggle collision-aware IK (avoid the table). Off = the arm chases
        the target freely (may clip the table); useful if the barrier gets
        the arm stuck in a local minimum."""
        await self.update_config({"avoid_obstacles": bool(enabled)})
        if self._solver is not None:
            self._solver._obstacle_enabled = bool(enabled)
        await self._publish_state()
        return {"avoid_obstacles": bool(enabled)}

    @service_method("set_hand", publishes=["state"])
    async def m_set_hand(self, side: str, amount: float, persist: bool = True) -> Dict[str, Any]:
        """Open/close a hand. ``side`` is 'r'/'right' or 'l'/'left'; ``amount``
        0 = open (extended), 1 = closed (curled). All phalanx joints of every
        finger curl proportionally — one control, tendon-style. ``persist=False``
        (teleop) skips the per-frame config write + coalesces the publish."""
        if self._solver is None:
            raise RuntimeError("no rig loaded")
        s = "r" if str(side).lower() in ("r", "right") else "l"
        moved = self._solver.set_finger_curl(s, float(amount))
        amt = max(0.0, min(1.0, float(amount)))
        self.config.hand_curl[s] = amt  # RAM
        if persist:
            await self.update_config({"hand_curl": dict(self.config.hand_curl)})
        await self._maybe_fanout()
        await self._publish_state(force=persist)
        return {"side": s, "amount": amt, "joints": len(moved)}

    @service_method("reset", publishes=["state"])
    async def m_reset(self) -> Dict[str, Any]:
        """Reset to the clamped neutral pose (targets unchanged)."""
        if self._solver is None:
            raise RuntimeError("no rig loaded")
        self._solver.reset()
        await self._publish_state()
        return self._snapshot()

    # ─── calibration / servos ─────────────────────────────────────────
    @service_method("set_calibration", publishes=["state"])
    async def m_set_calibration(
        self, joint: str, servo_proxy_id: Optional[str] = None,
        zero_offset_deg: Optional[float] = None, direction: Optional[int] = None,
        scale: Optional[float] = None, servo_min_deg: Optional[float] = None,
        servo_max_deg: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Update one joint's servo calibration (servo_proxy_id='-' unbinds)."""
        cals = list(self.config.calibration)
        idx = next((i for i, c in enumerate(cals) if c.joint == joint), -1)
        if idx == -1:
            cals.append(JointCalibration(joint=joint)); idx = len(cals) - 1
        cur = cals[idx].model_copy()
        if zero_offset_deg is not None: cur.zero_offset_deg = float(zero_offset_deg)
        if direction is not None: cur.direction = -1 if int(direction) < 0 else 1
        if servo_proxy_id is not None:
            cur.servo_proxy_id = None if servo_proxy_id == "-" else str(servo_proxy_id)
        if scale is not None: cur.scale = float(scale)
        if servo_min_deg is not None: cur.servo_min_deg = float(servo_min_deg)
        if servo_max_deg is not None: cur.servo_max_deg = float(servo_max_deg)
        cals[idx] = cur
        await self.update_config({"calibration": [c.model_dump() for c in cals]})
        await self._ensure_servo_subs()   # pick up any newly bound servo
        await self._publish_state()
        return self._snapshot()

    @service_method("set_follow_servos", publishes=["state"])
    async def m_set_follow_servos(self, enabled: bool) -> Dict[str, Any]:
        """Mirror live servo angles onto bound joints in the view. While on,
        the bound joints track the hardware (inverse-calibrated) rather than
        IK — drag/Reach are paused for those joints until you turn it off."""
        await self.update_config({"follow_servos": bool(enabled)})
        if enabled:
            await self._ensure_servo_subs()
        await self._publish_state()
        return {"follow_servos": bool(enabled)}

    @service_method("set_actuate_hardware", publishes=["state"])
    async def m_set_actuate_hardware(self, enabled: bool) -> Dict[str, Any]:
        """Arm/disarm physical servo output. When armed, every solve / set_joint
        / set_hand fans the solved angles out to the bound servos (drives the
        REAL robot). Default off = on-screen model only. Disarm + follow_servos
        are mutually exclusive in practice; teleop wants actuate on, follow off."""
        await self.update_config({"actuate_hardware": bool(enabled)})
        await self._publish_state()
        return {"actuate_hardware": bool(enabled)}

    @service_method("set_ee_enabled", publishes=["state"])
    async def m_set_ee_enabled(self, name: str, enabled: bool) -> Dict[str, Any]:
        """Enable/disable an end-effector's IK task + rebuild the solver task set.
        Disabling the head look_at EE lets direct set_joint head control drive
        head_pan/tilt/roll without the IK fighting it — used by teleop."""
        if self._solver is None:
            raise RuntimeError("no rig loaded")
        ees = [e.model_copy() for e in self.config.end_effectors]
        if not any(e.name == str(name) for e in ees):
            return {"error": f"no end-effector {name!r}", "end_effectors": [e.name for e in ees]}
        for e in ees:
            if e.name == str(name):
                e.enabled = bool(enabled)
        await self.update_config({"end_effectors": [e.model_dump() for e in ees]})
        self._solver.set_end_effectors(ees, posture_cost=self.config.posture_cost)
        self._apply_targets()       # re-assert remaining EE pendants
        self._apply_held()
        await self._publish_state()
        return {"name": str(name), "enabled": bool(enabled)}

    @service_method("set_locked_joints", publishes=["state"])
    async def m_set_locked_joints(self, joints: List[str]) -> Dict[str, Any]:
        """Set the joints the IK must NEVER move — pinned at their current value.
        Use for a fixed torso: ["waist_pan_joint","waist_roll_joint"]. Rebuilds
        the solver so it takes effect immediately; persists (survives restart +
        rig reloads). Pass [] to unlock all."""
        js = [str(j) for j in (joints or [])]
        await self.update_config({"locked_joints": js})
        if self._solver is not None:
            self._build_solver()      # fresh model → clears old locks, applies new
            self._apply_targets()
            self._apply_held()
        await self._publish_state()
        return {"locked_joints": js}

    @service_method("set_joint_clamps", publishes=["state"])
    async def m_set_joint_clamps(self, clamps_deg: Dict[str, List[float]]) -> Dict[str, Any]:
        """Set soft range limits (deg) that steer the IK's redundant DOFs toward
        natural poses — e.g. ``{"r_upper_arm_roll_joint":[-35,35],
        "l_upper_arm_roll_joint":[-35,35]}`` keeps a reach's elbow pointing DOWN
        instead of contorting. Applied on top of the URDF limits, rebuilds the
        solver, persists. Pass {} to clear."""
        cl = {str(k): [float(v[0]), float(v[1])] for k, v in (clamps_deg or {}).items() if v and len(v) == 2}
        await self.update_config({"joint_clamps_deg": cl})
        if self._solver is not None:
            self._build_solver()
            self._apply_targets()
            self._apply_held()
        await self._publish_state()
        return {"joint_clamps_deg": cl}

    @service_method("set_tuning", publishes=["state"])
    async def m_set_tuning(
        self, posture_cost: Optional[float] = None, hand_position_cost: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Tune IK responsiveness. ``posture_cost`` ↓ (e.g. 0.005) lets the arm
        strain toward the target instead of hugging neutral; ``hand_position_cost``
        ↑ (e.g. 2.0) makes the QP prioritize reaching over other terms. Rebuilds
        the solver + persists."""
        patch: Dict[str, Any] = {}
        if posture_cost is not None:
            patch["posture_cost"] = float(posture_cost)
        if hand_position_cost is not None:
            ees = [e.model_copy() for e in self.config.end_effectors]
            for e in ees:
                if e.name in ("right_hand", "left_hand"):
                    e.position_cost = float(hand_position_cost)
            patch["end_effectors"] = [e.model_dump() for e in ees]
        if patch:
            await self.update_config(patch)
            if self._solver is not None:
                self._build_solver()
                self._apply_targets()
                self._apply_held()
        await self._publish_state()
        return {"posture_cost": self.config.posture_cost,
                "hand_position_cost": next((e.position_cost for e in self.config.end_effectors if e.name == "right_hand"), None)}

    @service_method("link_servo", publishes=["state"])
    async def m_link_servo(
        self, joint: str, proxy_id: str, auto_fit: bool = True,
    ) -> Dict[str, Any]:
        """Bind a joint to a live servo@1.0.0 proxy and (by default)
        auto-fit the calibration so the servo's full travel maps onto the
        joint's full math range — servo centre lands on the joint's
        neutral. Without this the identity mapping (servo°==math°) hits
        the joint limit halfway through the servo's range. Pass
        ``auto_fit=False`` to bind with raw identity knobs."""
        cals = list(self.config.calibration)
        idx = next((i for i, c in enumerate(cals) if c.joint == joint), -1)
        base = cals[idx].model_copy() if idx != -1 else JointCalibration(joint=joint)
        base.servo_proxy_id = str(proxy_id)
        if auto_fit:
            base = self._auto_fit(base, joint, proxy_id)
        if idx == -1:
            cals.append(base)
        else:
            cals[idx] = base
        await self.update_config({"calibration": [c.model_dump() for c in cals]})
        await self._ensure_servo_subs()
        await self._publish_state()
        return self._snapshot()

    def _auto_fit(self, cal: JointCalibration, joint: str, proxy_id: str) -> JointCalibration:
        """Fit ``cal`` to map the joint's math range onto the bound
        servo's usable range. Joint limits come from the solver; the
        servo range from its last retained /state (min_angle/max_angle),
        defaulting to 0..180 when not yet seen. Keeps the joint's current
        direction (so a prior manual flip survives a re-link)."""
        if self._solver is None:
            return cal
        limits = self._solver.joint_limits_deg().get(joint)
        if not limits:
            return cal
        st = self._servo_state.get(proxy_id) or {}
        s_lo = float(st.get("min_angle", 0.0))
        s_hi = float(st.get("max_angle", 180.0))
        return auto_calibrate(cal, limits[0], limits[1], s_lo, s_hi, direction=cal.direction)

    @service_method("auto_calibrate", publishes=["state"])
    async def m_auto_calibrate(
        self, joint: str, direction: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Re-fit one joint's calibration to its math range ↔ servo range
        (the same fit link_servo does). ``direction`` flips the mapping;
        omitted keeps the current one. The joint must already be bound."""
        cals = list(self.config.calibration)
        idx = next((i for i, c in enumerate(cals) if c.joint == joint), -1)
        if idx == -1 or not cals[idx].servo_proxy_id:
            raise RuntimeError(f"auto_calibrate: joint {joint} is not bound to a servo")
        cur = cals[idx].model_copy()
        if direction is not None:
            cur.direction = -1 if int(direction) < 0 else 1
        cals[idx] = self._auto_fit(cur, joint, cur.servo_proxy_id)
        await self.update_config({"calibration": [c.model_dump() for c in cals]})
        await self._publish_state()
        return self._snapshot()

    @service_method("unlink_servo", publishes=["state"])
    async def m_unlink_servo(self, joint: str) -> Dict[str, Any]:
        """Unbind a joint from its servo."""
        return await self.m_set_calibration(joint=joint, servo_proxy_id="-")

    @service_method("send_to_servos")
    async def m_send_to_servos(self) -> Dict[str, Any]:
        """Fan out the current solved joint angles to every bound servo (manual
        one-shot). Live teleop uses the gated auto-fanout via actuate_hardware."""
        if self._solver is None:
            raise RuntimeError("no rig loaded")
        dispatched = await self._fanout_to_servos()
        return {"dispatched": len(dispatched), "fanout": dispatched}

    async def _fanout_to_servos(self) -> List[str]:
        """Map every bound joint's solved angle through its calibration and
        publish ``/servo/<id>/control {write, angle}``. Shared by the manual
        send_to_servos action and the gated teleop auto-fanout."""
        angles = self._solver.joint_angles_deg()
        dispatched: List[str] = []
        for cal in self.config.calibration:
            if not cal.servo_proxy_id:
                continue
            math_a = angles.get(cal.joint)
            if math_a is None:
                continue
            servo_a = math_to_servo(cal, math_a)
            await self.bus.publish(
                f"/servo/{cal.servo_proxy_id}/control",
                {"action": "write", "angle": int(round(servo_a))},
            )
            dispatched.append(f"{cal.joint}→{cal.servo_proxy_id}@{servo_a:.1f}°")
        return dispatched

    # ─── servo_controller interface (VIRTUAL controller) ──────────────
    # robot_kinematics declares ``implements: [servo_controller]`` so a
    # servo can attach to it the same way it attaches to an Arduino. The
    # servo pushes ``servo_write`` here; we map the angle to the joint(s)
    # linked to that servo (calibration.servo_proxy_id) and update the
    # model. Identified by ``servo_id`` (the sending servo) — virtual
    # controllers have no pins. We only SET joint angles + re-render; we
    # never write back to the servo, so there's no feedback loop with
    # send_to_servos.
    @service_method("servo_write", publishes=["state"])
    async def m_servo_write(
        self, angle: float, servo_id: Optional[str] = None, pin: Optional[int] = None,
    ) -> Dict[str, Any]:
        if self._solver is None or not servo_id:
            return {"applied": 0}
        angles = {
            c.joint: servo_to_math(c, float(angle))
            for c in self.config.calibration if c.servo_proxy_id == servo_id
        }
        if not angles:
            return {"applied": 0}
        self._solver.set_joint_angles_deg(angles)
        await self._publish_state()
        return {"applied": len(angles), "joints": list(angles)}

    @service_method("servo_attach")
    async def m_servo_attach(
        self, servo_id: Optional[str] = None, pin: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Virtual attach — no pin to configure. The servo→joint mapping
        comes from this rig's calibration (use ``link_servo`` to bind a
        servo to a joint). Acknowledge so the servo's attach succeeds;
        warn when the servo isn't linked to any joint yet."""
        bound = bool(servo_id) and any(
            c.servo_proxy_id == servo_id for c in self.config.calibration
        )
        if servo_id and not bound:
            logger.warning(
                "robot_kinematics %s: servo %r attached as a virtual controller "
                "but isn't linked to any joint — call link_servo(joint, %r)",
                self.proxy_id, servo_id, servo_id,
            )
        return {"ok": True, "virtual": True, "linked": bound}

    @service_method("servo_detach")
    async def m_servo_detach(
        self, servo_id: Optional[str] = None, pin: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Virtual detach — no pin to release. The calibration link is
        left intact (manage it via link_servo / unlink_servo)."""
        return {"ok": True, "virtual": True}

    # ─── state ────────────────────────────────────────────────────────
    def _snapshot(self) -> Dict[str, Any]:
        s = self._solver
        ee_status: List[Dict[str, Any]] = []
        for ee in self.config.end_effectors:
            ee_status.append({
                "name": ee.name, "link": ee.link, "mode": ee.mode, "enabled": ee.enabled,
                "pos_mm": s.ee_world_pos(ee.name) if s else None,
                "error_mm": s.reach_error_mm(ee.name) if s else None,
            })
        return {
            "rig_id": self.config.rig_id,
            "rig_title": self.config.rig_title,
            "visual": self.config.visual or None,
            "base_link": self.config.base_link,
            "loaded": s is not None,
            "error": self._error,
            "end_effectors": ee_status,
            "targets": {k: v.model_dump() for k, v in (self.config.targets or {}).items()},
            "props": [p.model_dump() for p in (self.config.props or [])],
            "joint_angles": s.joint_angles_deg() if s else {},
            "joint_limits": s.joint_limits_deg() if s else {},
            "link_positions": s.link_world_positions() if s else {},
            "link_poses": s.link_world_poses() if s else {},
            "link_parents": self._link_parents,
            "limit_violations": s.limit_violations() if s else [],
            "colliding_links": s.colliding_links() if s else [],
            "obstacle_clearance_mm": s.obstacle_clearance_mm() if s else None,
            "avoid_obstacles": self.config.avoid_obstacles,
            "locked_joints": list(self.config.locked_joints),
            "joint_clamps_deg": dict(self.config.joint_clamps_deg),
            "follow_servos": self.config.follow_servos,
            "actuate_hardware": self.config.actuate_hardware,
            "hand_curl": s.finger_curl() if s else {},
            "calibration": [c.model_dump() for c in self.config.calibration],
            "ts": time.time(),
        }

    async def _publish_state(self, force: bool = True) -> None:
        """Publish the full state snapshot (retained). ``force`` (default) always
        publishes — for config changes the UI must see immediately. The per-frame
        teleop path passes ``force=False`` so the ~7 set_target/set_joint calls a
        frame coalesce into one ~30 Hz publish instead of flooding the bus with
        redundant 26 KB snapshots (the dominant teleop latency source)."""
        if not force:
            now = time.time()
            if now - self._last_hot_pub < 1.0 / 30.0:
                return
            self._last_hot_pub = now
        await self.publish("state", self._snapshot(), retained=True)
