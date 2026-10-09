// SLAM — view_full panel for the slam service.
//
// New pattern for this codebase's service UIs: every other view centers
// on an <img> against an MJPEG endpoint (video/oakd) or a chat-style
// transcript (chat/discord/slack/google_chat). This one needs a live
// top-down 2D render instead — a <canvas> painting the occupancy grid
// (decoded from the retained /map message's base64 byte grid) plus a
// robot marker + heading arrow at the current /pose estimate.
//
// Grid byte encoding (matches slam_service.service._quantize_grid):
//   0 = unknown, 85 = free, 170 = inflated (footprint buffer), 255 = occupied.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ServiceProxy } from '@rlx/ui'
import { type InboundFrame } from '@rlx/ui'
import { useWsClient } from '@rlx/ui'
import Points3D from './Points3D'

interface SlamState {
  motor_control_connected?: boolean
  oakd_connected?: boolean
  mapping_backend?: string
  map_size_m?: number
  map_resolution_m?: number
}

interface SlamPose {
  x_m: number
  y_m: number
  theta_deg: number
  ts: number
}

interface SlamMap {
  cells: number
  resolution_m: number
  origin_cell: number
  grid_b64: string
  ts: number
}

interface SlamPoints {
  voxel_size_m: number
  count: number
  voxels_b64: string
  // Present only once oakd's depth_align_to_rgb has colored at least one
  // voxel (slam_service._publish_points omits it entirely otherwise —
  // see that function's docstring) — uint8 [r,g,b] triples, SAME order/
  // count as voxels_b64.
  colors_b64?: string
  ts: number
}

interface SlamLandmark {
  name: string
  x_m: number
  y_m: number
  // Present together, or not at all — a plain point marker without them.
  width_m?: number
  depth_m?: number
  heading_deg?: number
}

const CELL_COLOR: Record<number, [number, number, number]> = {
  0: [30, 30, 36],       // unknown / never scanned — dark neutral
  // Was slate-900 [15,23,42] — nearly indistinguishable from "unknown"
  // above (both read as "almost black" at a glance), which buried the
  // exact thing an operator most wants to see at a glance: "the floor
  // here has actually been scanned and is clear." A clear teal instead
  // of a barely-lighter dark tone makes that a real visual distinction.
  85: [13, 71, 74],      // free — SEEN and unobstructed (teal-900)
  170: [161, 98, 7],     // inflated (footprint buffer) — amber-800
  255: [190, 18, 60],    // occupied — rose-700
}

function base64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

// Matches slam_service.service._publish_points: a flat array of int16
// (vx, vy, vz) voxel-coordinate triples, native-endian (every
// realistic browser/deploy target here is little-endian, matching
// numpy's default tobytes() — same unstated assumption base64ToBytes'
// caller already makes for the grid).
function base64ToVoxels(b64: string): Int16Array {
  const bytes = base64ToBytes(b64)
  return new Int16Array(bytes.buffer, bytes.byteOffset, Math.floor(bytes.byteLength / 2))
}

// Matches slam_service.service._publish_points' colors_b64: flat uint8
// [r,g,b,r,g,b,...] triples, one per voxel, same order as voxels_b64 —
// no endianness concern (single-byte values), unlike the int16 above.
function base64ToColors(b64: string): Uint8Array {
  return base64ToBytes(b64)
}

// Height-above-floor -> color ramp for the point cloud. A separate hue
// family (blue -> pale cyan) from the grid's rose/amber/teal palette
// and the yellow landmark markers, so points read as their own layer
// at a glance rather than blending into "just more occupied cells."
// Clamped to a fixed 0..POINT_HEIGHT_CLAMP_M range (not the session's
// observed min/max) so a single stray high/low reading can't rescale
// -- and desaturate -- every other point's color tick to tick.
const POINT_HEIGHT_CLAMP_M = 2.0
const POINT_COLOR_STOPS: Array<[number, number, number]> = [
  [30, 64, 175],   // floor level — blue-700
  [56, 189, 248],  // mid height — sky-400
  [224, 242, 254], // near/above the clamp — sky-100
]
function colorForHeight(height_m: number): string {
  const t = Math.max(0, Math.min(1, height_m / POINT_HEIGHT_CLAMP_M))
  const seg = t * (POINT_COLOR_STOPS.length - 1)
  const i = Math.min(POINT_COLOR_STOPS.length - 2, Math.floor(seg))
  const f = seg - i
  const [r0, g0, b0] = POINT_COLOR_STOPS[i]
  const [r1, g1, b1] = POINT_COLOR_STOPS[i + 1]
  const r = Math.round(r0 + (r1 - r0) * f)
  const g = Math.round(g0 + (g1 - g0) * f)
  const b = Math.round(b0 + (b1 - b0) * f)
  return `rgb(${r},${g},${b})`
}

// Range-ring spacing, in meters, chosen so on-screen spacing stays
// legible at any zoom — the same "nice numbers" idea every map/GIS
// scale bar uses, so rings don't collapse into mush zoomed out or
// thin out uselessly zoomed in.
const RING_STEPS_M = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50]
const MIN_RING_SPACING_PX = 40
function pickRingStepM(pxPerMeter: number): number {
  for (const step of RING_STEPS_M) {
    if (step * pxPerMeter >= MIN_RING_SPACING_PX) return step
  }
  return RING_STEPS_M[RING_STEPS_M.length - 1]
}
function formatRingLabel(mRaw: number): string {
  const m = Math.round(mRaw * 10) / 10
  return Number.isInteger(m) ? `${m}m` : `${m.toFixed(1)}m`
}

export default function SlamFullView({ proxy }: { proxy: ServiceProxy }) {
  const wsClient = useWsClient()
  const proxyId = proxy.id ?? proxy.name ?? ''
  const stateTopic = `/slam/${proxyId}/state`
  const poseTopic = `/slam/${proxyId}/pose`
  const mapTopic = `/slam/${proxyId}/map`
  const pointsTopic = `/slam/${proxyId}/points`
  const landmarksTopic = `/slam/${proxyId}/landmarks`
  const controlTopic = `/slam/${proxyId}/control`

  const [state, setState] = useState<SlamState>({})
  const [pose, setPose] = useState<SlamPose | null>(null)
  const [mapMsg, setMapMsg] = useState<SlamMap | null>(null)
  const [pointsMsg, setPointsMsg] = useState<SlamPoints | null>(null)
  const [landmarks, setLandmarks] = useState<SlamLandmark[]>([])
  // Default on — the point cloud is the higher-fidelity layer (every
  // valid depth pixel, not just the 2D grid's one-winner-per-column)
  // and the reason it exists at all; the grid stays visible underneath
  // for its own still-useful free-space + safety-inflation context.
  const [showPoints, setShowPoints] = useState(true)
  // 3D orbit view — a genuinely separate rendering path (Points3D), not
  // a mode of the 2D canvas: a flat <canvas> has no camera to rotate.
  // Off by default so existing behavior/layout is unchanged until an
  // operator asks for it.
  const [show3D, setShow3D] = useState(false)
  // True-color point cloud — only meaningful once oakd's
  // depth_align_to_rgb has actually colored voxels (pointsMsg.colors_b64
  // present); the toggle button itself is disabled until then rather
  // than silently doing nothing. Off by default: same "don't change
  // existing behavior until asked" posture as show3D above, and the
  // height colormap remains the always-available fallback.
  const [colorMode, setColorMode] = useState<'height' | 'rgb'>('height')
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const overlayCanvasRef = useRef<HTMLCanvasElement | null>(null)
  // Measures the flex-fill map area (not the scroll box sized FROM this
  // measurement below) so observing and resizing never feed back into
  // each other.
  const mapAreaRef = useRef<HTMLDivElement | null>(null)
  const [containerSize, setContainerSize] = useState({ width: 420, height: 420 })

  useEffect(() => {
    const el = mapAreaRef.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (!entry) return
      const { width, height } = entry.contentRect
      setContainerSize({ width, height })
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  useEffect(() => {
    if (!proxyId) return
    const offState = wsClient.subscribe(stateTopic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      const p = f.payload
      if (p && typeof p === 'object') setState(p as SlamState)
    })
    const offPose = wsClient.subscribe(poseTopic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      const p = f.payload
      if (p && typeof p === 'object') setPose(p as SlamPose)
    })
    const offMap = wsClient.subscribe(mapTopic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      const p = f.payload
      if (p && typeof p === 'object') setMapMsg(p as SlamMap)
    })
    const offPoints = wsClient.subscribe(pointsTopic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      const p = f.payload
      if (p && typeof p === 'object') setPointsMsg(p as SlamPoints)
    })
    const offLandmarks = wsClient.subscribe(landmarksTopic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      const p = f.payload as { landmarks?: SlamLandmark[] } | undefined
      if (p && Array.isArray(p.landmarks)) setLandmarks(p.landmarks)
    })
    return () => { offState(); offPose(); offMap(); offPoints(); offLandmarks() }
  }, [proxyId, stateTopic, poseTopic, mapTopic, pointsTopic, landmarksTopic, wsClient])

  // Repaint the grid bitmap whenever a new map arrives. putImageData is
  // the right tool here (not per-cell fillRect calls) — a 400x400 grid
  // is 160k cells, and putImageData handles that as one bulk pixel-
  // buffer write. This canvas ONLY ever draws the raw grid bitmap: its
  // width/height are the grid's cell count (1 canvas pixel = 1 grid
  // cell) and CSS scales it up with `image-rendering: pixelated` so
  // scaling stays crisp instead of blurring a coarse map into mush.
  // Text/vectors (rings, robot marker, landmarks) live on a SEPARATE
  // overlay canvas below — mixing them onto this one meant labels got
  // rasterized at grid resolution and blown up the same blocky way as
  // the grid cells, which is right for cells but reads as garbled text.
  useEffect(() => {
    if (!mapMsg) return
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    if (canvas.width !== mapMsg.cells) canvas.width = mapMsg.cells
    if (canvas.height !== mapMsg.cells) canvas.height = mapMsg.cells

    const bytes = base64ToBytes(mapMsg.grid_b64)
    const image = ctx.createImageData(mapMsg.cells, mapMsg.cells)
    for (let i = 0; i < bytes.length && i < mapMsg.cells * mapMsg.cells; i++) {
      const [r, g, b] = CELL_COLOR[bytes[i]] ?? CELL_COLOR[0]
      image.data[i * 4] = r
      image.data[i * 4 + 1] = g
      image.data[i * 4 + 2] = b
      image.data[i * 4 + 3] = 255
    }
    ctx.putImageData(image, 0, 0)
  }, [mapMsg])

  const baseFitPx = Math.max(1, Math.floor(Math.min(containerSize.width, containerSize.height)) || 420)

  const [zoom, setZoom] = useState(1)
  // Capped at 30x (3000%), not 6x — a 400x400-cell map at 5cm/cell only
  // spans ~5.6 screen-inches at 6x, nowhere near enough room to tell two
  // nearby points apart. `image-rendering: pixelated` keeps cells crisp
  // rather than blurry at this range instead of trying to interpolate.
  //
  // Floored at 1x (fit), not below — the grid is a FIXED, non-growing
  // map_size_m square (config.py: "sized to comfortably cover... not
  // sized... growing, by design"), and "fit" already scales that whole
  // square to fill the panel. There's no larger map beyond the grid's
  // own edge for zooming out to reveal — going below 1x only shrank the
  // same fully-visible square into a smaller square pinned in the
  // panel's corner, which read as "the map got smaller" with no new
  // information, not "more of the map."
  const clampZoom = useCallback((z: number) => Math.min(30, Math.max(1, z)), [])
  const displayCssPx = baseFitPx * zoom

  // Decoded once per incoming /points message, not once per render —
  // the raw Int16Array view is cheap, but re-running base64 decode on
  // every unrelated re-render (zoom, pose) would not be.
  const voxels = useMemo(
    () => (pointsMsg ? base64ToVoxels(pointsMsg.voxels_b64) : null),
    [pointsMsg],
  )
  const colors = useMemo(
    () => (pointsMsg?.colors_b64 ? base64ToColors(pointsMsg.colors_b64) : null),
    [pointsMsg],
  )
  const rgbColorAvailable = colors != null
  // If color data disappears mid-session (oakd's depth_align_to_rgb
  // turned off, service restarted without it, reset-map wiped the
  // point cloud) fall back to height coloring rather than leaving the
  // 3D view stuck showing stale colors under a now-disabled toggle.
  useEffect(() => {
    if (!rgbColorAvailable && colorMode === 'rgb') setColorMode('height')
  }, [rgbColorAvailable, colorMode])

  // Rings, robot marker, and landmark markers/labels — drawn on a
  // separate canvas whose NATIVE resolution matches real display
  // pixels (CSS size × devicePixelRatio), not grid cells, so text
  // renders exactly as crisp as any other on-screen text at any zoom
  // or display DPI. Positions are computed directly in CSS-pixel space
  // (pxPerCell below), then the whole canvas is scaled by dpr so those
  // CSS-space coordinates land on the right native pixels.
  useEffect(() => {
    const canvas = overlayCanvasRef.current
    if (!canvas || !mapMsg) return
    const dpr = window.devicePixelRatio || 1
    const nativePx = Math.max(1, Math.round(displayCssPx * dpr))
    if (canvas.width !== nativePx) canvas.width = nativePx
    if (canvas.height !== nativePx) canvas.height = nativePx
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.clearRect(0, 0, canvas.width, canvas.height)
    ctx.save()
    ctx.scale(dpr, dpr)

    const pxPerCell = displayCssPx / mapMsg.cells
    // 90-degree-CCW-rotated map orientation, matching
    // hand_rolled.py's _world_to_cell: world +x (the robot's OWN
    // forward direction at pose (0,0,0)) maps to a SMALLER py (further
    // UP the canvas), and world +y (the robot's left) maps to a
    // SMALLER px (further LEFT) — i.e. "forward is up," the same
    // convention every other top-down robot/game map uses (confirmed
    // live, real hardware — see git history for the reflection/90°
    // bugs this fixed).
    const worldToPx = (x_m: number, y_m: number): [number, number] => {
      const cellX = mapMsg.origin_cell - y_m / mapMsg.resolution_m
      const cellY = mapMsg.origin_cell - x_m / mapMsg.resolution_m
      return [cellX * pxPerCell, cellY * pxPerCell]
    }

    // Point cloud — drawn FIRST (under the rings/robot marker/landmarks
    // below) since it's dense background context, not a foreground
    // callout. Every voxel is a real depth sample (project_depth_sample_
    // to_points keeps every valid pixel, not just the grid's one-
    // winner-per-column), top-down projected: (vx, vy) place it the
    // same way world meters place anything else here, height (vz)
    // only picks its color. Sized to its actual real-world footprint
    // (voxel_size_m in screen px) like the grid cells, not a fixed
    // marker size, so the cloud reads as "the room's real shape," not
    // a scatter of icons.
    if (showPoints && voxels && pointsMsg) {
      const voxelPx = Math.max(1, (pointsMsg.voxel_size_m / mapMsg.resolution_m) * pxPerCell)
      const half = voxelPx / 2
      for (let i = 0; i + 2 < voxels.length; i += 3) {
        const x_m = voxels[i] * pointsMsg.voxel_size_m
        const y_m = voxels[i + 1] * pointsMsg.voxel_size_m
        const z_m = voxels[i + 2] * pointsMsg.voxel_size_m
        const [px, py] = worldToPx(x_m, y_m)
        if (px < -voxelPx || py < -voxelPx || px > displayCssPx + voxelPx || py > displayCssPx + voxelPx) {
          continue  // off-canvas — skip the fillRect, not just clip it
        }
        ctx.fillStyle = colorForHeight(z_m)
        ctx.fillRect(px - half, py - half, voxelPx, voxelPx)
      }
    }

    if (pose) {
      const [px, py] = worldToPx(pose.x_m, pose.y_m)
      const heading = (pose.theta_deg * Math.PI) / 180

      // Range rings, metric, spaced to stay legible at the current
      // zoom — a fixed-size occupancy grid gives no sense of scale on
      // its own, which makes "how far is that target" hard to eyeball.
      // Capped at the farthest canvas corner from the robot, not the
      // whole map, so this doesn't waste draws on rings that can never
      // be visible.
      const pxPerMeter = pxPerCell / mapMsg.resolution_m
      const ringStepM = pickRingStepM(pxPerMeter)
      const ringStepPx = ringStepM * pxPerMeter
      const maxCornerDist = Math.max(
        Math.hypot(px, py),
        Math.hypot(displayCssPx - px, py),
        Math.hypot(px, displayCssPx - py),
        Math.hypot(displayCssPx - px, displayCssPx - py),
      )
      const ringCount = Math.floor(maxCornerDist / ringStepPx)
      ctx.strokeStyle = 'rgba(148, 163, 184, 0.3)'
      ctx.lineWidth = 1
      ctx.font = '10px monospace'
      ctx.fillStyle = 'rgba(203, 213, 225, 0.75)'
      ctx.textBaseline = 'middle'
      for (let i = 1; i <= ringCount; i++) {
        const r = i * ringStepPx
        ctx.beginPath()
        ctx.arc(px, py, r, 0, Math.PI * 2)
        ctx.stroke()
        ctx.fillText(formatRingLabel(i * ringStepM), px + 4, py - r)
      }

      // Fixed screen-space size (not scaled by zoom/cell-count) — like
      // any "you are here" map marker, it should stay a constant, easy
      // -to-spot size rather than ballooning at high zoom.
      ctx.fillStyle = '#38bdf8'
      ctx.beginPath()
      ctx.arc(px, py, 4, 0, Math.PI * 2)
      ctx.fill()
      ctx.strokeStyle = '#38bdf8'
      ctx.lineWidth = 2
      ctx.beginPath()
      ctx.moveTo(px, py)
      // A world direction (cos,sin) rotates through the SAME px/py
      // transform as the point conversion above: the x-component
      // (forward) contributes to py (up), the y-component (left)
      // contributes to px (left) — both negated, matching "minus maps
      // to smaller px/py" for both axes now.
      ctx.lineTo(px - Math.sin(heading) * 16, py - Math.cos(heading) * 16)
      ctx.stroke()
    }

    // Manually-placed reference points (e.g. a measured calibration
    // target) — same worldToPx convention as the robot marker above,
    // so they stay aligned with the grid underneath them. A landmark
    // with a known footprint (width_m + depth_m, from add_landmark)
    // draws as a rotated bounding box instead of a plain point marker.
    const markerR = 4
    ctx.font = '11px monospace'
    ctx.textBaseline = 'bottom'
    for (const lm of landmarks) {
      const [lx, ly] = worldToPx(lm.x_m, lm.y_m)

      if (lm.width_m != null && lm.depth_m != null) {
        // Corners computed in WORLD meters first (center +/- half-depth
        // along the box's own heading, +/- half-width perpendicular to
        // it), THEN converted to screen px — avoids re-deriving the
        // rotation directly in already-flipped screen space.
        const h = ((lm.heading_deg ?? 0) * Math.PI) / 180
        const cosH = Math.cos(h)
        const sinH = Math.sin(h)
        const halfD = lm.depth_m / 2
        const halfW = lm.width_m / 2
        const corners: [number, number][] = [[halfD, halfW], [halfD, -halfW], [-halfD, -halfW], [-halfD, halfW]]
          .map(([d, w]) => worldToPx(lm.x_m + d * cosH - w * sinH, lm.y_m + d * sinH + w * cosH))
        ctx.beginPath()
        ctx.moveTo(corners[0][0], corners[0][1])
        for (const [cx, cy] of corners.slice(1)) ctx.lineTo(cx, cy)
        ctx.closePath()
        ctx.fillStyle = 'rgba(250, 204, 21, 0.15)'
        ctx.fill()
        ctx.strokeStyle = '#facc15'
        ctx.lineWidth = 1.5
        ctx.stroke()
        const topY = Math.min(...corners.map((c) => c[1]))
        ctx.strokeStyle = 'rgba(0,0,0,0.75)'
        ctx.lineWidth = 2.5
        ctx.strokeText(lm.name, lx + markerR, topY)
        ctx.fillStyle = '#facc15'
        ctx.fillText(lm.name, lx + markerR, topY)
        continue
      }

      ctx.fillStyle = '#facc15'
      ctx.beginPath()
      ctx.moveTo(lx, ly - markerR)
      ctx.lineTo(lx + markerR, ly + markerR)
      ctx.lineTo(lx - markerR, ly + markerR)
      ctx.closePath()
      ctx.fill()
      ctx.strokeStyle = 'rgba(0,0,0,0.75)'
      ctx.lineWidth = 2.5
      ctx.strokeText(lm.name, lx + markerR + 3, ly - markerR)
      ctx.fillStyle = '#facc15'
      ctx.fillText(lm.name, lx + markerR + 3, ly - markerR)
    }

    ctx.restore()
  }, [mapMsg, pose, landmarks, displayCssPx, showPoints, voxels, pointsMsg])

  const serviceRunning = proxy.status === 'running' || proxy.status === 'starting'

  const onResetMap = useCallback(() => {
    wsClient.publish(controlTopic, { action: 'reset_map' })
  }, [wsClient, controlTopic])

  const [landmarkName, setLandmarkName] = useState('target-1')
  const [landmarkRangeM, setLandmarkRangeM] = useState('')
  const [landmarkBearingDeg, setLandmarkBearingDeg] = useState('0')
  const [landmarkWidthM, setLandmarkWidthM] = useState('')
  const [landmarkDepthM, setLandmarkDepthM] = useState('')
  const onAddLandmark = useCallback(() => {
    const range_m = Number(landmarkRangeM)
    const bearing_deg = Number(landmarkBearingDeg) || 0
    if (!landmarkName.trim() || !Number.isFinite(range_m) || range_m <= 0) return
    const width_m = Number(landmarkWidthM)
    const depth_m = Number(landmarkDepthM)
    const payload: Record<string, unknown> = { action: 'add_landmark', name: landmarkName.trim(), range_m, bearing_deg }
    // Both or neither — a lone dimension can't define a box (matches
    // the backend's own width_m/depth_m-together contract).
    if (Number.isFinite(width_m) && width_m > 0 && Number.isFinite(depth_m) && depth_m > 0) {
      payload.width_m = width_m
      payload.depth_m = depth_m
    }
    wsClient.publish(controlTopic, payload)
  }, [wsClient, controlTopic, landmarkName, landmarkRangeM, landmarkBearingDeg, landmarkWidthM, landmarkDepthM])

  const bothConnected = !!state.motor_control_connected && !!state.oakd_connected

  const poseLabel = useMemo(() => {
    if (!pose) return '—'
    return `x=${pose.x_m.toFixed(2)}m  y=${pose.y_m.toFixed(2)}m  θ=${pose.theta_deg.toFixed(0)}°`
  }, [pose])

  return (
    <div className="flex h-full min-w-[300px] flex-col gap-2 p-3 text-xs" onPointerDown={(e) => e.stopPropagation()}>
      <div className="shrink-0 rounded border border-slate-800 bg-slate-900/40 p-2">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
          <Field
            label="motor_control"
            value={state.motor_control_connected ? '● connected' : '○ waiting'}
            valueClass={state.motor_control_connected ? 'text-emerald-400' : 'text-amber-400'}
            hint="Odometry input — from motor_control's commanded output (no wheel encoders on this robot, see docs/TODO_SLAM.md)."
          />
          <Field
            label="oakd"
            value={state.oakd_connected ? '● connected' : '○ waiting'}
            valueClass={state.oakd_connected ? 'text-emerald-400' : 'text-amber-400'}
            hint="Depth-scan input — from oakd's depth_sample topic (must have publish_depth_samples enabled)."
          />
          <Field label="backend" value={state.mapping_backend ?? '—'} hint="Pluggable mapping backend — see docs/TODO_SLAM.md §5.2a. 'hand_rolled' has no drift correction by design." />
          <Field label="map" value={state.map_size_m != null ? `${state.map_size_m}m @ ${state.map_resolution_m}m/cell` : '—'} />
          <Field label="pose" value={poseLabel} mono />
        </div>
        {!bothConnected && (
          <div className="mt-2 rounded border border-amber-700 bg-amber-950/30 px-2 py-1 text-[10px] text-amber-300">
            waiting on both inputs — nothing to map until motor_control and oakd are both connected and configured
            (oakd_proxy_id / motor_control_proxy_id / left_channel / right_channel in this proxy's config)
          </div>
        )}
        <div className="mt-2 flex items-center gap-2">
          <button
            type="button"
            onClick={onResetMap}
            onPointerDown={(e) => e.stopPropagation()}
            disabled={!serviceRunning}
            className="nodrag nopan rounded border border-slate-700 px-2 py-1 text-[10px] text-slate-300 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
            title="Discard the accumulated map + pose and start fresh from the origin"
          >
            Reset map
          </button>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-1 border-t border-slate-800 pt-2" title="Mark a known reference point (e.g. a measured calibration target) relative to the robot's CURRENT position — 0° bearing = straight ahead, + = the robot's own left.">
          <span className="text-[9px] uppercase tracking-wider text-slate-500">add target</span>
          <input
            type="text" value={landmarkName} onChange={(e) => setLandmarkName(e.target.value)}
            onPointerDown={(e) => e.stopPropagation()}
            placeholder="name" className="nodrag nopan w-20 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono text-[10px] text-slate-200"
          />
          <input
            type="number" value={landmarkRangeM} onChange={(e) => setLandmarkRangeM(e.target.value)}
            onPointerDown={(e) => e.stopPropagation()}
            placeholder="range (m)" step={0.01} min={0}
            className="nodrag nopan w-20 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono text-[10px] text-slate-200"
          />
          <input
            type="number" value={landmarkBearingDeg} onChange={(e) => setLandmarkBearingDeg(e.target.value)}
            onPointerDown={(e) => e.stopPropagation()}
            placeholder="bearing (deg)" step={1}
            className="nodrag nopan w-24 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono text-[10px] text-slate-200"
          />
          <input
            type="number" value={landmarkWidthM} onChange={(e) => setLandmarkWidthM(e.target.value)}
            onPointerDown={(e) => e.stopPropagation()}
            placeholder="width (m)" step={0.01} min={0} title="Optional — with depth, draws a bounding box instead of a point"
            className="nodrag nopan w-20 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono text-[10px] text-slate-200"
          />
          <input
            type="number" value={landmarkDepthM} onChange={(e) => setLandmarkDepthM(e.target.value)}
            onPointerDown={(e) => e.stopPropagation()}
            placeholder="depth (m)" step={0.01} min={0} title="Optional — along the bearing ray, facing the camera"
            className="nodrag nopan w-20 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono text-[10px] text-slate-200"
          />
          <button
            type="button"
            onClick={onAddLandmark}
            onPointerDown={(e) => e.stopPropagation()}
            disabled={!serviceRunning || !landmarkName.trim() || !Number(landmarkRangeM)}
            className="nodrag nopan rounded border border-slate-700 px-2 py-1 text-[10px] text-slate-300 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Add
          </button>
          {landmarks.length > 0 && (
            <span className="ml-auto text-[9px] text-slate-500">{landmarks.map((l) => l.name).join(', ')}</span>
          )}
        </div>
      </div>

      <div className="flex min-h-0 flex-1 flex-col rounded border border-slate-800 bg-slate-950/60 p-2">
        <div className="mb-2 flex shrink-0 items-center justify-between gap-2">
          <div className="flex items-center gap-1">
            {!show3D && (
              <>
                <button
                  type="button"
                  onClick={() => setZoom((z) => clampZoom(z / 1.25))}
                  onPointerDown={(e) => e.stopPropagation()}
                  disabled={zoom <= 1}
                  className="nodrag nopan rounded border border-slate-700 px-1.5 py-0.5 font-mono text-[10px] text-slate-300 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
                  title={zoom <= 1 ? 'Already showing the full map — the grid is a fixed size, nothing more to reveal' : 'Zoom out'}
                >
                  −
                </button>
                <span className="w-9 text-center font-mono text-[10px] text-slate-400">{Math.round(zoom * 100)}%</span>
                <button
                  type="button"
                  onClick={() => setZoom((z) => clampZoom(z * 1.25))}
                  onPointerDown={(e) => e.stopPropagation()}
                  className="nodrag nopan rounded border border-slate-700 px-1.5 py-0.5 font-mono text-[10px] text-slate-300 hover:bg-slate-800"
                  title="Zoom in"
                >
                  +
                </button>
                <button
                  type="button"
                  onClick={() => setZoom(1)}
                  onPointerDown={(e) => e.stopPropagation()}
                  className="nodrag nopan rounded border border-slate-700 px-1.5 py-0.5 text-[10px] text-slate-300 hover:bg-slate-800"
                  title="Reset zoom to fit the panel"
                >
                  fit
                </button>
              </>
            )}
            {!show3D && (
              <button
                type="button"
                onClick={() => setShowPoints((v) => !v)}
                onPointerDown={(e) => e.stopPropagation()}
                className={`nodrag nopan rounded border px-1.5 py-0.5 text-[10px] ${
                  showPoints
                    ? 'border-sky-600 bg-sky-950/40 text-sky-300'
                    : 'border-slate-700 text-slate-300 hover:bg-slate-800'
                }`}
                title="Toggle the point-cloud layer — every valid depth pixel, not just the grid's one obstacle per bearing"
              >
                points{pointsMsg ? ` (${pointsMsg.count.toLocaleString()})` : ''}
              </button>
            )}
            <button
              type="button"
              onClick={() => setShow3D((v) => !v)}
              onPointerDown={(e) => e.stopPropagation()}
              className={`nodrag nopan rounded border px-1.5 py-0.5 text-[10px] ${
                show3D
                  ? 'border-sky-600 bg-sky-950/40 text-sky-300'
                  : 'border-slate-700 text-slate-300 hover:bg-slate-800'
              }`}
              title="Rotatable 3D point-cloud view — drag to orbit, scroll to zoom"
            >
              3D{show3D && pointsMsg ? ` (${pointsMsg.count.toLocaleString()})` : ''}
            </button>
            {show3D && (
              <button
                type="button"
                onClick={() => setColorMode((m) => (m === 'rgb' ? 'height' : 'rgb'))}
                onPointerDown={(e) => e.stopPropagation()}
                disabled={!rgbColorAvailable}
                className={`nodrag nopan rounded border px-1.5 py-0.5 text-[10px] ${
                  colorMode === 'rgb'
                    ? 'border-sky-600 bg-sky-950/40 text-sky-300'
                    : 'border-slate-700 text-slate-300 hover:bg-slate-800'
                } disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent`}
                title={
                  rgbColorAvailable
                    ? 'Color each point by the RGB camera pixel oakd correlated it with, instead of height'
                    : 'Needs the oakd proxy configured with depth_align_to_rgb enabled — points are currently colored by height only'
                }
              >
                RGB
              </button>
            )}
          </div>
          {!show3D && zoom > 1 && <span className="text-[9px] text-slate-600">scroll the map to pan</span>}
          {show3D && <span className="text-[9px] text-slate-600">drag to orbit, scroll to zoom</span>}
        </div>
        <div ref={mapAreaRef} className="flex min-h-0 flex-1 items-center justify-center overflow-hidden">
          {show3D ? (
            <div className="nodrag nopan h-full w-full rounded border border-slate-800" onPointerDown={(e) => e.stopPropagation()}>
              <Points3D
                voxels={voxels}
                voxelSizeM={pointsMsg?.voxel_size_m ?? 0.03}
                pose={pose}
                colors={colorMode === 'rgb' ? colors : null}
              />
            </div>
          ) : mapMsg ? (
            <div style={{ width: baseFitPx, height: baseFitPx, overflow: 'auto' }} className="rounded border border-slate-800">
              <div style={{ position: 'relative', width: displayCssPx, height: displayCssPx }}>
                <canvas
                  ref={canvasRef}
                  style={{ position: 'absolute', top: 0, left: 0, width: displayCssPx, height: displayCssPx, imageRendering: 'pixelated', display: 'block' }}
                />
                <canvas
                  ref={overlayCanvasRef}
                  style={{ position: 'absolute', top: 0, left: 0, width: displayCssPx, height: displayCssPx, pointerEvents: 'none' }}
                />
              </div>
            </div>
          ) : (
            <div className="flex h-full w-full items-center justify-center rounded border border-slate-800 text-[10px] text-slate-600">
              {serviceRunning ? 'no map yet — waiting for the first depth_sample' : 'service not running'}
            </div>
          )}
        </div>
        <Legend />
      </div>
    </div>
  )
}

function Legend() {
  return (
    <div className="mt-2 flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-t border-slate-800 pt-2 text-[9px] text-slate-400">
      <span className="uppercase tracking-wider text-slate-500">legend</span>
      <LegendItem color="#38bdf8" shape="circle" label="robot position + heading" />
      <LegendItem color="#facc15" label="manually-placed target" />
      <LegendItem color="rgb(148, 163, 184)" label="range rings, metric, spaced to stay legible" />
      <span className="flex items-center gap-1">
        <span
          className="inline-block h-2.5 w-6 rounded-[2px] border border-black/30"
          style={{ background: 'linear-gradient(90deg, rgb(30,64,175), rgb(56,189,248), rgb(224,242,254))' }}
        />
        points — every valid depth pixel, colored by height (floor to ~2m+)
      </span>
      <LegendItem color={rgb(CELL_COLOR[0])} label="unknown / unexplored" />
      <LegendItem color={rgb(CELL_COLOR[85])} label="free — seen, unobstructed" />
      <LegendItem color={rgb(CELL_COLOR[170])} label="inflated buffer (near obstacle)" />
      <LegendItem color={rgb(CELL_COLOR[255])} label="occupied (obstacle)" />
    </div>
  )
}

function rgb([r, g, b]: [number, number, number]): string {
  return `rgb(${r}, ${g}, ${b})`
}

function LegendItem({ color, label, shape = 'square' }: { color: string; label: string; shape?: 'square' | 'circle' }) {
  return (
    <span className="flex items-center gap-1">
      <span
        className={`inline-block h-2.5 w-2.5 border border-black/30 ${shape === 'circle' ? 'rounded-full' : 'rounded-[2px]'}`}
        style={{ backgroundColor: color }}
      />
      {label}
    </span>
  )
}

function Field({ label, value, mono, hint, valueClass }: { label: string; value: string; mono?: boolean; hint?: string; valueClass?: string }) {
  return (
    <div className="flex items-baseline gap-1" title={hint}>
      <span className={`text-[9px] uppercase tracking-wider text-slate-500 ${hint ? 'cursor-help' : ''}`}>{label}</span>
      <span className={`text-[10px] ${mono ? 'font-mono' : ''} ${valueClass ?? 'text-slate-300'}`}>{value}</span>
    </div>
  )
}
