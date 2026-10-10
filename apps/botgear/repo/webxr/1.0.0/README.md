# webxr — WebXR teleop (Meta Quest 3)

Turns a WebXR headset + Touch controllers into a two-way teleop console.
Inbound, the headset streams head/controller pose, buttons and
thumbsticks; the service converts them into the robot frame and
republishes them on the bus (and can drive actuators directly through a
mapping table). Outbound, an immersive in-headset view renders camera
feeds, telemetry, live 3-D robot scene and same-origin web UIs as panels
the operator arranges around them.

```
type:     webxr
version:  1.0.0
language: python (subprocess, own venv — light deps)
bundled:  false (installs on demand; heavy 3-D libs live only in its UI bundles)
```

## Architecture

```
 Quest Browser — immersive client (xr.js, full page)
   reads head + controller pose / buttons / axes each XRFrame
   renders panels (video, telemetry, scene, browser), menu, grid
        │ publish /webxr/<id>/input (throttled)      ▲ subscribe /webxr/<id>/panels (retained)
 ───────┴──────────── runtime bus WebSocket (/v1/ws, authed) ──┴──────────
        ▼                                            │
 WebXRService (Python subprocess)
   frame-convert WebXR (Y-up, m) → robot (Z-up, mm), recenter origin + heading
   republish head / controller/{left,right} / joystick/{left,right}
   run the mapping table → actuator control topics (IK target, servo, motor)
   own panels / mappings / origin config; publish panels (retained) + state
```

Stack: WebXR Device API (`immersive-vr`), `@react-three/xr` v6
(`createXRStore`, `<XR>`, `useXRInputSourceState`) on react-three-fiber.

## Two UI bundles, one service

| Bundle | Source | Where it runs |
|---|---|---|
| `ui/dist/ui.js` (small) | `ui/View.tsx` | Desktop control panel — composer / dock, like any service UI |
| `xr.js` (heavy, single file) | `ui/xr/View.tsx` | Full page on the headset at `/r/<runtime>/xr/<proxy>` |

An `XRSession` needs the whole page and a user gesture ("Enter VR"), so
the immersive client can't live in a composer panel. The host app serves
it from `/repo/webxr/1.0.0/xr.js` and routes `/r/:runtimeId/xr/:proxyId`
to a chrome-less page (same shape as the dock route).
`build-service-ui.mjs` emits the second entry single-file and honours the
bundle's `rlx.aliases` (used to stub out the IWER emulator: ~9 MB → ~2 MB).
Keeping the bundles separate means the desktop panel never loads
`@react-three/xr`.

**Desktop panel:** session status + fps, live head/controller readout
(robot frame), Recenter / Clear origin / Calibrate teleop, feed manager
(add/remove panels, anchor selector), mapping list + the InMoov teleop
preset, and the "open on headset" URL with the `adb reverse` hint.

## Topics

| Topic | Direction | Payload |
|---|---|---|
| `/webxr/<id>/input` | headset → service | raw XR-frame head + controllers (pose, buttons, axes) |
| `/webxr/<id>/head` | published | robot-frame pose + pan/tilt (heading-relative) |
| `/webxr/<id>/controller/{left,right}` | published | grip + ray pose, buttons, axes |
| `/webxr/<id>/joystick/{left,right}` | published | gamepad-shaped `{axes:[x,y], buttons:[…]}` — a motor_control `input_source` binds to it exactly like a physical joystick |
| `/webxr/<id>/panels` | published, retained | the panel list the immersive client renders |
| `/webxr/<id>/state` | published, retained | desktop-panel snapshot |

Wire shapes are declared in `service.py` (`HeadPayload`,
`ControllerPayload`, `GamepadPayload`, `PanelsPayload`, `WebXRState`) and
introspected into `/runtime/runtime/types/webxr`.

**Control actions** (`/webxr/<id>/control`): `recenter`, `clear_origin`,
`calibrate_teleop`, `set_teleop`, `set_enabled`, `set_panel`,
`remove_panel`, `set_mapping`, `remove_mapping`, `set_hand_tracking`.

## Frames and calibration

WebXR is Y-up, metres, right-handed; botgear is Z-up, mm (`frames.py`,
pure Python). **Recenter** stores the head position (`origin_mm`) and yaw
(`origin_yaw_deg`); every published position is offset and rotated by
−yaw, so the operator's forward is robot +X wherever they physically face.

**`calibrate_teleop`** — one gesture: stand facing the robot, arms straight
forward, trigger it (desktop button, or the in-VR menu's countdown). It
recenters, then for each hand `set_target` mapping sets `offset3` = that
robot shoulder's position, `scale3` = `reach_mm` / your measured reach, and
a shoulder ± reach clamp box — your retracted hand maps to the robot's
shoulder, your extended hand to its full reach. Requires the teleop preset
and a running target that publishes `link_positions`.

## Mapping table

`Mapping` (`model.py`) routes a normalized signal to an actuator action:
`source` is a dotted path (`controller.right.ray`, `controller.left.axes.ty`,
`head`), `target` a proxy id, `action` its control action, `args` merged
into the payload. Pose sources inject x/y/z (mm) after a per-axis affine
(`scale3`, `offset3`) and an optional `clamp_min`/`clamp_max` box; scalar
sources go through `deadzone`, then `value*scale + offset` into `arg_key`.
`enable_source`/`enable_min` is a deadman clutch (fire only while a button is
held); `ema` smooths the output.

`set_teleop(enabled)` installs the InMoov preset (both hands →
robot_kinematics `set_target`) and silences the robot's head look-at and
obstacle barrier while teleop is on; `teleop_enabled` persists across
restarts.

## Panels

Every feed is a `PanelSpec` — one primitive, rendered by one
`ManipulablePanel` wrapper that owns all manipulation:

| `source.kind` | Renders |
|---|---|
| `video_mjpeg` | an MJPEG stream URL as a texture |
| `telemetry` | a bus state topic as a text HUD |
| `scene` | a live 3-D robot: `binding: link_poses` maps robot_kinematics' per-link poses onto the GLB found at `asset_base` + the live `state.visual` |
| `browser` | a same-origin web UI (e.g. a dock view) loaded in a hidden iframe and rasterized to a texture |
| `video_webrtc` | reserved — not implemented |

**Anchors** (`placement`) — a panel's `transform` is interpreted in its
anchor frame, so moving/scaling works the same under every anchor:
`world` (fixed in the room), `head` (in front of you, lazy-follow with a
deadzone — `lazy` is the stiffness, 0.12 default), `body` (follows position
+ yaw only, for cockpit dashboards), `wrist` (on the left controller).

**Interaction** (Touch controllers):
- Arrange mode is **off** by default — panels are locked and rays pass
  through. Toggle with the left **Y** button or the menu.
- In arrange mode, **trigger** on a panel grabs it (6-DOF, follows the
  controller); right thumbstick **Y** pushes/pulls along the ray, **X**
  scales. On release the pose is converted back into the anchor frame
  and sent with `set_panel`, so the desktop panel, the config file and any
  other headset stay in sync (the bus is the source of truth; nothing is
  sent per frame).
- A head-locked **gear** opens the menu panel: teleop on/off, calibrate
  (with countdown), arrange on/off, recenter, per-panel anchor cycle /
  show-hide / remove, add a service view or model, exit VR.

## Running it on a Quest 3

WebXR needs a secure context. For development, USB + `adb reverse` makes
the runtime reachable at `localhost`, which counts as secure — no TLS:

1. Install + start a `webxr` service from the catalog; open its desktop panel.
2. `adb reverse tcp:8998 tcp:8998` with the Quest connected by USB.
3. In the Quest Browser, sign in at `http://localhost:8998`, then open
   `http://localhost:8998/r/<runtime>/xr/<webxr-proxy-id>` and press
   **Enter VR** — expect a reference grid, origin axes and both controllers.
4. The desktop panel should show the headset connected with live values.
   **Recenter** in your neutral pose.
5. Teleop: start `robot_kinematics-1`, turn teleop on, **Calibrate** — the
   InMoov arms follow your hands.
6. Video: add a `video_mjpeg` panel with a camera URL.

The immersive client authenticates with the headset browser's normal login
session.

## Watch-items

- Bus throughput at 60–90 Hz input — the client throttles to
  `publish_rate_hz`; switch to a binary encoding if JSON overhead bites.
- Motion-to-photon latency of MJPEG video panels.
- Quest Browser WebXR feature drift across Horizon OS updates.
- `xr.js` is ~2 MB, served once from the runtime.
