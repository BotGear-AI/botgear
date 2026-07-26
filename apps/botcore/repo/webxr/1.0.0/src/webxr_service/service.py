"""WebXRService — WebXR (Quest 3) teleop bridge.

Subprocess service. The immersive client (served as this service's
``xr.js`` bundle, opened in the headset browser) reads head + controller
poses each XRFrame and publishes them to ``/webxr/<id>/input``. This
service:

  * frame-converts (WebXR Y-up/m → robot Z-up/mm) + applies the recenter
    origin,
  * republishes normalized topics — ``/webxr/<id>/head``,
    ``/webxr/<id>/controller/{left,right}`` — for any actuator to consume,
  * runs the configured mapping table to drive actuators directly (e.g.
    right-controller ray → robot_kinematics ``set_target``),
  * owns the feed-panel / layout / mapping config and publishes it
    retained on ``/webxr/<id>/panels`` for the immersive client,
  * publishes ``/webxr/<id>/state`` for the desktop control panel.

Bus topics:
  /webxr/<id>/input       incoming — headset telemetry (head/controllers)
  /webxr/<id>/head        retained-ish — normalized head pose (robot frame)
  /webxr/<id>/controller/left|right   normalized controller pose+buttons+axes
  /webxr/<id>/panels      retained — feed config the client renders
  /webxr/<id>/state       retained — session status + summary (desktop UI)
  /webxr/<id>/control     incoming — actions (recenter, set_panel, …)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from rlx_bus import SubprocessService, service_method

from . import frames
from .model import Mapping, PanelSpec, WebXRConfig

logger = logging.getLogger(__name__)


class WebXRService(SubprocessService):
    type_name = "webxr"
    heartbeat_interval_s = 1.0
    config_class = WebXRConfig
    publishes = ["state", "head", "controller/left", "controller/right", "panels"]

    def __init__(self, proxy_id, bus) -> None:
        super().__init__(proxy_id, bus)
        self._session: Dict[str, Any] = {"active": False, "mode": None, "fps": 0.0}
        self._last_input_ts: float = 0.0
        self._last_state_pub: float = 0.0
        self._last_map_run: float = 0.0
        # Latest normalized signals (for the mapping resolver + state summary).
        self._norm: Dict[str, Any] = {}
        # Per-mapping smoothing state (mapping id → last emitted value/vector).
        self._ema_state: Dict[str, Any] = {}
        # Cached state of actuator targets (topic → latest payload) for
        # calibration, populated by a persistent one-per-topic subscription.
        self._state_subs: set = set()
        self._state_cache: Dict[str, Any] = {}

    # ─── lifecycle ────────────────────────────────────────────────────
    async def on_start(self) -> None:
        # Headset client publishes telemetry here; we consume it.
        await self.subscribe("input", self._on_input)
        await self._publish_panels()
        await self._publish_state()

    # ─── inbound telemetry ────────────────────────────────────────────
    async def _on_input(self, payload: Any) -> None:
        """Consume one headset telemetry frame, normalize, republish, map."""
        if not isinstance(payload, dict) or not self.config.enabled:
            return
        now = time.time()
        self._last_input_ts = now
        sess = payload.get("session")
        if isinstance(sess, dict):
            self._session = {
                "active": bool(sess.get("active", True)),
                "mode": sess.get("mode"),
                "fps": float(sess.get("fps", 0.0)),
            }

        origin = self.config.origin_mm
        yaw = self.config.origin_yaw_deg
        norm: Dict[str, Any] = {}

        head = payload.get("head")
        if isinstance(head, dict):
            # Raw robot-frame values (pre origin/yaw) — recenter captures these.
            raw_pos = frames.point_to_robot_mm(head.get("position", (0.0, 0.0, 0.0)))
            raw_euler = frames.head_euler_deg(head.get("orientation", (0.0, 0.0, 0.0, 1.0)))
            norm["_head_raw"] = {"pos": raw_pos, "yaw": raw_euler["pan"]}
            h = frames.transform_pose(head, origin, yaw)
            # Head euler drives head pan/tilt/roll directly. Make pan
            # heading-relative: subtract the recenter yaw so the head is neutral
            # (0) when looking the way you faced at recenter.
            h["euler"] = dict(raw_euler)
            if yaw:
                h["euler"]["pan"] = round(raw_euler["pan"] - yaw, 3)
            norm["head"] = h
            await self.publish("head", {**h, "ts": now})

        controllers = payload.get("controllers") or {}
        norm["controller"] = {}
        for side in ("left", "right"):
            c = controllers.get(side) if isinstance(controllers, dict) else None
            if not isinstance(c, dict):
                continue
            entry: Dict[str, Any] = {
                "buttons": c.get("buttons", {}) or {},
                "axes": c.get("axes", {}) or {},
                # Every gamepad component the device exposes (trigger,
                # squeeze, thumbstick, a/b/x/y, thumbrest, …), each with
                # {state, value, x, y} — so any actuator can map any input.
                "components": c.get("components", {}) or {},
            }
            for space in ("grip", "ray"):
                pose = c.get(space)
                if isinstance(pose, dict):
                    entry[space] = frames.transform_pose(pose, origin, yaw)
            # Derived euler (deg) from the RAW grip (palm) orientation, falling
            # back to ray — drives wrist-roll (the one wrist DOF InMoov has).
            raw = c.get("grip") if isinstance(c.get("grip"), dict) else c.get("ray")
            if isinstance(raw, dict) and "orientation" in raw:
                entry["euler"] = frames.head_euler_deg(raw["orientation"])
            norm["controller"][side] = entry
            await self.publish(f"controller/{side}", {**entry, "ts": now})

        self._norm = norm

        # Drive mapped actuators at ~60 Hz — the robot's IK solve is ~2.5 ms and
        # its state publishes are now coalesced, so we can halve the input→command
        # latency vs the old 30 Hz gate without flooding the bus.
        if now - self._last_map_run > 1.0 / 60.0:
            self._last_map_run = now
            await self._run_mappings(norm)

        # Telemetry to the desktop/HUD panel — ~30 Hz (was 10, felt laggy).
        if now - self._last_state_pub > 1.0 / 30.0:
            await self._publish_state()

    async def _run_mappings(self, norm: Dict[str, Any]) -> None:
        for m in self.config.mappings:
            if not m.enabled or not m.target:
                continue
            # Deadman / clutch: only fire while the enable signal is held.
            # Skipping leaves the actuator at its last commanded value (freeze
            # on release) and intentionally does NOT advance the smoothing
            # state, so re-engaging resumes from the held pose.
            if m.enable_source:
                ev = _as_scalar(_resolve_path(norm, m.enable_source))
                if ev is None or ev < m.enable_min:
                    continue
            val = _resolve_path(norm, m.source)
            if val is None:
                continue
            payload: Dict[str, Any] = dict(m.args)
            if isinstance(val, dict) and "pos" in val:           # pose source
                pos = val["pos"]
                # Per-axis affine: reach-scale about the shoulder, then anchor
                # offset, then a workspace clamp box so out-of-reach targets
                # don't drive IK into a thrash.
                p = [pos[i] * _axis(m.scale3, i, 1.0) + _axis(m.offset3, i, 0.0) for i in range(3)]
                for i in range(3):
                    lo = _axis(m.clamp_min, i, None)
                    hi = _axis(m.clamp_max, i, None)
                    if lo is not None and p[i] < lo:
                        p[i] = lo
                    if hi is not None and p[i] > hi:
                        p[i] = hi
                if m.ema > 0.0:
                    p = self._smooth(m.id, p, m.ema)
                payload.setdefault("x", round(p[0], 2))
                payload.setdefault("y", round(p[1], 2))
                payload.setdefault("z", round(p[2], 2))
            else:                                                # scalar source
                v = _as_scalar(val)
                if v is None or not m.arg_key:
                    continue
                if m.deadzone > 0.0 and abs(v) < m.deadzone:
                    v = 0.0
                v = v * m.scale + m.offset
                if m.ema > 0.0:
                    v = self._smooth(m.id, v, m.ema)
                payload[m.arg_key] = round(v, 4)
            try:
                await self.bus.publish(
                    _control_topic(m.target), {"action": m.action, **payload}
                )
            except Exception:  # noqa: BLE001 — one bad mapping shouldn't kill the loop
                logger.exception("webxr %s: mapping %s publish failed", self.proxy_id, m.id)

    def _smooth(self, mid: str, new: Any, a: float):
        """Exponential smoothing keyed by mapping id. ``new`` is a float or a
        list (per-axis); ``a`` in 0..1 is the weight kept from the previous
        value (higher = smoother). Shape changes reset the filter."""
        prev = self._ema_state.get(mid)
        if isinstance(new, list):
            if not isinstance(prev, list) or len(prev) != len(new):
                out = list(new)
            else:
                out = [a * prev[i] + (1.0 - a) * new[i] for i in range(len(new))]
        else:
            out = new if not isinstance(prev, (int, float)) else a * prev + (1.0 - a) * new
        self._ema_state[mid] = out
        return out

    # ─── actions ──────────────────────────────────────────────────────
    @service_method("recenter", publishes=["state"])
    async def m_recenter(self) -> Dict[str, Any]:
        """Capture the current head POSITION + HEADING as the origin so the
        operator can stand anywhere, facing any way. Stores the absolute robot-mm
        offset + yaw (deg) in config; every subsequent pose is translated then
        rotated so 'here, facing forward' becomes the robot's origin/forward."""
        raw = self._norm.get("_head_raw") if isinstance(self._norm, dict) else None
        if not raw:
            raise RuntimeError("recenter: no head pose received yet")
        origin = [round(c, 2) for c in raw["pos"]]
        yaw = round(float(raw["yaw"]), 3)
        await self.update_config({"origin_mm": origin, "origin_yaw_deg": yaw})
        await self._publish_state()
        return {"origin_mm": origin, "origin_yaw_deg": yaw}

    @service_method("clear_origin", publishes=["state"])
    async def m_clear_origin(self) -> Dict[str, Any]:
        await self.update_config({"origin_mm": None, "origin_yaw_deg": None})
        await self._publish_state()
        return {"origin_mm": None, "origin_yaw_deg": None}

    async def _read_target_state(self, target: str, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
        """Latest /state of an actuator target, via a persistent (one-per-topic)
        cache subscription. Retained state arrives on subscribe, so first read
        blocks only until it lands."""
        topic = _control_topic(target)[: -len("/control")] + "/state"
        if topic not in self._state_subs:
            self._state_subs.add(topic)

            async def _cache(payload: Any, _t: str = topic) -> None:
                if isinstance(payload, dict):
                    self._state_cache[_t] = payload

            try:
                await self.bus.subscribe(topic, _cache)
            except Exception:  # noqa: BLE001
                self._state_subs.discard(topic)
        deadline = time.time() + timeout
        while topic not in self._state_cache and time.time() < deadline:
            await asyncio.sleep(0.05)
        return self._state_cache.get(topic)

    @service_method("calibrate_teleop", publishes=["state"])
    async def m_calibrate_teleop(
        self, target: Optional[str] = None, reach_mm: float = 560.0,
        right_shoulder_link: str = "r_shoulder_base_link",
        left_shoulder_link: str = "l_shoulder_base_link",
    ) -> Dict[str, Any]:
        """Auto-calibrate the hand mappings from the current pose — ONE gesture:
        stand facing the robot, hold BOTH ARMS STRAIGHT FORWARD, trigger this
        (desktop Calibrate button or the in-VR menu countdown). It recenters
        (position + heading) to your head, then per hand sets offset3 = the robot
        shoulder position, scale3 = reach_mm / your measured forward reach, and a
        shoulder±reach clamp — so retracting your hand maps to the robot shoulder
        and extending maps to its forward reach. Your hand pairs to the robot's."""
        # Align the frame (position + heading) to the current head pose first,
        # then let a few input frames refresh _norm before measuring the hands.
        raw = (self._norm or {}).get("_head_raw")
        if raw:
            await self.update_config({
                "origin_mm": [round(c, 2) for c in raw["pos"]],
                "origin_yaw_deg": round(float(raw["yaw"]), 3),
            })
            await asyncio.sleep(0.25)
        ctl = (self._norm or {}).get("controller") or {}
        hands: Dict[str, Mapping] = {}
        for m in self.config.mappings:
            if m.action == "set_target" and isinstance(m.args, dict):
                ee = str(m.args.get("ee", ""))
                if ee == "right_hand":
                    hands["right"] = m
                elif ee == "left_hand":
                    hands["left"] = m
        if not hands:
            return {"error": "no hand set_target mappings — install the teleop preset first"}
        tgt = target or next(iter(hands.values())).target
        state = await self._read_target_state(tgt)
        if not state:
            return {"error": f"no state from {tgt} — is it running?"}
        links = state.get("link_positions") or {}
        new_maps = [m.model_copy() for m in self.config.mappings]
        by_id = {m.id: m for m in new_maps}
        out = []
        for side, m in hands.items():
            cc = ctl.get(side) or {}
            pose = cc.get("grip") or cc.get("ray") or {}
            C = pose.get("pos")
            S = links.get(right_shoulder_link if side == "right" else left_shoulder_link)
            if not C or not S:
                continue
            human_reach = max((C[0] ** 2 + C[1] ** 2 + C[2] ** 2) ** 0.5, 300.0)
            scale = round(float(reach_mm) / human_reach, 4)
            u = by_id[m.id]
            u.scale3 = [scale, scale, scale]
            u.offset3 = [round(float(S[0]), 1), round(float(S[1]), 1), round(float(S[2]), 1)]
            u.clamp_min = [round(S[0] - reach_mm, 1), round(S[1] - reach_mm, 1), round(S[2] - reach_mm, 1)]
            u.clamp_max = [round(S[0] + reach_mm, 1), round(S[1] + reach_mm, 1), round(S[2] + reach_mm, 1)]
            out.append({"side": side, "scale": scale, "anchor": u.offset3})
        if not out:
            return {"error": "no controller+shoulder data — recenter, hold arms forward, in view"}
        await self.update_config({"mappings": [m.model_dump() for m in new_maps]})
        await self._publish_state()
        return {"calibrated": out}

    @service_method("set_teleop", publishes=["state"])
    async def m_set_teleop(self, enabled: bool, target: str = "robot_kinematics-1") -> Dict[str, Any]:
        """Turn InMoov teleop ON/OFF as a DURABLE state — it stays until you
        change it (survives restart, since the config persists). ON installs the
        teleop mapping set and silences the robot's head look_at EE + table
        barrier; OFF removes the teleop mappings and restores them. Non-teleop
        mappings are left alone."""
        others = [m for m in self.config.mappings if m.id not in _TELEOP_IDS]
        maps = others + _teleop_mappings(target) if enabled else others
        await self.update_config({
            "mappings": [m.model_dump() for m in maps],
            "teleop_enabled": bool(enabled),
        })
        # Prep the robot: direct head control wants the look_at EE off; smooth
        # tracking wants the table barrier off. Restore both when disabling.
        try:
            ctrl = _control_topic(target)
            await self.bus.publish(ctrl, {"action": "set_ee_enabled", "name": "head", "enabled": not enabled})
            await self.bus.publish(ctrl, {"action": "set_obstacle_avoidance", "enabled": not enabled})
        except Exception:  # noqa: BLE001 — robot may be down; mappings still set
            logger.exception("webxr %s: set_teleop robot prep failed", self.proxy_id)
        await self._publish_state()
        return {"teleop_enabled": bool(enabled), "mappings": len(maps)}

    @service_method("set_enabled", publishes=["state"])
    async def m_set_enabled(self, enabled: bool) -> Dict[str, Any]:
        await self.update_config({"enabled": bool(enabled)})
        await self._publish_state()
        return {"enabled": bool(enabled)}

    @service_method("set_panel", publishes=["state"])
    async def m_set_panel(self, panel: Dict[str, Any]) -> Dict[str, Any]:
        """Add or update a feed panel (matched by id)."""
        spec = PanelSpec(**panel)
        panels = [p for p in self.config.panels if p.id != spec.id]
        panels.append(spec)
        await self.update_config({"panels": [p.model_dump() for p in panels]})
        await self._publish_panels()
        await self._publish_state()
        return {"panels": len(panels)}

    @service_method("remove_panel", publishes=["state"])
    async def m_remove_panel(self, id: str) -> Dict[str, Any]:
        panels = [p for p in self.config.panels if p.id != id]
        await self.update_config({"panels": [p.model_dump() for p in panels]})
        await self._publish_panels()
        await self._publish_state()
        return {"panels": len(panels)}

    @service_method("set_mapping", publishes=["state"])
    async def m_set_mapping(self, mapping: Dict[str, Any]) -> Dict[str, Any]:
        """Add or update a controller→actuator mapping (matched by id)."""
        m = Mapping(**mapping)
        maps = [x for x in self.config.mappings if x.id != m.id]
        maps.append(m)
        await self.update_config({"mappings": [x.model_dump() for x in maps]})
        await self._publish_state()
        return {"mappings": len(maps)}

    @service_method("remove_mapping", publishes=["state"])
    async def m_remove_mapping(self, id: str) -> Dict[str, Any]:
        maps = [x for x in self.config.mappings if x.id != id]
        await self.update_config({"mappings": [x.model_dump() for x in maps]})
        await self._publish_state()
        return {"mappings": len(maps)}

    @service_method("set_hand_tracking", publishes=["state"])
    async def m_set_hand_tracking(self, enabled: bool) -> Dict[str, Any]:
        await self.update_config({"hand_tracking": bool(enabled)})
        await self._publish_panels()
        await self._publish_state()
        return {"hand_tracking": bool(enabled)}

    # ─── outbound config + status ─────────────────────────────────────
    async def _publish_panels(self) -> None:
        """Retained config the immersive client renders from."""
        await self.publish("panels", {
            "reference_space": self.config.reference_space,
            "publish_rate_hz": self.config.publish_rate_hz,
            "hand_tracking": self.config.hand_tracking,
            "panels": [p.model_dump() for p in self.config.panels],
        }, retained=True)

    def _snapshot(self) -> Dict[str, Any]:
        head = self._norm.get("head") if isinstance(self._norm, dict) else None
        ctrl = self._norm.get("controller", {}) if isinstance(self._norm, dict) else {}
        # "connected" = we've heard from a headset in the last 2s.
        connected = (time.time() - self._last_input_ts) < 2.0 if self._last_input_ts else False
        return {
            "enabled": self.config.enabled,
            "connected": connected,
            "session": self._session,
            "reference_space": self.config.reference_space,
            "publish_rate_hz": self.config.publish_rate_hz,
            "hand_tracking": self.config.hand_tracking,
            "origin_mm": self.config.origin_mm,
            "head": head,
            "controller": {
                "left": _ctrl_summary(ctrl.get("left")),
                "right": _ctrl_summary(ctrl.get("right")),
            },
            "panels": [p.model_dump() for p in self.config.panels],
            "mappings": [m.model_dump() for m in self.config.mappings],
            "teleop_enabled": self.config.teleop_enabled,
            "ts": time.time(),
        }

    async def _publish_state(self) -> None:
        self._last_state_pub = time.time()
        await self.publish("state", self._snapshot(), retained=True)


def _ctrl_summary(c: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(c, dict):
        return None
    pose = c.get("grip") if isinstance(c.get("grip"), dict) else c.get("ray")
    return {
        "ray": c.get("ray"),
        "pos": pose.get("pos") if isinstance(pose, dict) else None,
        "euler": c.get("euler"),   # {pan(yaw), tilt(pitch), roll} deg
        "buttons": c.get("buttons", {}),
        "axes": c.get("axes", {}),
        "components": c.get("components", {}),
    }


def _control_topic(target: str) -> str:
    """Control topic for a mapping target. Services subscribe on
    ``/<type>/<id>/control`` — NOT ``/<id>/control`` — so a bare proxy id
    ``robot_kinematics-1`` is expanded to ``/robot_kinematics/robot_kinematics-1/control``
    by stripping the trailing instance suffix for the type. An explicit
    ``type/id`` path (e.g. ``servo/head_pan``) is used verbatim — needed when
    the type can't be derived from the id (named servos)."""
    t = target.strip().strip("/")
    if "/" in t:
        return f"/{t}/control"
    typ = t
    if "-" in t and t.rsplit("-", 1)[1].isdigit():
        typ = t.rsplit("-", 1)[0]
    return f"/{typ}/{t}/control"


def _teleop_mappings(target: str = "robot_kinematics-1") -> List[Mapping]:
    """The InMoov teleop mapping set — both hands (IK position, grip-to-engage
    deadman, reach scale/clamp, ease-iters), both wrist-rolls, head pan/tilt/roll
    (direct joints from headset orientation), and trigger→finger-curl. Fixed ids
    so re-enabling replaces and set_teleop can find + remove them. Deadman uses
    the normalized buttons.grip/trigger (never components.<xr-standard-id>)."""
    def sq(s: str) -> str:
        return f"controller.{s}.buttons.grip"
    maps: List[Mapping] = []
    # Per-hand anchor + reachable clamp in the URDF world frame (X-fwd, Y-left,
    # Z-up, mm), from the InMoov workspace scan: hands stay smooth over
    # x∈[-100,350], z∈[950,1350], y∈[±50,±425] (can't cross the centreline).
    # Anchor at the reachable centre so grip-neutral sits mid-range; Calibrate
    # refines per operator from the live shoulder + reach.
    hands = (
        ("right", "right_hand", [125.0, -240.0, 1150.0], [-100.0, -450.0, 900.0], [350.0, -30.0, 1350.0]),
        ("left",  "left_hand",  [125.0,  240.0, 1150.0], [-100.0,   30.0, 900.0], [350.0,  450.0, 1350.0]),
    )
    for s, ee, off, cmin, cmax in hands:
        maps.append(Mapping(
            id=f"tele_{s}_hand", source=f"controller.{s}.grip", target=target, action="set_target",
            args={"ee": ee, "solve": True, "iters": 10, "persist": False}, scale3=[0.85, 0.85, 0.85], offset3=off,
            clamp_min=cmin, clamp_max=cmax,
            enable_source=sq(s), enable_min=0.5, ema=0.15))
    for ax, j in (("pan", "head_pan_joint"), ("tilt", "head_tilt_joint"), ("roll", "head_roll_joint")):
        # pan/tilt/roll all -1 (flipped to match the operator's head direction).
        maps.append(Mapping(id=f"tele_head_{ax}", source=f"head.euler.{ax}", target=target,
                            action="set_joint", args={"joint": j}, arg_key="angle", scale=-1.0, ema=0.4))
    for s, j in (("right", "r_wrist_roll_joint"), ("left", "l_wrist_roll_joint")):
        maps.append(Mapping(id=f"tele_{s}_wrist", source=f"controller.{s}.euler.roll", target=target,
                            action="set_joint", args={"joint": j}, arg_key="angle",
                            enable_source=sq(s), enable_min=0.5, ema=0.3))
    for s, side in (("right", "r"), ("left", "l")):
        # NO deadman on finger curl: the trigger should ALWAYS close the hand,
        # independent of the grip (holding the grip to move the arm is separate).
        # scale 1.4 so a light squeeze fully closes (trigger>~0.7 → closed).
        maps.append(Mapping(id=f"tele_{s}_grip", source=f"controller.{s}.buttons.trigger", target=target,
                            action="set_hand", args={"side": side, "persist": False}, arg_key="amount",
                            scale=1.4, deadzone=0.04))
    return maps


_TELEOP_IDS = {m.id for m in _teleop_mappings()}


def _resolve_path(root: Dict[str, Any], path: str) -> Any:
    """Walk a dotted path into a nested dict; None if any hop is missing."""
    cur: Any = root
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _as_scalar(v: Any) -> Optional[float]:
    """Coerce a resolved signal to a float for scalar/deadman use: numbers
    pass through, bools → 0/1, a gamepad component dict → its value/state/
    pressed; anything else → None. Lets ``enable_source`` point at a button
    (bool), a components.*.value (float), or a whole component dict."""
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for k in ("value", "state", "pressed"):
            if k in v:
                return _as_scalar(v[k])
    return None


def _axis(seq: Any, i: int, default: Any) -> Any:
    """seq[i] if present, else default — tolerates None / short lists so the
    per-axis mapping fields stay optional."""
    try:
        return seq[i]
    except (TypeError, IndexError, KeyError):
        return default
