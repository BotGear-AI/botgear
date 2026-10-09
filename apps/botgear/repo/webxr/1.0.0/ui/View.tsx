// WebXR desktop control panel (ui.js) — the operator-at-the-desk face
// of the service. Shows headset session/connection status + live head/
// controller telemetry, recenter/calibration, the FEED MANAGER (assign
// camera/telemetry sources to in-headset panels), the controller→
// actuator MAPPING list, and an "open on headset" link.
//
// The immersive client is a SEPARATE bundle (xr.js) opened on the Quest
// at /r/<runtime>/xr/<proxy>. This panel never enters XR itself.
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Crosshair, Eye, Gamepad2, Globe, Plus, Trash2, Video } from 'lucide-react'
import { useWsClient, useServiceRequest } from '@rlx/ui'
import type { ServiceProxy, InboundFrame } from '@rlx/ui'

type Vec = number[]
type Component = { state: string; value: number; x: number; y: number }
type CtrlSummary = {
  ray?: { pos: Vec; quat: Vec } | null
  buttons?: Record<string, number | boolean>
  axes?: { x: number; y: number }
  components?: Record<string, Component>
} | null
type Anchor = 'world' | 'head' | 'body' | 'wrist'
type PanelCfg = {
  id: string; title?: string
  source: { kind: string; ref: string }
  placement?: Anchor
  transform: { pos: Vec; quat: Vec; width_m: number; height_m: number; scale: number }
  enabled?: boolean
}
// Mirrors the immersive client's defaultOffsetForAnchor — switching anchor
// snaps the panel to a sensible spot in that frame.
const ANCHOR_DEFAULT: Record<Anchor, { pos: Vec; quat: Vec }> = {
  head: { pos: [0, 0, -1.5], quat: [0, 0, 0, 1] },
  body: { pos: [0, -0.1, -1.4], quat: [0, 0, 0, 1] },
  wrist: { pos: [0, 0.04, -0.08], quat: [0, 0, 0, 1] },
  world: { pos: [0, 1.4, -1.5], quat: [0, 0, 0, 1] },
}
type Mapping = {
  id: string; enabled?: boolean; source: string; target: string; action: string
  args?: Record<string, unknown>; arg_key?: string | null; scale?: number; offset?: number
  // teleop transforms (pose: per-axis scale/offset/clamp; scalar: deadzone;
  // deadman: enable_source/enable_min; ema smoothing). All optional/no-op.
  scale3?: number[]; offset3?: number[]; clamp_min?: number[] | null; clamp_max?: number[] | null
  deadzone?: number; enable_source?: string; enable_min?: number; ema?: number
}
type WState = {
  enabled?: boolean; connected?: boolean
  session?: { active?: boolean; mode?: string | null; fps?: number }
  origin_mm?: Vec | null
  head?: { pos: Vec; quat: Vec } | null
  controller?: { left: CtrlSummary; right: CtrlSummary }
  panels?: PanelCfg[]
  mappings?: Mapping[]
  teleop_enabled?: boolean
}

const fmt = (v?: Vec | null, d = 0) =>
  v ? `[${v.map((n) => Number(n ?? 0).toFixed(d)).join(', ')}]` : '—'

const shortId = (id: string) => id.replace(/^xr-standard-/, '')

// Live readout of every gamepad component the headset reports for one
// controller — active ones (touched/pressed/non-zero) light up green.
function CompList({ c }: { c: CtrlSummary }) {
  const comps = c?.components ?? {}
  const ids = Object.keys(comps).sort()
  if (!ids.length) return <div className="text-[10px] text-slate-600">—</div>
  return (
    <div className="space-y-0.5 font-mono text-[10px] leading-tight">
      {ids.map((id) => {
        const k = comps[id] ?? {}
        // Partial/absent fields (e.g. a device that omits axes) must not crash
        // the panel — default every numeric to 0 and state to ''.
        const kx = k.x ?? 0, ky = k.y ?? 0, kv = k.value ?? 0, ks = k.state ?? ''
        const axes = Math.abs(kx) > 0.001 || Math.abs(ky) > 0.001 || id.includes('thumbstick')
        const active = ks !== 'default' || Math.abs(kv) > 0.02 || Math.abs(kx) > 0.02 || Math.abs(ky) > 0.02
        return (
          <div key={id} className={active ? 'text-emerald-300' : 'text-slate-600'} title={`${id} · ${ks}`}>
            {shortId(id)} {ks.charAt(0)} {kv.toFixed(2)}
            {axes ? ` (${kx.toFixed(2)},${ky.toFixed(2)})` : ''}
          </div>
        )
      })}
    </div>
  )
}

// Compact numeric input for the mapping editors.
function NumIn({ v, on, step = '0.1', w = 'w-12' }: { v: number; on: (n: number) => void; step?: string; w?: string }) {
  return (
    <input type="number" step={step} value={v}
      onChange={(e) => on(Number.isFinite(parseFloat(e.target.value)) ? parseFloat(e.target.value) : 0)}
      className={`${w} rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[10px]`} />
  )
}
// Three numeric inputs for a [x,y,z] vector field (scale3/offset3/clamp).
function Vec3In({ v, on }: { v?: number[] | null; on: (a: number[]) => void }) {
  const a = [v?.[0] ?? 0, v?.[1] ?? 0, v?.[2] ?? 0]
  return (
    <span className="inline-flex gap-0.5">
      {a.map((n, i) => (
        <NumIn key={i} v={n} w="w-11" step="1" on={(x) => { const b = [...a]; b[i] = x; on(b) }} />
      ))}
    </span>
  )
}

export default function WebXRControlView({ proxy }: { proxy: ServiceProxy }) {
  const ws = useWsClient()
  const proxyId = proxy.id ?? proxy.name ?? ''
  const [type] = (proxy.service_meta_id ?? 'webxr@1.0.0').split('@')
  const stateTopic = `/${type}/${proxyId}/state`
  const controlTopic = `/${type}/${proxyId}/control`

  const [state, setState] = useState<WState>({})
  const [err, setErr] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)   // mapping id being edited
  const [robotArmed, setRobotArmed] = useState(false)             // target's actuate_hardware

  useEffect(() => {
    const off = ws.subscribe(stateTopic, (f: InboundFrame) => {
      if (f.method === 'message') setState(f.payload as WState)
    })
    return () => off()
  }, [ws, stateTopic])

  const req = useServiceRequest<Record<string, unknown>>(controlTopic, {
    timeoutMs: 15_000, errorField: 'error', replyPrefix: `webxr-${proxyId}`,
  })
  const call = useCallback(async (action: string, payload?: Record<string, unknown>) => {
    try { await req.request(action, payload); setErr(null) }
    catch (e) { setErr(String((e as Error)?.message ?? e)) }
  }, [req])

  // The immersive route the headset opens. We derive the runtime id from
  // the current URL (/r/<runtimeId>/…). Over USB, the operator opens the
  // localhost form on the Quest (adb reverse) — shown as a hint.
  const runtimeId = useMemo(() => {
    const m = window.location.pathname.match(/^\/r\/([^/]+)/)
    return m ? m[1] : 'runtime'
  }, [])
  const xrUrl = useMemo(
    () => `${window.location.origin}/r/${runtimeId}/xr/${encodeURIComponent(proxyId)}`,
    [runtimeId, proxyId],
  )

  const [vTitle, setVTitle] = useState('camera')
  const [vUrl, setVUrl] = useState('')
  const [tTitle, setTTitle] = useState('telemetry')
  const [tTopic, setTTopic] = useState('')
  const [bProxy, setBProxy] = useState('')
  const [bView, setBView] = useState('')
  const [mapTarget, setMapTarget] = useState('robot_kinematics-1')

  const addVideo = () => {
    if (!vUrl.trim()) return
    void call('set_panel', {
      panel: {
        id: `video-${Date.now().toString(36)}`,
        title: vTitle || 'camera',
        source: { kind: 'video_mjpeg', ref: vUrl.trim() },
        transform: { pos: [0, 1.4, -1.5], quat: [0, 0, 0, 1], width_m: 1.2, height_m: 0.7, scale: 1 },
      },
    })
    setVUrl('')
  }
  const addTelemetry = () => {
    if (!tTopic.trim()) return
    void call('set_panel', {
      panel: {
        id: `telem-${Date.now().toString(36)}`,
        title: tTitle || 'telemetry',
        source: { kind: 'telemetry', ref: tTopic.trim() },
        transform: { pos: [1.4, 1.4, -1.0], quat: [0, -0.38, 0, 0.92], width_m: 0.9, height_m: 0.6, scale: 1 },
      },
    })
    setTTopic('')
  }
  // Browser feed — surface a service's web UI in a VR panel. ref is the same
  // "Open in window" dock URL (/r/<rt>/dock/<proxy>?view=…); the immersive
  // client loads it in a hidden iframe and rasterizes it to a texture.
  const addBrowser = () => {
    const pid = bProxy.trim()
    if (!pid) return
    const v = bView.trim()
    const ref = `/r/${encodeURIComponent(runtimeId)}/dock/${encodeURIComponent(pid)}`
      + (v ? `?view=${encodeURIComponent(v)}` : '')
    void call('set_panel', {
      panel: {
        id: `web-${Date.now().toString(36)}`,
        title: pid,
        source: { kind: 'browser', ref },
        transform: { pos: [-1.4, 1.4, -1.0], quat: [0, 0.38, 0, 0.92], width_m: 1.0, height_m: 0.7, scale: 1 },
      },
    })
    setBProxy('')
    setBView('')
  }
  // Teleop is now a durable server-side toggle (webxr set_teleop) — installs/
  // removes the whole InMoov mapping set + preps the robot in one action, and
  // persists. The button below calls it; the VR menu has the same toggle.
  // Auto-calibrate: recenter facing the robot, hold both arms straight forward,
  // then click. Derives per-hand reach-scale + shoulder anchor from the robot's
  // real geometry so your hands pair to the robot's hands.
  const calibrateTeleop = () => void call('calibrate_teleop', {})

  // Re-send a mapping with a patched field (edits persist via set_mapping).
  const updateMapping = (mp: Mapping, patch: Partial<Mapping>) =>
    void call('set_mapping', { mapping: { ...mp, ...patch } })

  // The mapping target is a bare proxy id; the actuator subscribes on
  // /<type>/<id>/control (same expansion the mapping engine does).
  const targetControl = (t: string) => {
    const s = t.trim()
    const typ = s.includes('/') ? s.split('/')[0] : (s.match(/^(.*)-\d+$/)?.[1] ?? s)
    return `/${typ}/${s}/control`
  }
  const targetState = (t: string) => targetControl(t).replace(/\/control$/, '/state')
  // Arm/disarm the target's PHYSICAL servo output (Phase 2). Off = model only.
  const armHardware = (on: boolean) => ws.publish(targetControl(mapTarget), { action: 'set_actuate_hardware', enabled: on })
  // Track the target's actuate_hardware so the button reflects armed state.
  useEffect(() => {
    if (!mapTarget.trim()) return
    const off = ws.subscribe(targetState(mapTarget), (f: InboundFrame) => {
      if (f.method === 'message') setRobotArmed(!!(f.payload as { actuate_hardware?: boolean })?.actuate_hardware)
    })
    return () => off()
  }, [ws, mapTarget])

  const sess = state.session ?? {}
  const dot = (ok?: boolean) => (
    <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: 8,
      background: ok ? '#34d399' : '#64748b' }} />
  )

  return (
    <div className="space-y-3 p-3 text-xs text-slate-200">
      {/* status */}
      <div className="flex items-center gap-2">
        {dot(state.connected)}
        <span className="font-medium">
          {state.connected ? `headset connected · ${sess.mode ?? 'vr'} · ${Math.round(sess.fps ?? 0)} fps`
            : 'no headset — open the view on the Quest'}
        </span>
        <button onClick={() => call('set_enabled', { enabled: !(state.enabled ?? true) })}
          className={`ml-auto rounded px-2 py-0.5 ${state.enabled ?? true
            ? 'bg-emerald-800/60 text-emerald-200' : 'border border-slate-700 text-slate-400'}`}>
          {state.enabled ?? true ? 'enabled' : 'disabled'}
        </button>
      </div>

      {/* open on headset */}
      <div className="rounded border border-slate-800 bg-slate-950/60 p-2">
        <div className="mb-1 flex items-center gap-1 text-[10px] uppercase tracking-wider text-slate-400">
          <Eye className="h-3 w-3" /> open on headset
        </div>
        <a href={xrUrl} target="_blank" rel="noreferrer"
          className="block break-all rounded bg-slate-900 px-2 py-1 text-[11px] text-sky-300 underline decoration-sky-700 hover:bg-slate-800 hover:text-sky-200">
          {xrUrl}
        </a>
        <p className="mt-1 text-[10px] text-slate-500">
          Open this on the Quest 3 Browser and press Enter VR. Dev over USB:{' '}
          <code>adb reverse tcp:5051 tcp:5051</code> first, so the headset reaches it at localhost.
        </p>
      </div>

      {/* live telemetry + calibration */}
      <div className="rounded border border-slate-800 bg-slate-950/60 p-2">
        <div className="mb-1 flex items-center gap-1 text-[10px] uppercase tracking-wider text-slate-400">
          <Crosshair className="h-3 w-3" /> telemetry (robot frame, mm)
        </div>
        <div className="grid grid-cols-[64px_1fr] gap-x-2 gap-y-0.5 font-mono text-[11px]">
          <span className="text-slate-500">head</span><span>{fmt(state.head?.pos)}</span>
          <span className="text-slate-500">L ray</span><span>{fmt(state.controller?.left?.ray?.pos)}</span>
          <span className="text-slate-500">R ray</span><span>{fmt(state.controller?.right?.ray?.pos)}</span>
          <span className="text-slate-500">R axes</span>
          <span>{state.controller?.right?.axes
            ? `x ${(state.controller.right.axes.x ?? 0).toFixed(2)} y ${(state.controller.right.axes.y ?? 0).toFixed(2)}` : '—'}</span>
          <span className="text-slate-500">origin</span><span>{fmt(state.origin_mm)}</span>
        </div>
        <div className="mt-1 flex gap-1">
          <button onClick={() => call('recenter')} disabled={!state.head}
            className="rounded bg-sky-900/60 px-2 py-0.5 text-sky-200 hover:bg-sky-800/60 disabled:opacity-40">
            Recenter
          </button>
          <button onClick={() => call('clear_origin')}
            className="rounded border border-slate-700 px-2 py-0.5 text-slate-300 hover:border-slate-500">
            Clear origin
          </button>
        </div>
      </div>

      {/* controller inputs — every button/trigger/stick on the bus */}
      <div className="rounded border border-slate-800 bg-slate-950/60 p-2">
        <div className="mb-1 flex items-center gap-1 text-[10px] uppercase tracking-wider text-slate-400">
          <Gamepad2 className="h-3 w-3" /> controller inputs
          <span className="ml-auto normal-case text-slate-600">on /controller/&#123;left,right&#125;</span>
        </div>
        <div className="grid grid-cols-2 gap-2">
          {(['left', 'right'] as const).map((side) => (
            <div key={side}>
              <div className="mb-0.5 text-[9px] uppercase tracking-wider text-slate-500">{side}</div>
              <CompList c={state.controller?.[side] ?? null} />
            </div>
          ))}
        </div>
      </div>

      {/* feed manager */}
      <div className="rounded border border-slate-800 bg-slate-950/60 p-2">
        <div className="mb-1 flex items-center gap-1 text-[10px] uppercase tracking-wider text-slate-400">
          <Video className="h-3 w-3" /> feeds ({(state.panels ?? []).length})
        </div>
        <div className="space-y-0.5">
          {(state.panels ?? []).map((p) => (
            <div key={p.id} className="flex items-center gap-1">
              <span className="rounded bg-slate-800 px-1 text-[9px] uppercase text-slate-400">
                {p.source.kind === 'telemetry' ? 'tlm' : p.source.kind === 'browser' ? 'web' : 'vid'}
              </span>
              <span className="min-w-0 flex-1 truncate font-mono text-[11px]" title={p.source.ref}>{p.title || p.source.ref}</span>
              {/* anchor — persists via set_panel; switching resets the
                  offset to a sensible default for that frame */}
              <select value={p.placement ?? 'world'}
                onChange={(e) => {
                  const a = e.target.value as Anchor
                  const off = ANCHOR_DEFAULT[a]
                  void call('set_panel', { panel: { ...p, placement: a, transform: { ...p.transform, pos: off.pos, quat: off.quat } } })
                }}
                title="Anchor frame (head = locks in front of gaze)"
                className="shrink-0 rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[10px]">
                <option value="world">world</option>
                <option value="head">head</option>
                <option value="body">body</option>
                <option value="wrist">wrist</option>
              </select>
              <button onClick={() => call('remove_panel', { id: p.id })}
                className="shrink-0 rounded p-0.5 text-slate-500 hover:bg-rose-900/40 hover:text-rose-300">
                <Trash2 className="h-3 w-3" />
              </button>
            </div>
          ))}
        </div>
        <div className="mt-1.5 space-y-1 border-t border-slate-800 pt-1.5">
          <div className="flex items-center gap-1">
            <input value={vTitle} onChange={(e) => setVTitle(e.target.value)} placeholder="title"
              className="w-20 rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[11px]" />
            <input value={vUrl} onChange={(e) => setVUrl(e.target.value)} placeholder="stream id e.g. video/video-1 (or full MJPEG url)"
              className="min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[11px]" />
            <button onClick={addVideo} title="Add video feed"
              className="rounded bg-slate-800 px-1.5 py-0.5 hover:bg-slate-700"><Plus className="h-3 w-3" /></button>
          </div>
          <div className="flex items-center gap-1">
            <input value={tTitle} onChange={(e) => setTTitle(e.target.value)} placeholder="title"
              className="w-20 rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[11px]" />
            <input value={tTopic} onChange={(e) => setTTopic(e.target.value)} placeholder="bus topic (telemetry)"
              className="min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[11px]" />
            <button onClick={addTelemetry} title="Add telemetry panel"
              className="rounded bg-slate-800 px-1.5 py-0.5 hover:bg-slate-700"><Plus className="h-3 w-3" /></button>
          </div>
          {/* browser feed — surface a service's web UI ("Open in window") */}
          <div className="flex items-center gap-1">
            {/* lucide icons don't accept `title`; wrap to keep the tooltip. */}
            <span title="Service UI in VR" className="flex shrink-0"><Globe className="h-3 w-3 text-slate-500" /></span>
            <input value={bProxy} onChange={(e) => setBProxy(e.target.value)} placeholder="service proxy id e.g. servo-1"
              className="min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[11px]" />
            <input value={bView} onChange={(e) => setBView(e.target.value)} placeholder="view (optional)"
              className="w-20 rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[11px]" />
            <button onClick={addBrowser} title="Add service UI panel (browser)"
              className="rounded bg-slate-800 px-1.5 py-0.5 hover:bg-slate-700"><Plus className="h-3 w-3" /></button>
          </div>
        </div>
      </div>

      {/* mappings */}
      <div className="rounded border border-slate-800 bg-slate-950/60 p-2">
        <div className="mb-1 flex items-center gap-1 text-[10px] uppercase tracking-wider text-slate-400">
          <Gamepad2 className="h-3 w-3" /> mappings ({(state.mappings ?? []).length})
        </div>
        <div className="space-y-0.5">
          {(state.mappings ?? []).map((m) => {
            const open = expanded === m.id
            const isPose = m.action === 'set_target'
            return (
              <div key={m.id} className="rounded border border-slate-900 bg-slate-950/40">
                <div className="flex items-center gap-1 px-1 py-0.5 font-mono text-[11px]">
                  <button onClick={() => setExpanded(open ? null : m.id)}
                    className="shrink-0 text-slate-500 hover:text-slate-300" title="edit">{open ? '▾' : '▸'}</button>
                  <span className="truncate" title={`${m.source} → ${m.target}.${m.action}`}>
                    {m.source} → {m.action}
                  </span>
                  {m.enable_source ? <span className="shrink-0 text-amber-500" title={`deadman: ${m.enable_source}`}>◉</span> : null}
                  <button onClick={() => call('remove_mapping', { id: m.id })}
                    className="ml-auto rounded p-0.5 text-slate-500 hover:bg-rose-900/40 hover:text-rose-300">
                    <Trash2 className="h-3 w-3" />
                  </button>
                </div>
                {open && (
                  <div className="space-y-1 border-t border-slate-900 px-2 py-1 text-[10px] text-slate-400">
                    {isPose ? (
                      <>
                        <label className="flex items-center gap-1">scale3 <Vec3In v={m.scale3} on={(a) => updateMapping(m, { scale3: a })} /></label>
                        <label className="flex items-center gap-1" title="shoulder/world anchor (mm) — the teleop reach centre">offset3 <Vec3In v={m.offset3} on={(a) => updateMapping(m, { offset3: a })} /></label>
                        <label className="flex items-center gap-1">clamp− <Vec3In v={m.clamp_min} on={(a) => updateMapping(m, { clamp_min: a })} /></label>
                        <label className="flex items-center gap-1">clamp+ <Vec3In v={m.clamp_max} on={(a) => updateMapping(m, { clamp_max: a })} /></label>
                      </>
                    ) : (
                      <div className="flex flex-wrap items-center gap-2">
                        <label className="flex items-center gap-1">scale <NumIn v={m.scale ?? 1} on={(x) => updateMapping(m, { scale: x })} /></label>
                        <label className="flex items-center gap-1">offset <NumIn v={m.offset ?? 0} on={(x) => updateMapping(m, { offset: x })} /></label>
                        <label className="flex items-center gap-1" title="ignore |value| below this">dead <NumIn v={m.deadzone ?? 0} step="0.01" on={(x) => updateMapping(m, { deadzone: x })} /></label>
                      </div>
                    )}
                    <div className="flex flex-wrap items-center gap-2">
                      <label className="flex items-center gap-1" title="smoothing 0..1 (higher = smoother)">ema <NumIn v={m.ema ?? 0} step="0.05" on={(x) => updateMapping(m, { ema: x })} /></label>
                      <label className="flex min-w-0 flex-1 items-center gap-1" title="deadman: only fires while this signal ≥ min">grip
                        <input value={m.enable_source ?? ''} onChange={(e) => updateMapping(m, { enable_source: e.target.value })}
                          placeholder="controller.right.buttons.grip"
                          className="min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[10px]" /></label>
                    </div>
                  </div>
                )}
              </div>
            )
          })}
        </div>
        <div className="mt-1.5 flex items-center gap-1 border-t border-slate-800 pt-1.5">
          <input value={mapTarget} onChange={(e) => setMapTarget(e.target.value)} placeholder="robot_kinematics proxy id"
            className="min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[11px]" />
          <button onClick={() => call('set_teleop', { enabled: !state.teleop_enabled, target: mapTarget })}
            title="Toggle InMoov teleop — installs/removes the mapping set + preps the robot. Stays until toggled off."
            className={`rounded px-2 py-0.5 text-[11px] ${state.teleop_enabled ? 'bg-emerald-700/70 text-emerald-100 hover:bg-emerald-600/70' : 'bg-sky-900/60 text-sky-200 hover:bg-sky-800/60'}`}>
            {state.teleop_enabled ? '● Teleop ON' : 'Teleop off'}
          </button>
          <button onClick={calibrateTeleop}
            title="Recenter first, then hold both arms STRAIGHT FORWARD and click — auto-fits reach scale + shoulder anchor so your hands pair to the robot's"
            className="rounded bg-emerald-900/60 px-2 py-0.5 text-[11px] text-emerald-200 hover:bg-emerald-800/60">
            Calibrate
          </button>
        </div>
        {/* Physical-servo arm/disarm (Phase 2). Red = the real robot WILL move. */}
        <div className="mt-1.5 flex items-center gap-2 border-t border-slate-800 pt-1.5 text-[11px]">
          <button onClick={() => armHardware(!robotArmed)}
            title="Arm physical servo output on the target — the REAL robot follows teleop"
            className={`rounded px-2 py-0.5 ${robotArmed ? 'bg-rose-800/70 text-rose-100 hover:bg-rose-700/70' : 'bg-slate-800 text-slate-300 hover:bg-slate-700'}`}>
            {robotArmed ? '● HARDWARE ARMED' : '○ arm hardware'}
          </button>
          <span className="text-[10px] text-slate-500">{robotArmed ? 'real servos move' : 'model only'} on {mapTarget || '—'}</span>
        </div>
      </div>

      {err && <div className="truncate font-mono text-[10px] text-rose-300" title={err}>{err}</div>}
    </div>
  )
}
