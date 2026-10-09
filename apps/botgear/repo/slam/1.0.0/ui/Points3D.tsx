// SLAM — rotatable 3D point-cloud viewer, alongside (not replacing) the
// top-down 2D view in View.tsx. Reported live: the flat 2D top-down
// projection of the point cloud reads as "a big blob," and a person
// asking to rotate/inspect a point cloud reasonably expects an actual
// orbit-able 3D scene, which a flat <canvas> fundamentally can't be —
// there's no camera to rotate because there's no 3D scene, just a
// projection. This is that real scene: plain three.js (imperative API,
// not @react-three/fiber — avoids the JSX-intrinsic-typing issues
// already present in this repo's other three.js consumer, webxr) with
// OrbitControls.
//
// Coordinate convention (documented once here, not re-derived per
// call): world (x_m forward, y_m left, z_m height-above-floor) maps to
// three.js (x, y-up, z) as three.x = -y_m, three.y = z_m, three.z =
// -x_m. Chosen to keep the SAME handedness the existing 2D view
// already uses (View.tsx's worldToPx: world +y/left decreases screen
// X; here world +y/left likewise decreases three.x) rather than
// picking an unrelated convention for this one view.
import { useEffect, useRef } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'

export interface Points3DPose {
  x_m: number
  y_m: number
  theta_deg: number
}

const POINT_HEIGHT_CLAMP_M = 2.0
// Same stops as View.tsx's colorForHeight (0..1 linear RGB here, not
// CSS strings) — kept as a literal copy rather than a shared import
// since the 2D canvas path wants a CSS color string per point and this
// wants a flat Float32Array written straight into a BufferAttribute;
// sharing one helper meant either allocating a string per point here
// or a array per point there, worse than the duplication once.
const STOPS: Array<[number, number, number]> = [
  [30 / 255, 64 / 255, 175 / 255],
  [56 / 255, 189 / 255, 248 / 255],
  [224 / 255, 242 / 255, 254 / 255],
]
function colorForHeight01(height_m: number, out: THREE.Color): void {
  const t = Math.max(0, Math.min(1, height_m / POINT_HEIGHT_CLAMP_M))
  const seg = t * (STOPS.length - 1)
  const i = Math.min(STOPS.length - 2, Math.floor(seg))
  const f = seg - i
  const [r0, g0, b0] = STOPS[i]
  const [r1, g1, b1] = STOPS[i + 1]
  out.setRGB(r0 + (r1 - r0) * f, g0 + (g1 - g0) * f, b0 + (b1 - b0) * f)
}

export default function Points3D({
  voxels, voxelSizeM, pose, colors: rgbColors,
}: {
  voxels: Int16Array | null
  voxelSizeM: number
  pose: Points3DPose | null
  // Per-voxel [r,g,b] uint8 triples, SAME order/count as voxels — true-
  // color mode when present and length-matched, height colormap
  // otherwise (null, still loading, or a stale array from a previous
  // /points message caught mid-transition — the length check below is
  // what actually decides, not just nullness).
  colors?: Uint8Array | null
}) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const sceneRef = useRef<THREE.Scene | null>(null)
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null)
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null)
  const controlsRef = useRef<OrbitControls | null>(null)
  const pointsRef = useRef<THREE.Points | null>(null)
  const robotRef = useRef<THREE.Group | null>(null)
  // Tracks whether OrbitControls has ever been re-targeted at the
  // robot — only auto-frame the camera ONCE (first pose/points arrival),
  // never again, so an operator's own orbit/pan/zoom isn't yanked back
  // to "follow" on every subsequent map update.
  const hasFramedRef = useRef(false)

  // ── one-time scene setup ────────────────────────────────────────────
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const scene = new THREE.Scene()
    sceneRef.current = scene

    const camera = new THREE.PerspectiveCamera(50, 1, 0.05, 200)
    camera.position.set(1.5, 1.8, 2.5)
    cameraRef.current = camera

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
    renderer.setClearColor(0x000000, 0)
    container.appendChild(renderer.domElement)
    rendererRef.current = renderer

    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.dampingFactor = 0.08
    controls.target.set(0, 0.3, 0)
    controls.update()
    controlsRef.current = controls

    // Floor reference grid — same rough footprint as map_size_m's
    // default (6m); not tied to the live config since this view has
    // no direct access to it, and an approximate reference grid is all
    // it's for (scale/orientation cues, not a precise boundary).
    const grid = new THREE.GridHelper(6, 30, 0x334155, 0x1e293b)
    scene.add(grid)

    const ambient = new THREE.AmbientLight(0xffffff, 1.0)
    scene.add(ambient)

    // Robot marker: a small cone lying on its side, tip pointing along
    // its own -Z (matches ConeGeometry's default apex-up axis rotated
    // 90deg about X so the apex points along -Z instead) — heading is
    // then just a rotation about Y, no per-frame trig beyond that.
    const robot = new THREE.Group()
    const coneGeo = new THREE.ConeGeometry(0.08, 0.22, 12)
    coneGeo.rotateX(-Math.PI / 2)
    const cone = new THREE.Mesh(coneGeo, new THREE.MeshBasicMaterial({ color: 0x38bdf8 }))
    robot.add(cone)
    const dot = new THREE.Mesh(
      new THREE.SphereGeometry(0.05, 12, 12),
      new THREE.MeshBasicMaterial({ color: 0x38bdf8 }),
    )
    robot.add(dot)
    robot.position.set(0, 0.05, 0)
    scene.add(robot)
    robotRef.current = robot

    let raf = 0
    const animate = () => {
      raf = requestAnimationFrame(animate)
      controls.update()
      renderer.render(scene, camera)
    }
    animate()

    const ro = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (!entry) return
      const { width, height } = entry.contentRect
      if (width <= 0 || height <= 0) return
      camera.aspect = width / height
      camera.updateProjectionMatrix()
      renderer.setPixelRatio(window.devicePixelRatio || 1)
      renderer.setSize(width, height)
    })
    ro.observe(container)

    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
      controls.dispose()
      renderer.dispose()
      coneGeo.dispose()
      dot.geometry.dispose()
      pointsRef.current?.geometry.dispose()
      if (container.contains(renderer.domElement)) container.removeChild(renderer.domElement)
      sceneRef.current = null
      cameraRef.current = null
      rendererRef.current = null
      controlsRef.current = null
      pointsRef.current = null
      robotRef.current = null
    }
  }, [])

  // ── point cloud data -> BufferGeometry ──────────────────────────────
  useEffect(() => {
    const scene = sceneRef.current
    if (!scene) return

    if (pointsRef.current) {
      scene.remove(pointsRef.current)
      pointsRef.current.geometry.dispose()
      pointsRef.current = null
    }
    if (!voxels || voxels.length < 3) return

    const n = Math.floor(voxels.length / 3)
    const positions = new Float32Array(n * 3)
    const colors = new Float32Array(n * 3)
    // Only trust rgbColors when it's actually a per-voxel match for THIS
    // /points message — a stale array from the previous message (colors
    // decoded a render tick behind voxels, or /points arriving with
    // voxels_b64 but no colors_b64 this tick) would otherwise paint
    // point i with some unrelated voxel's color instead of falling back.
    const useRgb = !!rgbColors && rgbColors.length === n * 3
    const c = new THREE.Color()
    for (let i = 0; i < n; i++) {
      const x_m = voxels[i * 3] * voxelSizeM
      const y_m = voxels[i * 3 + 1] * voxelSizeM
      const z_m = voxels[i * 3 + 2] * voxelSizeM
      positions[i * 3] = -y_m
      positions[i * 3 + 1] = z_m
      positions[i * 3 + 2] = -x_m
      if (useRgb) {
        colors[i * 3] = rgbColors![i * 3] / 255
        colors[i * 3 + 1] = rgbColors![i * 3 + 1] / 255
        colors[i * 3 + 2] = rgbColors![i * 3 + 2] / 255
      } else {
        colorForHeight01(z_m, c)
        colors[i * 3] = c.r
        colors[i * 3 + 1] = c.g
        colors[i * 3 + 2] = c.b
      }
    }
    const geo = new THREE.BufferGeometry()
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3))
    geo.setAttribute('color', new THREE.BufferAttribute(colors, 3))
    const material = new THREE.PointsMaterial({
      size: Math.max(0.015, voxelSizeM),
      vertexColors: true,
      sizeAttenuation: true,
    })
    const points = new THREE.Points(geo, material)
    scene.add(points)
    pointsRef.current = points

    // Frame the camera on the point cloud's centroid ONCE, the first
    // time real data arrives — never again (see hasFramedRef's own
    // comment on the ref declaration above).
    if (!hasFramedRef.current && controlsRef.current) {
      let cx = 0, cy = 0, cz = 0
      for (let i = 0; i < n; i++) {
        cx += positions[i * 3]; cy += positions[i * 3 + 1]; cz += positions[i * 3 + 2]
      }
      cx /= n; cy /= n; cz /= n
      controlsRef.current.target.set(cx, cy, cz)
      controlsRef.current.update()
      hasFramedRef.current = true
    }
  }, [voxels, voxelSizeM, rgbColors])

  // ── robot marker position/heading ───────────────────────────────────
  useEffect(() => {
    const robot = robotRef.current
    if (!robot || !pose) return
    robot.position.set(-pose.y_m, 0.05, -pose.x_m)
    // World theta (0=+x/forward, +=CCW) -> three.js Y-axis rotation
    // under this file's x=-y, z=-x mapping: world direction
    // (cos th, sin th) becomes three (-sin th, -cos th) on the XZ
    // plane, which is exactly a rotation of -theta about +Y from the
    // cone's own -Z-pointing rest orientation.
    robot.rotation.y = -(pose.theta_deg * Math.PI) / 180
  }, [pose])

  return <div ref={containerRef} className="h-full w-full" />
}
