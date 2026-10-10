# unmanaged
"""OakdService — Luxonis OAK-D depth camera capture + streaming.

DepthAI V2 API (pinned `depthai>=2.24,<3.0` — see this type's README.md
for why V2 over the newer V3). Two on-device pipeline outputs are streamed:

  * RGB   — ColorCamera.video -> XLinkOut, exactly like the ``video``
            service's webcam frames (BGR uint8, via ``getCvFrame()``).
  * Depth — two MonoCameras -> StereoDepth -> XLinkOut(disparity),
            normalized + colorized (cv2.applyColorMap) on the host
            before JPEG encoding, since MJPEG is an 8-bit transport and
            disparity/depth is computed on-device at higher precision.

Optionally (``publish_depth_samples``, opt-in, default off) a THIRD
on-device output — StereoDepth's separate ``.depth`` (already metric
millimeters) — is decimated to a small grid and published as plain JSON
on ``/oakd/<id>/depth_sample`` at a low, independently-configurable
rate. Added for the `slam` service (repo/slam/1.0.0/README.md § Ownership split) — raw
disparity alone can't convert to real-world range without stereo
calibration intrinsics, which is why this is a separate on-device
output rather than reusing the disparity one above.

Optionally (``publish_feature_flow``, opt-in, default off, independent
of ``publish_depth_samples``) a FOURTH on-device output — DepthAI's
FeatureTracker node running on the left mono camera — publishes
matched-id point correspondences + per-point metric depth on
``/oakd/<id>/feature_flow_sample``, for visual (optical-flow) odometry.
See repo/slam/1.0.0/README.md § Visual odometry — this is that design's Phase 4a.

Optionally (``mono_stream_camera``, "left"/"right"/None, default None)
ONE more MJPEG stream — ``/oakd/<id>/mono_left`` or
``/oakd/<id>/mono_right`` — the raw individual grayscale feed from one
mono camera, untouched by stereo matching. Added to let an operator
visually confirm neither camera's own view is physically occluded (a
mount bracket, cable) — something the fused colorized depth/disparity
preview can't show, since it's already a product of both cameras
together. ONE AT A TIME, not both simultaneously — confirmed live on
worke's real hardware (a USB2-limited OAK-D Lite, see the worke
hardware doc in the Obsidian vault) that both mono streams at once,
stacked on top of the already-running RGB+depth+depth_mm+features load,
exceeds available USB bandwidth ("Couldn't open stream" on device
open). Switched live via the ``set_mono_stream`` action (reconnects —
the DepthAI pipeline graph is fixed at device-open time).

All MJPEG streams share one capture loop and one ``register_stream``
call each (``oakd/<id>/rgb``, ``oakd/<id>/depth``, and the single mono
stream when one is selected) — the same ``rlx_bus.Stream`` mechanism
the ``video`` service uses, just called per-stream from one proxy. The
filter pipeline (``filters/``) is a near-verbatim port of
``video_service.filters`` — same
``Filter``/``make_param``/``_REGISTRY`` shape, applied to the RGB
stream only.

Wire shape for every published topic is declared below (``OakdState``,
``DepthSamplePayload``, ``FeatureFlowPayload``, snapshot + filter
models via ``state_schema``/``topic_schemas``) and introspected into
``/runtime/runtime/types/oakd``, as are the ``control`` actions.

REQUIRES a one-time host udev rule — see "OAK-D USB permissions" in
AGENTS.md. Connect failures (missing udev rule, no USB3, device
unplugged) don't abort startup — the service stays up and reports
``/state.error``, same posture as ``video``.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from typing import Any, Dict, List, Literal, Optional

import cv2
import numpy as np
from pydantic import BaseModel, ConfigDict, Field
from rlx_bus import ServiceConfig, Stream, SubprocessService, service_method

from .filters import Filter, build_filter, catalog as filter_catalog


logger = logging.getLogger(__name__)

_RESOLUTION_MAP = {
    "1080p": "THE_1080_P",
    "4k": "THE_4_K",
}
_COLORMAP_MAP = {
    "jet": "COLORMAP_JET",
    "hot": "COLORMAP_HOT",
    "turbo": "COLORMAP_TURBO",
    "bone": "COLORMAP_BONE",
}

# Bounds StereoDepth's aligned-to-RGB output when depth_align_to_rgb is on
# (via setOutputSize — "Currently only applicable when aligning to RGB
# camera" per the pinned depthai==2.32.0.0 API). Deliberately the SAME
# resolution as mono_left/mono_right's own THE_480_P, not the RGB camera's
# native resolution (1920x1080 at the 1080p default, more at 4k) — without
# this, aligning to RGB would multiply the pixel count of the ALWAYS-ON
# colorized /depth preview + depth_sample's decimation source several-fold
# for zero benefit (depth_sample decimates down to a coarse grid either
# way), just to satisfy an alignment step that only cares about matching
# FIELD OF VIEW, not matching resolution.
_DEPTH_ALIGN_OUTPUT_W = 640
_DEPTH_ALIGN_OUTPUT_H = 480

# A decimated cell backed by fewer than this fraction of valid native
# source pixels reports invalid (0) rather than a low-confidence average
# computed from just a couple of stray readings.
_MIN_VALID_FRACTION = 0.3


def _decimate_depth_masked(depth_mm: np.ndarray, cols: int, rows: int) -> np.ndarray:
    """Downsample a metric depth-in-mm array to (rows, cols), area-
    averaging ONLY the valid (>0) source pixels in each output cell —
    NOT a plain `cv2.resize(..., INTER_AREA)`, which has no concept of
    "0 means invalid" and blends invalid pixels into the average as if
    they were real close-range readings. A decimated cell straddling a
    real depth reading and an invalid region gets pulled toward 0 by
    the invalid pixels mixed in — exactly the "close-range noise at
    edges" pattern this grid's consumers (the `slam` service) have had
    to filter around all session: not sensor noise, an artifact of
    averaging in zeros. Confirmed live, real hardware, this session
    (the SLAM debugging that led here): the same artifact also dilutes
    a REAL object's signal when it only partially covers a decimated
    cell, which was making a physically-present box intermittently
    undetectable at the map layer.

    cv2 has no masked/NaN-aware area-average built in, so this computes
    it from two plain INTER_AREA resizes instead: one of the depth
    values (invalid pixels contribute 0), one of the 0/1 validity mask
    (whose area-average IS exactly the fraction of valid source pixels
    in that cell, since averaging 0s and 1s gives a proportion) —
    dividing the first by the second recovers the average among ONLY
    the valid pixels. A cell backed by too FEW valid source pixels
    (< `_MIN_VALID_FRACTION`) reports 0/invalid rather than a low-
    confidence average from a couple of stray readings.
    """
    valid = depth_mm > 0
    depth_f = np.where(valid, depth_mm, 0).astype(np.float32)
    valid_f = valid.astype(np.float32)

    depth_area_avg = cv2.resize(depth_f, (cols, rows), interpolation=cv2.INTER_AREA)
    valid_fraction = cv2.resize(valid_f, (cols, rows), interpolation=cv2.INTER_AREA)

    out = np.zeros((rows, cols), dtype=np.float32)
    trusted = valid_fraction >= _MIN_VALID_FRACTION
    out[trusted] = depth_area_avg[trusted] / valid_fraction[trusted]
    return out


class OakdConfig(ServiceConfig):
    """Config for OakdService.

    Fields:
      * device_mxid    — target a specific unit by its DepthAI MxId
                         (a serial number baked into the device, stable
                         across cables/ports — unlike a USB bus path).
                         None = first available device (the common
                         single-camera case). Auto-populated after the
                         first successful open (see _open_device) so
                         reconnects — including the autoreconnect loop
                         below — target that exact physical unit rather
                         than "whichever OAK-D answers first", which
                         matters once autoreconnect is retrying blindly
                         in the background.
      * autoreconnect  — keep a background loop alive that retries
                         opening the device every
                         _AUTO_CONNECT_RETRY_S seconds whenever
                         disconnected — on startup, after a transient
                         open failure (bad cable/port at the moment the
                         service started), and after a live unplug.
                         Default True: unlike `joystick` (which defaults
                         this off to avoid grabbing a guessed
                         controller), oakd's whole job is one specific
                         depth camera, so retrying until it's found is
                         the desired default. An explicit `disconnect`
                         action turns this off (mirrors joystick's
                         detach) — `connect` turns it back on.
      * rgb_resolution — "1080p" or "4k". 1080p is the better default for
                         a live MJPEG preview (lower encode CPU); 4k is
                         available for higher-res use cases.
      * rgb_fps        — target capture fps for the color camera.
      * depth_enabled  — whether to build the stereo depth pipeline at
                         all. Off saves device compute + host encode CPU
                         if only the RGB feed is needed.
      * depth_colormap — one of "jet"/"hot"/"turbo"/"bone", applied to
                         the normalized disparity map before JPEG
                         encoding. Not part of the filter pipeline — it's
                         required to make 16-bit depth JPEG-streamable at
                         all, not optional post-processing.
      * jpeg_quality   — JPEG encode quality (0-100), same as video.
      * publish_depth_samples — opt-in (default off, zero cost to
                         existing users). Publishes a low-rate,
                         decimated, METRIC (millimeters) depth grid on
                         /oakd/<id>/depth_sample for consumers that need
                         real range data — the `slam` service in
                         particular. See repo/slam/1.0.0/README.md § Ownership split Distinct
                         from the disparity-based colorized MJPEG
                         preview above — raw disparity alone can't
                         convert to real-world range without stereo
                         calibration intrinsics, so this uses
                         StereoDepth's separate `.depth` output
                         (already metric, computed on-device) instead.
      * depth_sample_rate_hz — how often to publish (independent of
                         camera/stream fps — deliberately much lower).
      * depth_sample_cols/_rows — decimation grid size. Coarse on
                         purpose; room-outline tracing doesn't need
                         per-pixel resolution.
      * depth_align_to_rgb — opt-in (default off), only meaningful
                         together with publish_depth_samples. Aligns the
                         on-device stereo depth/disparity to the RGB
                         camera's own perspective (DepthAI setDepthAlign)
                         instead of the mono cameras' native frame, and
                         has each depth_sample tick include a same-shape
                         decimated RGB color grid ("rgb" field) alongside
                         depth_mm — cell (r, c) in each is now the SAME
                         physical point, with no extra alignment math
                         needed downstream. Built for the `slam` service's
                         true-color point cloud. Output resolution is
                         bounded (setOutputSize, see _DEPTH_ALIGN_OUTPUT_W/
                         _H above) so this doesn't multiply the cost of
                         the ALWAYS-ON colorized /depth preview the way
                         aligning to RGB's full native resolution would.
                         Mutually exclusive with publish_feature_flow,
                         which specifically needs (repo/slam/1.0.0/README.md
                         §9.3) depth to stay in mono_left's own frame for
                         its per-pixel lookup — enabling both raises at
                         device-open time rather than silently breaking
                         one of them.
      * publish_feature_flow — opt-in (default off, zero cost to
                         existing users), independent of
                         publish_depth_samples. Runs DepthAI's on-device
                         FeatureTracker (Lucas-Kanade optical flow /
                         hardware motion estimation, VPU-side — not a
                         host CPU workload) on the left mono camera and
                         publishes matched-id point correspondences +
                         per-point metric depth on
                         /oakd/<id>/feature_flow_sample, for visual
                         odometry (the `slam` service; see
                         repo/slam/1.0.0/README.md § Visual odometry). This is Phase 4a of that
                         design — `oakd` stays a dumb sensor publisher
                         (frame-to-frame id matching only); the
                         rigid-motion fit that turns correspondences
                         into a pose delta is SLAM-specific and lives in
                         `slam`, not here.
      * feature_flow_rate_hz — how often to publish, independent of the
                         mono camera's own capture fps (deliberately
                         lower, same idea as depth_sample_rate_hz).
      * mono_stream_camera — "left", "right", or None (default), only
                         meaningful when depth_enabled (mono_left/
                         mono_right only exist in the pipeline for
                         stereo). Raw grayscale MJPEG preview of ONE
                         mono camera — /oakd/<id>/mono_left or
                         /oakd/<id>/mono_right — for visually confirming
                         nothing (a bracket, cable, part of the mount)
                         is physically occluding that camera's own view,
                         something the colorized disparity/depth preview
                         can't show directly since it's already a fused
                         product of both. ONE AT A TIME, not both —
                         confirmed live on worke's real (USB2-limited)
                         OAK-D Lite that running both simultaneously on
                         top of the existing RGB+depth+depth_mm+features
                         load exceeds available USB bandwidth ("Couldn't
                         open stream"); see the worke hardware doc in
                         the Obsidian vault. Switch sides with the
                         set_mono_stream action rather than both fields
                         at once.
    """
    device_mxid: Optional[str] = None
    autoreconnect: bool = True
    rgb_resolution: str = "1080p"
    rgb_fps: float = 30.0
    depth_enabled: bool = True
    depth_colormap: str = "jet"
    jpeg_quality: int = 75
    publish_depth_samples: bool = False
    depth_sample_rate_hz: float = 2.0
    depth_sample_cols: int = 32
    depth_sample_rows: int = 24
    depth_align_to_rgb: bool = False
    publish_feature_flow: bool = False
    feature_flow_rate_hz: float = 5.0
    mono_stream_camera: Optional[Literal["left", "right"]] = None
    # Ordered list of filter specs, applied to the RGB stream. Same
    # shape/persistence contract as video.VideoConfig.filters.
    filters: List[Dict[str, Any]] = []


# ── wire schemas for what this service PUBLISHES ──────────────────────

class Intrinsics(BaseModel):
    """Left mono camera (CAM_B) at 640x480 — the feature_flow uv space."""
    fx: float
    fy: float
    cx: float
    cy: float


class OakdState(BaseModel):
    """Retained on ``/oakd/{id}/state``."""
    connected: bool
    mxid: Optional[str]
    device_name: Optional[str]
    usb_speed: Optional[str] = Field(description="DepthAI UsbSpeed name; anything but SUPER/SUPER_PLUS means reduced bandwidth")
    has_imu: Optional[bool]
    rgb_resolution: Optional[str]
    depth_enabled: bool
    mono_stream_camera: Optional[str] = Field(description="'left' | 'right' | None — the one raw mono MJPEG stream")
    observed_fps: float
    dropped: int
    error: Optional[str]
    autoreconnect: bool
    retrying: bool
    intrinsics: Optional[Intrinsics] = Field(description="Only when publish_feature_flow is on")


class DepthSamplePayload(BaseModel):
    """Published on ``/oakd/{id}/depth_sample`` (opt-in) — metric depth
    decimated to a small grid, row-major; 0 = no valid depth."""
    cols: int
    rows: int
    depth_mm: List[List[int]] = Field(description="rows x cols, millimetres")
    ts: float
    rgb: Optional[List[List[List[int]]]] = Field(None, description="rows x cols x [r, g, b]; only with depth_align_to_rgb")


class FeatureMatch(BaseModel):
    id: int = Field(description="DepthAI's persistent track id")
    prev_uv: List[float]
    curr_uv: List[float]
    prev_depth_mm: float
    curr_depth_mm: float


class FeatureFlowPayload(BaseModel):
    """Published on ``/oakd/{id}/feature_flow_sample`` (opt-in) — tracked
    features seen in both this and the previous tick, both with valid
    depth. uv is mono_left 640x480 pixel space (see ``state.intrinsics``)."""
    ts: float
    matches: List[FeatureMatch]


class SnapshotPayload(BaseModel):
    """Published on ``/oakd/{id}/snapshot`` (event, matched by
    ``request_id``) and — on success — retained on ``latest_snapshot``.
    Same shape as ``video``'s."""
    request_id: Optional[str]
    ts: float
    resolution: Optional[List[int]] = Field(None, description="Success only")
    jpeg_b64: Optional[str] = Field(None, description="Success only")
    error: Optional[str] = Field(None, description="Failure only")


class FilterEntry(BaseModel):
    """One persisted RGB filter spec, enriched from the catalog."""
    model_config = ConfigDict(extra="allow")
    id: Optional[str] = None
    type: Optional[str] = None
    enabled: Optional[bool] = None
    params: Optional[Dict[str, Any]] = None
    title: Optional[str] = None
    publishes_telemetry: Optional[bool] = None
    telemetry_topic: Optional[str] = None


class FiltersPayload(BaseModel):
    filters: List[FilterEntry]


class FilterCatalogEntry(BaseModel):
    type: str
    title: str
    description: str
    publishes_telemetry: bool
    param_schema: List[Dict[str, Any]]
    telemetry_schema: Optional[Dict[str, Any]]


class FilterCatalogPayload(BaseModel):
    filters: List[FilterCatalogEntry]


class FilterIndexEntry(BaseModel):
    """Retained on the GLOBAL ``/filter/index/{filter_id}`` — shared with
    ``video`` (hence ``video_proxy_id``, which here is this oakd's id)."""
    filter_id: str
    type: Optional[str]
    title: Optional[str]
    video_proxy_id: str
    runtime_id: Optional[str]
    telemetry_topic: Optional[str]
    enabled: bool


class OakdService(SubprocessService):
    type_name = "oakd"
    config_class = OakdConfig
    publishes = [
        "state", "snapshot", "latest_snapshot", "filters", "filter_catalog",
        "depth_sample", "feature_flow_sample",
    ]
    state_schema = OakdState
    topic_schemas = {
        "snapshot": SnapshotPayload,
        "latest_snapshot": SnapshotPayload,
        "filters": FiltersPayload,
        "filter_catalog": FilterCatalogPayload,
        "depth_sample": DepthSamplePayload,
        "feature_flow_sample": FeatureFlowPayload,
        "filter/{filter_id}": {"type": "object", "description": "Filter-specific telemetry (see filter_catalog[].telemetry_schema)"},
        "/filter/index/{filter_id}": FilterIndexEntry,
    }
    heartbeat_interval_s = 5.0

    # How often the auto-connect loop retries opening the device while
    # disconnected. Same value as joystick/sabertooth's own auto-connect
    # loops — a few seconds is prompt enough on a replug without
    # spamming device-open attempts (which, unlike a plain serial-port
    # open, actually boots firmware onto the Myriad X VPU) for a camera
    # that's genuinely gone.
    _AUTO_CONNECT_RETRY_S = 3.0

    def __init__(self, proxy_id: str, bus) -> None:
        super().__init__(proxy_id, bus)
        self._device: Any = None
        self._q_rgb: Any = None
        self._q_depth: Any = None
        self._q_depth_mm: Any = None
        self._q_features: Any = None
        self._q_mono: Any = None
        self._last_depth_sample_publish: float = 0.0
        self._last_feature_flow_publish: float = 0.0
        # Previous tick's tracked features, keyed by DepthAI's persistent
        # track id: {id: (u, v, depth_mm)}. Reset on every (re)connect
        # (_teardown_capture) so a reconnect never matches ids against a
        # stale, arbitrarily-old frame.
        self._prev_features: Dict[int, tuple] = {}
        self._max_disparity: float = 1.0
        self._rgb_stream: Optional[Stream] = None
        self._depth_stream: Optional[Stream] = None
        # At most one of left/right is ever live — see
        # OakdConfig.mono_stream_camera's docstring for why. Tracks
        # WHICH side _mono_stream currently represents, so a live
        # left<->right switch (not just on/off) is detected in
        # _sync_mono_stream.
        self._mono_stream: Optional[Stream] = None
        self._mono_stream_camera: Optional[str] = None
        self._capture_task: Optional[asyncio.Task[None]] = None
        self._auto_connect_task: Optional[asyncio.Task[None]] = None
        # Most recent RGB frame, cached for the snapshot action —
        # same reasoning as video's _latest_frame (avoid racing the
        # capture loop for a second device read).
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_frame_ts: float = 0.0
        self._filters: List[Filter] = []
        self._filters_signature: tuple = ()
        self._filter_index_ids: set = set()
        self._state: dict = {
            "connected": False,
            "mxid": None,
            "device_name": None,
            "usb_speed": None,
            "has_imu": None,
            "rgb_resolution": None,
            "depth_enabled": False,
            "mono_stream_camera": None,
            "observed_fps": 0.0,
            "dropped": 0,
            "error": None,
            # Set by _publish_state on every publish (not just here) —
            # see that method for why these are computed rather than
            # tracked as independent fields.
            "autoreconnect": True,
            "retrying": False,
            # fx/fy/cx/cy of the left mono camera (CAM_B), fetched once at
            # device-open time — only populated when publish_feature_flow
            # is on; consumers doing the depth-lookup→3D backprojection
            # (repo/slam/1.0.0/README.md § Visual odometry) need this once, not per-sample.
            "intrinsics": None,
        }

    # ─── lifecycle ────────────────────────────────────────────────────

    async def on_start(self) -> None:
        # Same announce-on-reconnect pattern video uses — a plain
        # retained publish only survives until the runtime restarts;
        # bus.announce re-fires on every (re)connect.
        catalog_topic = self.resolve_topic(self.topic("filter_catalog"))
        self.bus.announce(catalog_topic, lambda: FilterCatalogPayload(filters=filter_catalog()).model_dump(), retained=True)
        await self.publish("filter_catalog", FilterCatalogPayload(filters=filter_catalog()).model_dump(), retained=True)

        filters_topic = self.resolve_topic(self.topic("filters"))
        self.bus.announce(
            filters_topic,
            lambda: FiltersPayload(filters=self._filters_announcement()).model_dump(exclude_unset=True),
            retained=True,
        )

        self._rebuild_filters(getattr(self.config, "filters", []) or [])
        await self._publish_filters()

        # Register streams BEFORE opening the device so the discovery
        # message lands even if the device open is slow/fails.
        self._rgb_stream = await self.register_stream(
            stream_id=f"oakd/{self.proxy_id}/rgb", kinds=["mjpeg"], format="jpeg",
        )
        if self.config.depth_enabled:
            self._depth_stream = await self.register_stream(
                stream_id=f"oakd/{self.proxy_id}/depth", kinds=["mjpeg"], format="jpeg",
            )
            await self._sync_mono_stream()
        if self._rgb_stream is None:
            logger.warning("%s: rgb stream registration failed — no runtime env", self.proxy_id)

        try:
            await self._open_device()
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s: initial device open failed", self.proxy_id)
            self._state["error"] = self._friendly_error(exc)
            await self._publish_state()

        # Started regardless of whether the attempt above succeeded —
        # the loop itself no-ops while connected, and picks up both "was
        # never found at startup" and "was found, then unplugged mid-
        # session" (the capture loop's crash handler sets connected =
        # False, which is all this loop watches for).
        if self.config.autoreconnect:
            self._start_auto_connect_loop()

    async def on_stop(self) -> None:
        self._stop_auto_connect_loop()
        await self._teardown_capture()
        if self._rgb_stream is not None:
            await self._rgb_stream.close()
            self._rgb_stream = None
        if self._depth_stream is not None:
            await self._depth_stream.close()
            self._depth_stream = None
        if self._mono_stream is not None:
            await self._mono_stream.close()
            self._mono_stream = None
            self._mono_stream_camera = None

    async def _sync_mono_stream(self) -> None:
        """Register or close the single active mono-camera bus stream to
        match mono_stream_camera. Called at startup AND whenever
        set_mono_stream changes it live — unlike the DepthAI pipeline
        graph itself, register_stream isn't redone by a plain device
        reconnect, so a live change needs this too, not just
        _open_device(). Also handles a straight left<->right SWITCH (not
        just on/off) by closing the old stream and opening the new one
        under its own topic — see OakdConfig.mono_stream_camera's
        docstring for why only one side is ever live at once."""
        want = self.config.mono_stream_camera if self.config.depth_enabled else None
        if want == self._mono_stream_camera:
            return
        if self._mono_stream is not None:
            await self._mono_stream.close()
            self._mono_stream = None
        self._mono_stream_camera = want
        if want in ("left", "right"):
            self._mono_stream = await self.register_stream(
                stream_id=f"oakd/{self.proxy_id}/mono_{want}", kinds=["mjpeg"], format="jpeg",
            )

    # ─── device / pipeline ─────────────────────────────────────────────

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        """DepthAI's own exceptions are often terse USB/XLink errors.
        Point permission-shaped failures at the documented fix instead
        of leaving the operator to guess."""
        msg = str(exc)
        lowered = msg.lower()
        # DepthAI's actual exception text for the missing-udev-rule case
        # is the generic "Failed to boot device!" — the specific
        # "Insufficient permissions ... udev rules" detail only goes to
        # its own C++-side stderr warning, not the Python exception. So
        # "boot"/"unbooted" have to be in this list too, not just the
        # more obviously permission-shaped strings — confirmed against
        # a real device with no udev rule installed (RuntimeError:
        # 'Failed to boot device!', while dai's own stderr warning said
        # 'Insufficient permissions ... Make sure udev rules are set').
        if any(s in lowered for s in (
            "permission", "no available device", "x_link", "boot", "unbooted",
        )):
            return (
                f"{msg} — if the device is plugged in but won't open, this is almost "
                f"always the missing udev rule (see 'OAK-D USB permissions' in AGENTS.md), "
                f"not a botgear bug. Also confirm it's on a USB3 port/cable."
            )
        return msg

    def _build_pipeline(self, dai: Any) -> Any:
        """Construct the DepthAI pipeline graph. Split out from
        ``_open_device`` so the graph-building logic (which is what's
        most likely to need adjustment against real hardware/SDK
        version drift) is isolated from the device-open/queue-wiring
        plumbing around it."""
        pipeline = dai.Pipeline()

        cam_rgb = pipeline.create(dai.node.ColorCamera)
        cam_rgb.setBoardSocket(dai.CameraBoardSocket.CAM_A)
        res_name = _RESOLUTION_MAP.get(self.config.rgb_resolution, "THE_1080_P")
        cam_rgb.setResolution(getattr(dai.ColorCameraProperties.SensorResolution, res_name))
        cam_rgb.setInterleaved(False)
        cam_rgb.setFps(float(self.config.rgb_fps or 30.0))

        xout_rgb = pipeline.create(dai.node.XLinkOut)
        xout_rgb.setStreamName("rgb")
        cam_rgb.video.link(xout_rgb.input)

        if self.config.depth_enabled:
            mono_left = pipeline.create(dai.node.MonoCamera)
            mono_left.setBoardSocket(dai.CameraBoardSocket.CAM_B)
            mono_left.setResolution(dai.MonoCameraProperties.SensorResolution.THE_480_P)

            mono_right = pipeline.create(dai.node.MonoCamera)
            mono_right.setBoardSocket(dai.CameraBoardSocket.CAM_C)
            mono_right.setResolution(dai.MonoCameraProperties.SensorResolution.THE_480_P)

            if self.config.mono_stream_camera in ("left", "right"):
                # Raw per-camera preview, untouched by stereo matching —
                # for visually confirming that camera's own view isn't
                # physically occluded (a mount bracket, cable, etc.),
                # something the fused colorized depth/disparity preview
                # can't show on its own. `.out` fans out to multiple
                # consumers with no conflict (same precedent as
                # feature_tracker below sharing mono_left.out with
                # `stereo`) — this doesn't touch the depth pipeline.
                # ONE fixed XLinkOut name regardless of side — at most
                # one is ever built per pipeline (see
                # OakdConfig.mono_stream_camera's docstring for why),
                # so _open_device's queue-open doesn't need two branches.
                mono_node = mono_left if self.config.mono_stream_camera == "left" else mono_right
                xout_mono = pipeline.create(dai.node.XLinkOut)
                xout_mono.setStreamName("mono")
                mono_node.out.link(xout_mono.input)

            stereo = pipeline.create(dai.node.StereoDepth)
            stereo.setDefaultProfilePreset(dai.node.StereoDepth.PresetMode.DEFAULT)
            stereo.setLeftRightCheck(True)
            mono_left.out.link(stereo.left)
            mono_right.out.link(stereo.right)

            if self.config.depth_align_to_rgb:
                # Re-defines BOTH `.depth` and `.disparity` below (they're
                # the same underlying map, just different units) in the
                # RGB camera's own frame instead of mono_left's rectified
                # one — see OakdConfig.depth_align_to_rgb's docstring for
                # why (slam's true-color point cloud) and why the output
                # size is bounded rather than left at RGB's native
                # resolution. _validate_config (called before this method,
                # in _open_device) already rejected the publish_feature_flow
                # combination, so no guard needed here.
                stereo.setDepthAlign(dai.CameraBoardSocket.CAM_A)
                stereo.setOutputSize(_DEPTH_ALIGN_OUTPUT_W, _DEPTH_ALIGN_OUTPUT_H)

            xout_depth = pipeline.create(dai.node.XLinkOut)
            xout_depth.setStreamName("depth")
            stereo.disparity.link(xout_depth.input)
            self._max_disparity = float(stereo.initialConfig.getMaxDisparity() or 1.0)

            if self.config.publish_depth_samples or self.config.publish_feature_flow:
                # Separate XLinkOut for StereoDepth's OTHER output —
                # `.depth`, already metric millimeters (computed
                # on-device from the stereo calibration intrinsics),
                # as opposed to `.disparity` above which is relative
                # units only meaningful for the colorized preview. See
                # repo/slam/1.0.0/README.md § Ownership split for why raw disparity alone
                # can't be converted to real range downstream. Shared by
                # both opt-in flags — feature-flow matches (§9.4) need
                # per-point metric depth lookups too, no reason to build
                # this on-device output twice.
                xout_depth_mm = pipeline.create(dai.node.XLinkOut)
                xout_depth_mm.setStreamName("depth_mm")
                stereo.depth.link(xout_depth_mm.input)

            if self.config.publish_feature_flow:
                # Attach to mono_left (CAM_B), NOT the RGB camera —
                # `.depth` above is defined in mono_left's rectified
                # frame as long as depth_align_to_rgb is off (the normal
                # case for this flag — _validate_config rejects the two
                # together, see depth_align_to_rgb's docstring), so a
                # tracked pixel's depth is a direct array lookup with no
                # alignment step to add. See repo/slam/1.0.0/README.md § Visual odometry
                # FeatureTracker runs
                # on the Myriad X VPU (numShaves/numMemorySlices), not
                # the host CPU — mono_left.out fans out to both `stereo`
                # above and this node, DepthAI allows multiple consumers
                # of one output.
                feature_tracker = pipeline.create(dai.node.FeatureTracker)
                mono_left.out.link(feature_tracker.inputImage)

                xout_features = pipeline.create(dai.node.XLinkOut)
                xout_features.setStreamName("features")
                feature_tracker.outputFeatures.link(xout_features.input)

        return pipeline

    def _validate_config(self) -> None:
        """Config-combination checks that need to fail loudly (device
        open aborts, /state.error surfaces the message) rather than
        silently picking one flag over the other or building a pipeline
        that quietly breaks a downstream consumer's assumptions."""
        if self.config.depth_align_to_rgb and self.config.publish_feature_flow:
            raise ValueError(
                "depth_align_to_rgb and publish_feature_flow can't both be on: "
                "feature_flow's per-pixel depth lookup assumes depth stays in "
                "mono_left's native frame (repo/slam/1.0.0/README.md § Visual odometry), which "
                "depth_align_to_rgb changes to the RGB camera's frame instead. "
                "Pick one."
            )

    async def _open_device(self) -> None:
        await self._teardown_capture()
        self._validate_config()

        import depthai as dai  # lazy — see the discord/slack precedent

        def _open() -> Any:
            pipeline = self._build_pipeline(dai)
            if self.config.device_mxid:
                device_info = dai.DeviceInfo(self.config.device_mxid)
                return dai.Device(pipeline, device_info)
            return dai.Device(pipeline)

        device = await asyncio.to_thread(_open)
        self._device = device
        self._q_rgb = device.getOutputQueue(name="rgb", maxSize=4, blocking=False)
        if self.config.depth_enabled:
            self._q_depth = device.getOutputQueue(name="depth", maxSize=4, blocking=False)
            if self.config.publish_depth_samples or self.config.publish_feature_flow:
                self._q_depth_mm = device.getOutputQueue(name="depth_mm", maxSize=4, blocking=False)
            if self.config.publish_feature_flow:
                self._q_features = device.getOutputQueue(name="features", maxSize=4, blocking=False)
                self._prev_features = {}
            if self.config.mono_stream_camera in ("left", "right"):
                self._q_mono = device.getOutputQueue(name="mono", maxSize=4, blocking=False)

        # Best-effort introspection — wrapped individually so an SDK
        # version that renamed/removed one of these methods degrades to
        # "unknown" for that one field instead of failing device open
        # entirely (this is exactly the kind of SDK drift-risk this type
        # is exposed to; none of these are load-bearing for actually
        # streaming frames).
        mxid = self._safe_call(lambda: device.getMxId())
        device_name = self._safe_call(lambda: device.getDeviceName())
        usb_speed = self._safe_call(lambda: device.getUsbSpeed().name)
        connected_imu = self._safe_call(lambda: device.getConnectedIMU())
        # Confirmed against a real OAK-D Lite (no IMU on this unit):
        # getConnectedIMU() returns an EMPTY STRING for "none", not the
        # string "NONE" — this type's earlier IMU ambiguity note
        # turned out to be hardware-model-dependent, not an SDK-return-
        # shape mystery. None (the safe-call failure sentinel) still
        # means "couldn't ask", kept distinct from "asked, got nothing".
        if connected_imu is None:
            has_imu = None
        else:
            imu_name = str(connected_imu).strip()
            has_imu = bool(imu_name) and imu_name.upper() != "NONE"

        intrinsics = None
        if self.config.publish_feature_flow:
            # Fetched once here (not per-sample) — a visual-odometry
            # consumer (repo/slam/1.0.0/README.md § Visual odometry) needs these to
            # back-project matched pixels into 3D, but they're fixed
            # for the life of this device-open (factory calibration, not
            # something that changes frame to frame). 640x480 matches
            # mono_left's THE_480_P resolution set in _build_pipeline —
            # explicit, not relying on the calibration's own default
            # resolution happening to match.
            def _get_intrinsics() -> Dict[str, float]:
                calib = device.readCalibration()
                m = calib.getCameraIntrinsics(dai.CameraBoardSocket.CAM_B, 640, 480)
                return {"fx": m[0][0], "fy": m[1][1], "cx": m[0][2], "cy": m[1][2]}

            intrinsics = self._safe_call(_get_intrinsics)
            if intrinsics is None:
                logger.warning(
                    "%s: publish_feature_flow is on but camera intrinsics "
                    "could not be read — feature_flow_sample will still "
                    "publish pixel coordinates, but a consumer can't "
                    "back-project them into 3D without intrinsics",
                    self.proxy_id,
                )

        if mxid and self.config.device_mxid != mxid:
            # Lock onto this exact physical unit's serial for future
            # opens — see OakdConfig.device_mxid's docstring. Without
            # this, a bare `dai.Device(pipeline)` (device_mxid still
            # None) picks "whichever OAK-D answers first" every time,
            # which is fine for a single-camera host but means the
            # autoreconnect loop above could silently latch onto a
            # DIFFERENT device if one is ever plugged in alongside this
            # one. Best-effort: a failure here just means the next open
            # falls back to "any device" again, not a fatal error.
            try:
                await self.update_config({"device_mxid": mxid})
            except Exception:  # noqa: BLE001
                logger.exception("%s: failed to persist discovered device_mxid", self.proxy_id)

        self._state.update({
            "connected": True,
            "mxid": mxid,
            "device_name": device_name,
            "usb_speed": usb_speed,
            "has_imu": has_imu,
            "rgb_resolution": self.config.rgb_resolution,
            "depth_enabled": bool(self.config.depth_enabled),
            "mono_stream_camera": self.config.mono_stream_camera if self.config.depth_enabled else None,
            "intrinsics": intrinsics,
            "error": None,
        })
        await self._publish_state()

        if usb_speed and usb_speed not in ("SUPER", "SUPER_PLUS"):
            logger.warning(
                "%s: USB speed reported as %s, not SUPER — expect reduced bandwidth/reliability "
                "(OAK-D wants USB3; check the cable/port)", self.proxy_id, usb_speed,
            )

        self._capture_task = asyncio.create_task(
            self._capture_loop(), name=f"oakd.capture:{self.proxy_id}"
        )

    @staticmethod
    def _safe_call(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return None

    async def _teardown_capture(self) -> None:
        if self._capture_task is not None:
            self._capture_task.cancel()
            try:
                await self._capture_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._capture_task = None
        if self._device is not None:
            try:
                await asyncio.to_thread(self._device.close)
            except Exception:  # noqa: BLE001
                logger.exception("%s: device.close raised", self.proxy_id)
            self._device = None
        self._q_rgb = None
        self._q_depth = None
        self._q_depth_mm = None
        self._q_features = None
        self._q_mono = None
        self._prev_features = {}

    # ─── auto-connect ───────────────────────────────────────────────────

    def _start_auto_connect_loop(self) -> None:
        """(Re)arm the background retry loop. Idempotent — a second call
        while one is already running is a no-op, same contract as
        joystick/sabertooth's own version of this method."""
        if self._auto_connect_task is not None and not self._auto_connect_task.done():
            return
        self._auto_connect_task = asyncio.create_task(
            self._auto_connect_loop(), name=f"oakd.autoconnect:{self.proxy_id}"
        )

    def _stop_auto_connect_loop(self) -> None:
        if self._auto_connect_task is not None and not self._auto_connect_task.done():
            self._auto_connect_task.cancel()
        self._auto_connect_task = None

    async def _auto_connect_loop(self) -> None:
        """Runs for as long as ``autoreconnect`` stays True: whenever
        not connected, retries ``_open_device`` every
        ``_AUTO_CONNECT_RETRY_S``. Covers both "never found the device
        at startup" and "found it, then it was unplugged mid-session" —
        the capture loop's own crash handler already flips
        ``_state['connected']`` to False on a live disconnect, so this
        loop only needs to watch that one flag, not duplicate any
        USB-hotplug detection itself.

        Device identity across a replug (possibly into a different USB
        port/cable) is handled by DepthAI itself, not this loop: once
        ``device_mxid`` is set (either by the operator, or auto-locked
        onto the first device this instance ever opened — see
        ``_open_device``), every attempt here targets that specific
        MxId/serial rather than "any device", so a reconnect can't
        silently latch onto a second, different OAK-D that happens to be
        plugged in at the same time.
        """
        try:
            while self.config.autoreconnect:
                if not self._state.get("connected"):
                    try:
                        await self._open_device()
                    except Exception as exc:  # noqa: BLE001
                        self._state.update({"connected": False, "error": self._friendly_error(exc)})
                        await self._publish_state()
                await asyncio.sleep(self._AUTO_CONNECT_RETRY_S)
        except asyncio.CancelledError:
            pass

    # ─── capture pipeline ─────────────────────────────────────────────

    async def _capture_loop(self) -> None:
        """Block on the RGB queue (paced by the device's own fps) each
        iteration, then opportunistically grab depth non-blocking —
        depth and RGB packets don't arrive in perfect lockstep, and
        blocking on both would let a stalled depth pipeline (e.g.
        depth_enabled toggled off mid-run) starve the RGB stream too."""
        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), int(self.config.jpeg_quality)]
        last_state_publish = time.monotonic()
        frame_count = 0
        try:
            while not self.is_stopping():
                if self._q_rgb is None:
                    return
                rgb_pkt = await asyncio.to_thread(self._q_rgb.get)
                frame = rgb_pkt.getCvFrame()

                self._latest_frame = frame
                self._latest_frame_ts = time.time()

                for f in self._filters:
                    try:
                        frame = f.process(frame)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "%s: filter %s/%s.process raised — skipping",
                            self.proxy_id, f.type_name, f.id,
                        )
                for f in self._filters:
                    if not f.publishes_telemetry:
                        continue
                    try:
                        tel = f.telemetry()
                    except Exception:  # noqa: BLE001
                        logger.exception("%s: filter %s/%s.telemetry raised", self.proxy_id, f.type_name, f.id)
                        continue
                    if tel is not None:
                        await self.publish(f"filter/{f.id}", tel, retained=True)

                ok, buf = cv2.imencode(".jpg", frame, encode_param)
                if ok and self._rgb_stream is not None:
                    self._rgb_stream.push(buf.tobytes())

                if self._q_depth is not None and self._depth_stream is not None:
                    depth_pkt = self._q_depth.tryGet()
                    if depth_pkt is not None:
                        disparity = depth_pkt.getFrame()
                        colorized = self._colorize_depth(disparity)
                        ok2, dbuf = cv2.imencode(".jpg", colorized, encode_param)
                        if ok2:
                            self._depth_stream.push(dbuf.tobytes())

                if self._q_mono is not None and self._mono_stream is not None:
                    mono_pkt = self._q_mono.tryGet()
                    if mono_pkt is not None:
                        ok3, mbuf = cv2.imencode(".jpg", mono_pkt.getCvFrame(), encode_param)
                        if ok3:
                            self._mono_stream.push(mbuf.tobytes())

                if self._q_depth_mm is not None:
                    # depth_sample and feature_flow are two INDEPENDENT
                    # opt-in consumers of the SAME on-device depth_mm
                    # XLinkOut (_build_pipeline shares one xout between
                    # them). Fetch at most once per tick and hand the
                    # same packet to whichever consumer(s) are due —
                    # calling tryGet() twice here would silently split
                    # the incoming packet stream between them instead of
                    # each seeing every frame it's due to sample from.
                    now_mono = time.monotonic()
                    depth_due = (
                        self.config.publish_depth_samples
                        and now_mono - self._last_depth_sample_publish
                        >= 1.0 / max(0.1, self.config.depth_sample_rate_hz)
                    )
                    flow_due = (
                        self._q_features is not None
                        and now_mono - self._last_feature_flow_publish
                        >= 1.0 / max(0.1, self.config.feature_flow_rate_hz)
                    )
                    if depth_due or flow_due:
                        depth_mm_pkt = self._q_depth_mm.tryGet()
                        if depth_mm_pkt is not None:
                            depth_mm_frame = depth_mm_pkt.getFrame()
                            if depth_due:
                                # `frame` is THIS tick's just-captured RGB
                                # frame (top of the loop, above) — good
                                # enough sync for a decimated, low-rate
                                # (depth_sample_rate_hz) grid; depth_mm_frame
                                # arrived from a separate on-device queue so
                                # there's no hard guarantee they're the
                                # exact same capture instant, same caveat
                                # already implicit in RGB/depth not sharing
                                # one packet at all.
                                rgb_for_sample = frame if self.config.depth_align_to_rgb else None
                                await self._publish_depth_sample(depth_mm_frame, rgb_for_sample)
                                self._last_depth_sample_publish = now_mono
                            if flow_due:
                                features_pkt = self._q_features.tryGet()
                                if features_pkt is not None:
                                    await self._publish_feature_flow(
                                        features_pkt.trackedFeatures, depth_mm_frame,
                                    )
                                    self._last_feature_flow_publish = now_mono

                frame_count += 1
                now = time.monotonic()
                if now - last_state_publish >= 1.0:
                    self._state["observed_fps"] = round(frame_count / (now - last_state_publish), 2)
                    if self._rgb_stream is not None:
                        self._state["dropped"] = self._rgb_stream.drop_count
                    await self._publish_state()
                    frame_count = 0
                    last_state_publish = now
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("%s: capture loop crashed", self.proxy_id)
            self._state["error"] = "capture loop crashed"
            self._state["connected"] = False
            await self._publish_state()

    def _colorize_depth(self, disparity: np.ndarray) -> np.ndarray:
        """Normalize the on-device disparity map to 8-bit + apply a
        colormap so it's JPEG-streamable. Raw depth values aren't
        preserved past this point — see `publish_depth_samples` in this
        type's README.md for the metric-depth path instead."""
        scale = 255.0 / self._max_disparity if self._max_disparity else 1.0
        norm = np.clip(disparity.astype(np.float32) * scale, 0, 255).astype(np.uint8)
        cv_map = getattr(cv2, _COLORMAP_MAP.get(self.config.depth_colormap, "COLORMAP_JET"))
        return cv2.applyColorMap(norm, cv_map)

    async def _publish_depth_sample(self, depth_mm: np.ndarray, rgb_frame: Optional[np.ndarray] = None) -> None:
        """Decimate the metric depth-in-mm array to the configured
        small grid and publish as plain JSON — see repo/slam/1.0.0/README.md
        §4. See `_decimate_depth_masked` for why this ISN'T a plain
        `cv2.resize(..., INTER_AREA)` — that blends invalid (0) source
        pixels into the average as if they were real close-range
        measurements, which was fabricating exactly the "close-range
        noise at edges" this whole grid's consumers (`slam`) have had
        to filter around all session.

        ``rgb_frame`` (only passed when depth_align_to_rgb is on — see
        that field's docstring) is decimated to the SAME (cols, rows)
        grid and included as an "rgb" field, one [r, g, b] triple per
        cell in the same row-major order as depth_mm. Plain
        `cv2.resize(..., INTER_AREA)` is fine here (unlike depth above)
        — there's no "0 means invalid" sentinel in a color image, every
        source pixel is a real reading, so a masked average buys
        nothing. BGR->RGB channel swap happens here so a consumer never
        has to know OpenCV's internal channel order."""
        cols = max(1, int(self.config.depth_sample_cols))
        rows = max(1, int(self.config.depth_sample_rows))
        resized = _decimate_depth_masked(depth_mm, cols, rows)
        rgb = None
        if rgb_frame is not None:
            color_grid = cv2.resize(rgb_frame, (cols, rows), interpolation=cv2.INTER_AREA)
            rgb = color_grid[:, :, ::-1].astype(np.uint8).tolist()
        payload = DepthSamplePayload(
            cols=cols, rows=rows, depth_mm=resized.astype(np.int32).tolist(), ts=time.time(), rgb=rgb,
        )
        await self.publish("depth_sample", payload.model_dump(exclude_none=True))

    async def _publish_feature_flow(self, tracked_features: List[Any], depth_mm: np.ndarray) -> None:
        """Match this tick's on-device-tracked features against the
        previous tick's by DepthAI's own persistent track id, look up
        each match's metric depth from the shared depth_mm frame, and
        publish raw pixel/depth correspondences on
        /oakd/<id>/feature_flow_sample — repo/slam/1.0.0/README.md § Visual odometry

        Frame-to-frame CORRESPONDENCE is DepthAI's job already (ids stay
        stable across frames on-device); this only filters to ids seen
        in both frames and attaches depth. The rigid-motion fit that
        turns these correspondences into a pose delta is SLAM-specific
        and deliberately NOT done here — see §9.4/§9.5 for why that
        split belongs to the consumer (`slam`), not this generic sensor
        publisher (same reasoning as `oakd` publishing raw metric depth
        for `depth_sample` rather than a pre-baked robot-frame scan)."""
        h, w = depth_mm.shape[:2]
        # feat.position is in mono_left's native pixel space (640x480,
        # THE_480_P — see _build_pipeline). depth_mm's actual resolution
        # is NOT guaranteed to match that 1:1 — confirmed on real
        # hardware (this session) that StereoDepth's default profile
        # outputs a decimated 320x240 here, not 640x480. Scale into
        # depth_mm's actual space before indexing rather than assuming
        # they're pixel-identical; publish the ORIGINAL mono-space
        # uv unscaled (matches, and the intrinsics in /state, are both
        # in mono_left's 640x480 space, consistently).
        _MONO_W, _MONO_H = 640, 480
        sx, sy = w / _MONO_W, h / _MONO_H
        current: Dict[int, tuple] = {}
        matches: List[Dict[str, Any]] = []
        for feat in tracked_features:
            u, v = feat.position.x, feat.position.y
            px, py = int(round(u * sx)), int(round(v * sy))
            if not (0 <= px < w and 0 <= py < h):
                continue
            d = float(depth_mm[py, px])
            current[feat.id] = (u, v, d)
            prev = self._prev_features.get(feat.id)
            if prev is not None and prev[2] > 0 and d > 0:
                # Both endpoints need a valid (non-zero) depth reading —
                # StereoDepth reports 0 for "no depth computed here"
                # (occlusion, out of range, low-texture), and a match
                # with an invalid depth on either end can't be
                # back-projected into 3D at all.
                matches.append({
                    "id": feat.id,
                    "prev_uv": [prev[0], prev[1]],
                    "curr_uv": [u, v],
                    "prev_depth_mm": prev[2],
                    "curr_depth_mm": d,
                })
        self._prev_features = current
        await self.publish("feature_flow_sample", FeatureFlowPayload(ts=time.time(), matches=matches).model_dump())

    # ─── state publish ────────────────────────────────────────────────

    async def _publish_state(self) -> None:
        # Computed here (not tracked as independent fields elsewhere) so
        # every call site — connect, disconnect, the auto-connect loop,
        # a live-disconnect crash — reports them correctly without each
        # one having to remember to set them.
        self._state["autoreconnect"] = bool(self.config.autoreconnect)
        self._state["retrying"] = bool(self.config.autoreconnect) and not self._state.get("connected")
        await self.publish("state", OakdState(**self._state).model_dump(), retained=True)

    # ─── control actions ──────────────────────────────────────────────

    @service_method("connect")
    async def m_connect(self) -> None:
        """(Re)open the device. Also re-enables autoreconnect if a
        previous explicit `disconnect` had turned it off, and (re)arms
        the background retry loop — mirrors joystick's attach-implies-
        autoreconnect behavior."""
        if not self.config.autoreconnect:
            await self.update_config({"autoreconnect": True})
        self._start_auto_connect_loop()
        try:
            await self._open_device()
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s: connect failed", self.proxy_id)
            self._state.update({"connected": False, "error": self._friendly_error(exc)})
            await self._publish_state()

    @service_method("disconnect")
    async def m_disconnect(self) -> None:
        """Release the device without exiting the service. Also turns
        autoreconnect off — an explicit disconnect means "stay
        disconnected" until `connect` is called again, same contract as
        joystick's `detach`. A live unplug does NOT go through this
        method (it's caught by the capture loop's crash handler
        instead), so it never touches autoreconnect."""
        self._stop_auto_connect_loop()
        await self.update_config({"autoreconnect": False})
        await self._teardown_capture()
        self._latest_frame = None
        self._latest_frame_ts = 0.0
        self._state.update({"connected": False, "error": None})
        await self._publish_state()

    @service_method("set_auto_connect")
    async def m_set_auto_connect(self, enabled: bool) -> None:
        """Toggle autoreconnect independent of a manual connect/
        disconnect — mirrors joystick's action of the same name."""
        enabled = bool(enabled)
        await self.update_config({"autoreconnect": enabled})
        if enabled:
            self._start_auto_connect_loop()
        else:
            self._stop_auto_connect_loop()
        await self._publish_state()

    @service_method("set_mono_stream")
    async def m_set_mono_stream(self, camera: Optional[str] = None) -> None:
        """Switch which single mono camera streams live — 'left',
        'right', or None/omitted for off. ONE AT A TIME — see
        OakdConfig.mono_stream_camera's docstring for why this robot's
        real hardware can't sustain both simultaneously. The DepthAI
        pipeline graph is fixed at device-open time, so this reconnects
        to rebuild it with the new (or no) mono XLinkOut, same as any
        other pipeline-shape config change; _sync_mono_stream
        additionally (un)registers the bus-level stream, which a plain
        reconnect doesn't touch."""
        if camera not in (None, "left", "right"):
            raise ValueError(f"camera must be 'left', 'right', or omitted/None; got {camera!r}")
        await self.update_config({"mono_stream_camera": camera})
        await self._sync_mono_stream()
        try:
            await self._open_device()
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s: reconnect after set_mono_stream failed", self.proxy_id)
            self._state.update({"connected": False, "error": self._friendly_error(exc)})
            await self._publish_state()

    @service_method("snapshot")
    async def m_snapshot(self, request_id: Optional[str] = None) -> None:
        """Publish a single JPEG-encoded RGB frame (depth snapshot isn't
        exposed in v1). Same twin-publish shape as video's snapshot:
        an event on ``/snapshot`` + a retained cache on
        ``/latest_snapshot`` for late subscribers."""
        frame = self._latest_frame
        if frame is None or not self._state.get("connected"):
            err = SnapshotPayload(request_id=request_id, ts=time.time(), error="no frame available; device not connected")
            await self.publish("snapshot", err.model_dump(exclude_none=True))
            return
        ok, buf = await asyncio.to_thread(
            cv2.imencode, ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(self.config.jpeg_quality)],
        )
        if not ok:
            err = SnapshotPayload(request_id=request_id, ts=time.time(), error="cv2.imencode failed")
            await self.publish("snapshot", err.model_dump(exclude_none=True))
            return
        payload = SnapshotPayload(
            request_id=request_id,
            ts=self._latest_frame_ts,
            resolution=[int(frame.shape[1]), int(frame.shape[0])],
            jpeg_b64=base64.b64encode(buf.tobytes()).decode("ascii"),
        ).model_dump(exclude_none=True)
        await self.publish("snapshot", payload)
        await self.publish("latest_snapshot", payload, retained=True)

    # ─── filter pipeline ───────────────────────────────────────────────
    # Near-verbatim port of video_service's pipeline plumbing — see this
    # type's README.md for why this is duplicated rather than
    # imported from a shared package.

    def _rebuild_filters(self, specs: List[Dict[str, Any]]) -> None:
        new_filters: List[Filter] = []
        for spec in specs or []:
            if not spec.get("enabled", True):
                continue
            f = build_filter(spec)
            if f is None:
                logger.warning("%s: unknown filter spec dropped: %r", self.proxy_id, spec)
                continue
            new_filters.append(f)
        self._filters = new_filters
        self._filters_signature = tuple(
            (s.get("id"), s.get("type"), bool(s.get("enabled", True)),
             tuple(sorted((s.get("params") or {}).items())))
            for s in specs or []
        )
        logger.info("%s: pipeline rebuilt — %d active filters: %s",
                    self.proxy_id, len(new_filters), [f.type_name for f in new_filters])

    def _apply_config_state(self, raw: Any) -> None:
        super()._apply_config_state(raw)
        try:
            specs = list(getattr(self.config, "filters", []) or [])
        except Exception:  # noqa: BLE001
            specs = []
        sig = tuple(
            (s.get("id"), s.get("type"), bool(s.get("enabled", True)),
             tuple(sorted((s.get("params") or {}).items())))
            for s in specs
        )
        if sig != self._filters_signature:
            self._rebuild_filters(specs)
            asyncio.create_task(self._publish_filters())

    def _filters_announcement(self) -> List[Dict[str, Any]]:
        specs = list(getattr(self.config, "filters", []) or [])
        cat_by_type = {e["type"]: e for e in filter_catalog()}
        type_root = f"/{self._type_name}/{self.proxy_id}"
        enriched: List[Dict[str, Any]] = []
        for s in specs:
            entry = dict(s)
            cat = cat_by_type.get(s.get("type") or "")
            if cat:
                entry["title"] = cat.get("title")
                publishes = bool(cat.get("publishes_telemetry"))
                entry["publishes_telemetry"] = publishes
                if publishes and s.get("id"):
                    entry["telemetry_topic"] = f"{type_root}/filter/{s['id']}"
            enriched.append(entry)
        return enriched

    async def _publish_filters(self) -> None:
        enriched = self._filters_announcement()
        await self.publish("filters", FiltersPayload(filters=enriched).model_dump(exclude_unset=True), retained=True)
        await self._reconcile_filter_index(enriched)

    async def _reconcile_filter_index(self, enriched: List[Dict[str, Any]]) -> None:
        import os
        new_ids: set = set()
        for s in enriched:
            fid = s.get("id")
            if not fid or not s.get("publishes_telemetry"):
                continue
            new_ids.add(fid)
            entry = FilterIndexEntry(
                filter_id=fid, type=s.get("type"), title=s.get("title"),
                video_proxy_id=self.proxy_id,
                runtime_id=os.environ.get("BOTGEAR_RUNTIME_ID"),
                telemetry_topic=s.get("telemetry_topic"),
                enabled=bool(s.get("enabled", True)),
            )
            await self.bus.publish(f"/filter/index/{fid}", entry.model_dump(), retained=True)
        gone = self._filter_index_ids - new_ids
        for fid in gone:
            await self.bus.publish(f"/filter/index/{fid}", None, retained=True)
        self._filter_index_ids = new_ids

    def meta_topics(self) -> Dict[str, str]:
        return {
            "filter_catalog": self.resolve_topic(self.topic("filter_catalog")),
            "filters": self.resolve_topic(self.topic("filters")),
            "snapshot": self.resolve_topic(self.topic("snapshot")),
            "latest_snapshot": self.resolve_topic(self.topic("latest_snapshot")),
            "depth_sample": self.resolve_topic(self.topic("depth_sample")),
            "feature_flow_sample": self.resolve_topic(self.topic("feature_flow_sample")),
        }

    async def _persist_filters(self, specs: List[Dict[str, Any]]) -> None:
        await self.update_config({"filters": specs})
        self._rebuild_filters(specs)
        await self._publish_filters()

    @service_method("add_filter")
    async def m_add_filter(
        self, type: str, params: Optional[Dict[str, Any]] = None,
        id: Optional[str] = None, position: Optional[int] = None,
    ) -> None:
        if not isinstance(type, str) or not type:
            return
        spec: Dict[str, Any] = {
            "id": id or uuid.uuid4().hex, "type": type, "enabled": True, "params": params or {},
        }
        if build_filter(spec) is None:
            logger.warning("%s: add_filter rejected unknown type=%r", self.proxy_id, type)
            return
        specs = list(getattr(self.config, "filters", []) or [])
        if any(s.get("id") == spec["id"] for s in specs):
            return
        if isinstance(position, int) and 0 <= position <= len(specs):
            specs.insert(position, spec)
        else:
            specs.append(spec)
        await self._persist_filters(specs)

    @service_method("remove_filter")
    async def m_remove_filter(self, id: str) -> None:
        specs = [s for s in (getattr(self.config, "filters", []) or []) if s.get("id") != id]
        await self.publish(f"filter/{id}", None, retained=True)
        await self._persist_filters(specs)

    @service_method("update_filter")
    async def m_update_filter(
        self, id: str, params: Optional[Dict[str, Any]] = None, enabled: Optional[bool] = None,
    ) -> None:
        specs = list(getattr(self.config, "filters", []) or [])
        updated = False
        for s in specs:
            if s.get("id") != id:
                continue
            if params is not None:
                cur = dict(s.get("params") or {})
                cur.update(params)
                s["params"] = cur
            if enabled is not None:
                s["enabled"] = bool(enabled)
            updated = True
            break
        if updated:
            await self._persist_filters(specs)

    @service_method("reorder_filters")
    async def m_reorder_filters(self, ids: List[str]) -> None:
        current = list(getattr(self.config, "filters", []) or [])
        by_id = {s.get("id"): s for s in current}
        if set(ids) != set(by_id.keys()):
            logger.warning("%s: reorder_filters rejected — id set mismatch", self.proxy_id)
            return
        await self._persist_filters([by_id[i] for i in ids])

    @service_method("set_filters")
    async def m_set_filters(self, filters: List[Dict[str, Any]]) -> None:
        cleaned: List[Dict[str, Any]] = []
        for s in filters or []:
            if not isinstance(s, dict):
                continue
            t = s.get("type")
            if not isinstance(t, str):
                continue
            spec = {
                "id": s.get("id") or uuid.uuid4().hex, "type": t,
                "enabled": bool(s.get("enabled", True)), "params": s.get("params") or {},
            }
            if build_filter(spec) is None:
                continue
            cleaned.append(spec)
        await self._persist_filters(cleaned)
