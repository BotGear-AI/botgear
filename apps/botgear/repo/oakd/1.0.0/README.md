# oakd — OAK-D depth camera service

Subprocess botgear service wrapping Luxonis' DepthAI V2 API for OAK-D
cameras. Streams RGB + colorized depth as MJPEG, with opt-in metric
depth/feature-flow sampling for other services (`slam`) to consume.

```
type:      oakd
version:   1.0.0
language:  python (subprocess — own venv, depthai is large and
                    version-coupled to the RVC2 chip)
```


## Architecture

One `OakdService` instance streams RGB + colorized depth as two
independent MJPEG streams via the same `register_stream`/`Stream`
mechanism the `video` service uses (`oakd/<id>/rgb`, `oakd/<id>/depth`),
sharing one capture loop. Pinned to `depthai>=2.24,<3` — V3's RVC2
maturity wasn't verifiable against real hardware when this was pinned.

Optional low-cost additions (all opt-in, zero cost when off):

- `publish_depth_samples` — a decimated, metric-millimeter depth grid
  on `/oakd/<id>/depth_sample`, built from StereoDepth's separate
  `.depth` output (not `.disparity`, which is relative units only good
  for the colorized preview). Feeds the `slam` service.
- `publish_feature_flow` — DepthAI's on-device `FeatureTracker` on
  `mono_left`, publishing matched-id point correspondences + depth on
  `/oakd/<id>/feature_flow_sample`, for visual odometry (`slam`'s
  visual-odometry phase). Mutually exclusive with `depth_align_to_rgb`
  (raises at device-open) since visual flow needs depth to stay in
  mono_left's native frame.
- `mono_stream_camera` — raw single mono-camera MJPEG preview (left OR
  right, never both — confirmed on real hardware that both plus the
  existing RGB+depth load exceeds USB2 bandwidth on an OAK-D Lite).

Filter pipeline (`filters/`) is a near-verbatim duplicate of `video`'s
`Filter`/`make_param`/`_REGISTRY` shape, applied to the RGB stream
only — intentionally not extracted into a shared package until ≥2 real
consumers exist.

On-device spatial AI (real-world x/y/z per detection) remains an
explicitly deferred future phase — the `yolo.py` filter that exists
runs host-side on decoded RGB frames, NOT on-device Myriad inference.


## Gotchas

- **USB permissions**: requires a one-time udev rule (see "OAK-D USB
  permissions" in `apps/botgear/AGENTS.md`) — without it `depthai`
  fails to open the device even though `lsusb` sees it. Connect
  failures surface a friendly message in `/state.error` rather than a
  bare traceback (`_friendly_error` pattern-matches on
  "permission"/"boot"/"x_link" strings — DepthAI's actual exception
  for a missing udev rule is the generic "Failed to boot device!", not
  an obviously permission-shaped string).
- **USB3 required** — USB2 causes bandwidth starvation/crashes on the
  Lite specifically. `/state.usb_speed` reports what was actually
  negotiated.
- **IMU presence is genuinely hardware-dependent**, not a fixed spec:
  `device.getConnectedIMU()` returns an empty string (not `"NONE"`)
  when absent — confirmed no IMU on the real OAK-D Lite unit used here.
- **`depth_align_to_rgb`** aligns StereoDepth output to the RGB
  camera's frame and bounds output to 640x480 (not RGB's native
  resolution) to avoid multiplying encode cost.
