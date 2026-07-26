"""WebXR teleop service config + nested specs.

The config persists across restarts (the panel layout, controller→
actuator mappings, recenter origin). The immersive client + desktop
control panel both read it off the bus; the desktop panel edits it via
service actions.
"""
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field
from rlx_bus import ServiceConfig


# ── Source kinds for a feed panel ────────────────────────────────────
# video_mjpeg — an MJPEG stream URL (existing botcore video service).
# video_webrtc — reserved (Phase 2): a WebRTC track.
# telemetry — render a bus state topic as a text/gauge HUD.
# scene — a live 3-D ghost driven by a topic's link_poses (robot_kinematics).
# browser — surface a same-origin web UI (e.g. a service's "Open in window"
#   dock view at /r/<runtime>/dock/<proxy>?view=…). The immersive client
#   loads it in a hidden iframe and rasterizes it to a texture (DOM can't be
#   composited into an immersive WebXR scene). ``ref`` is the same-origin URL.
# Anchor — the frame a panel's transform is interpreted in:
#   world — fixed in the room
#   head  — in front of the viewer (lazy-follow); the primary camera
#   body  — follows position + yaw only (cockpit dashboards)
#   wrist — stuck to the left controller (palette)
PanelSourceKind = Literal["video_mjpeg", "video_webrtc", "telemetry", "scene", "browser"]
PanelPlacement = Literal["world", "head", "body", "wrist"]
PanelShape = Literal["flat", "curved", "equirect"]


class PanelSource(BaseModel):
    kind: PanelSourceKind = "video_mjpeg"
    # MJPEG/stream URL, or a bus topic (telemetry/scene). Interpreted by kind.
    ref: str = ""
    # ── scene (3-D model) fields ─────────────────────────────────────
    # binding — names the client-side pose adapter that maps the bound
    #   topic's state payload onto the model. "link_poses" (robot_kinematics:
    #   per-link world pos+quat) is the only adapter today; the immersive
    #   client keys a registry on this so new robots / model conventions
    #   (joint angles, static props, tf trees) drop in without a schema change.
    binding: str = "link_poses"
    # asset_base — the "/repo/<type>/<version>/file/" prefix the client
    #   prepends to the LIVE state.visual to resolve the GLB URL. Set by
    #   whoever creates the panel (it knows the service's meta id); resolving
    #   visual live — not baking a full URL — keeps the model correct when the
    #   service swaps rigs. Empty for sources that don't load a model.
    asset_base: str = ""


class PanelTransform(BaseModel):
    pos: List[float] = Field(default_factory=lambda: [0.0, 1.4, -1.5])  # metres, XR frame
    quat: List[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0, 1.0])
    width_m: float = 1.2
    height_m: float = 0.7
    scale: float = 1.0


class PanelAudio(BaseModel):
    enabled: bool = False
    spatial: bool = False
    volume: float = 1.0


class PanelSpec(BaseModel):
    """One feed surface in the headset — the single primitive every feed
    (video / telemetry / scene) is expressed as."""
    id: str
    title: str = ""
    source: PanelSource = Field(default_factory=PanelSource)
    placement: PanelPlacement = "world"
    # Lazy-follow stiffness for head/body anchors (0 = frozen, 1 = rigid
    # 1:1). ~0.12 gives a comfortable damped follow with a deadzone.
    lazy: float = 0.12
    transform: PanelTransform = Field(default_factory=PanelTransform)
    shape: PanelShape = "flat"
    # Render via a WebXR media layer (high quality) vs a textured mesh.
    layer: bool = False
    audio: PanelAudio = Field(default_factory=PanelAudio)
    enabled: bool = True


class Mapping(BaseModel):
    """Route a normalized headset signal to an actuator action.

    ``source`` is a dotted path into the normalized data, e.g.
    ``controller.right.ray`` (a pose), ``controller.left.axes.ty`` (a
    scalar), or ``head``. ``target`` is a service proxy id; ``action``
    its control action. ``args`` are merged into the published payload.
    For scalar sources, ``arg_key`` names the field to inject the
    (scaled/offset) value into; for pose sources, x/y/z (mm) are injected.
    """
    id: str
    enabled: bool = True
    source: str = "controller.right.ray"
    target: str = ""                      # actuator proxy id, e.g. "robot_kinematics-1"
    action: str = "set_target"            # its control action
    args: Dict[str, object] = Field(default_factory=dict)
    arg_key: Optional[str] = None         # scalar sources: field to fill
    scale: float = 1.0                    # scalar sources: value*scale + offset
    offset: float = 0.0
    # ── teleop transforms (all default to no-op; backward compatible) ──
    # Pose sources: per-axis affine BEFORE inject — reach scaling about the
    # shoulder (scale3) then shoulder/world anchor (offset3), then a workspace
    # clamp box so an out-of-reach controller can't drive IK into a thrash.
    scale3: List[float] = Field(default_factory=lambda: [1.0, 1.0, 1.0])
    offset3: List[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    clamp_min: Optional[List[float]] = None   # [x,y,z] mm, or None
    clamp_max: Optional[List[float]] = None
    # Scalar sources: zero out |value| < deadzone before scale/offset (kills
    # trigger/stick jitter near rest).
    deadzone: float = 0.0
    # Deadman / clutch: only fire while the value at ``enable_source`` (a
    # button/component/scalar path) is >= enable_min. Empty = always armed.
    enable_source: str = ""
    enable_min: float = 0.5
    # Exponential smoothing on the emitted value, 0..1 (0 = off, higher =
    # smoother): out = ema*prev + (1-ema)*new. Tames raw 60 Hz jitter.
    ema: float = 0.0


class WebXRConfig(ServiceConfig):
    """Persisted working state for one WebXR teleop instance."""
    model_config = {"protected_namespaces": (), "extra": "allow"}

    enabled: bool = True
    reference_space: Literal["local-floor", "local"] = "local-floor"
    publish_rate_hz: int = 60                       # advisory cap sent to the client
    # Recenter offset in robot mm (subtracted from published positions).
    origin_mm: Optional[List[float]] = None
    # Recenter heading (deg): the operator's head yaw at recenter time. Every
    # controller/head position is rotated by −origin_yaw about Z so the
    # operator's forward maps to robot forward (+X) regardless of which way they
    # physically face — "forward is forward". None = no yaw alignment.
    origin_yaw_deg: Optional[float] = None
    panels: List[PanelSpec] = []
    layouts: Dict[str, List[PanelTransform]] = {}   # named arrangements (future use)
    active_layout: Optional[str] = None
    mappings: List[Mapping] = []
    hand_tracking: bool = False
    # Durable teleop on/off. When True the InMoov teleop mapping set is
    # installed + the robot's head look_at EE / table barrier are silenced;
    # stays until explicitly turned off. Persisted, so teleop survives restart.
    teleop_enabled: bool = False
