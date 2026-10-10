# video — OpenCV capture, filters, and the stream channel

Captures from anything `cv2.VideoCapture` accepts (camera index, file
path, RTSP URL), runs an ordered filter pipeline, and serves the result
as a browser-native MJPEG stream plus low-rate bus thumbnails.

```
type:     video
version:  1.0.0
language: python (subprocess — isolates cv2's threads and the V4L2 handle)
```

## Streams: the media plane

The bus is the coordination plane; it is the wrong pipe for media (JSON
base64 bloats frames by a third, every subscriber queue holds a copy, and
federation forwards bus traffic over the same WebSocket used for control).
So frames travel on a separate **stream channel**, while discovery and
control stay on the bus:

```
Producer (subprocess)                 Runtime (runtime/streams.py)           Consumer
WS /v1/stream/<id>/upload ── JPEG ──▶ latest frame + per-consumer wakeup ──▶ GET /v1/stream/<id>/mjpeg
                                                                              (<img src>, multipart)
also on the bus:
  /stream/index/<id>        retained discovery announcement (endpoints, kinds, resolution, fps)
  /<type>/<id>/state        the service's own state
  /<type>/<id>/frame_sample low-rate base64 JPEG thumbnail (~2 Hz) for CV/ML consumers
```

- Producers push through the runtime rather than serving HTTP themselves:
  one auth gate, no per-service ports, and consumer URLs that survive a
  subprocess restart. The runtime stores the JPEG bytes as-is; there's no
  re-encode.
- `set_frame` never blocks: a slow consumer drops frames instead of
  stalling the producer.
- `GET /v1/stream/<id>/mjpeg?token=…` — `<img>` can't send headers, so
  auth is a query token. `GET /v1/stream` (admin) lists active streams.
- Discovery is a bus message, so streams on federated peers are found the
  same way.

Any subprocess service can produce a stream (`rlx_bus.SubprocessService`):

```python
stream = await self.register_stream("cam-1", kinds=["mjpeg"], resolution=(1280, 720), fps=30)
...
stream.push(jpeg_bytes)
```

OpenCV hands out **BGR** frames; convert if a consumer expects RGB.

## Filters

`config.filters` is an ordered, linear pipeline of `{id, type, enabled,
params}`. The live pipeline is retained on `/video/<id>/filters`, together
with the catalog of available types. Built-in types (`filters/`):
`flip`, `rotate`, `grayscale`, `gaussian_blur`, `pyramid_down`,
`threshold`, `canny`, `motion`, `lk_optical_flow`, `face_detect`, `yolo`,
`box_tracker`, `overlay`. YOLO weights download on first use and are
git-ignored.

## Actions (`/video/<id>/control`)

`connect`, `disconnect`, `set_resolution`, `snapshot`, `add_filter`,
`remove_filter`, `update_filter`, `reorder_filters`, `set_filters`.
Payload shapes (`VideoState`, `SnapshotPayload`, the filter models) are
introspected into `/runtime/runtime/types/video`.

Config: `source` (default `"0"`), `width` / `height` / `fps` (requested —
`state.resolution` reports what was negotiated), `jpeg_quality` (75),
`filters`.

## Not built

- WebRTC transport (`aiortc`, signaling over the bus) for 50–150 ms latency.
- Shared-memory transport for same-host CV consumers (only if benchmarks
  demand it).
- Deliberately out of scope: GPU (cv2 CUDA), filter DAGs (linear is enough),
  recording (belongs in a separate recorder service).
