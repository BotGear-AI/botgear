import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type WheelEvent as ReactWheelEvent } from 'react'
import { Play, Square, Crosshair, Trash2, Plus, X, Download, Zap, RotateCcw } from 'lucide-react'
import type { ServiceProxy, InboundFrame } from '@rlx/ui'
import { useWsClient, useApiFetch, useServiceRequest } from '@rlx/ui'

// ── oscope — an oscilloscope for the bus ────────────────────────────────
// The backend (oscope service) subscribes to each channel's (topic, field),
// evaluates the trigger, and streams delta sample-frames on /oscope/{id}/frame.
// This view keeps a per-channel ring buffer and renders traces on a <canvas>:
// analog overlaid in the main plot, digital as thin logic rows pinned at the
// bottom (hybrid layout). Controls publish actions to /oscope/{id}/control.

interface Channel {
  id: string; label: string; source_topic: string; field: string
  kind: 'analog' | 'digital'; color: string; auto: boolean; scale: number; offset: number; enabled: boolean
}
interface Trigger {
  channel_id: string | null; mode: 'auto' | 'normal' | 'single'
  edge: 'rising' | 'falling' | 'either'; level: number; holdoff_ms: number; publish: boolean
}
interface TrigEvent { channel_id: string; t: number; value: number; edge: string; direction: string; seq: number }
interface OscopeState {
  running: boolean; mode: string; timebase_ms: number; frame_hz: number; buffer_s: number
  channels: Channel[]; trigger: Trigger; last_trigger: TrigEvent | null; t_now: number
}
interface Frame { t: number; seq: number; running: boolean; ch: Record<string, [number, number][]>; trig: TrigEvent | null }
type Buf = { t: number[]; v: number[] }

const field = 'rounded border border-slate-700 bg-slate-900 px-1.5 py-0.5 text-xs text-slate-200'
const btn = 'inline-flex items-center gap-1 rounded px-2 py-1 text-xs disabled:opacity-40'
const TB_PRESETS = [200, 500, 1000, 2000, 5000, 10000, 20000]
const ANALOG_DIVS = 8   // vertical divisions in the analog region

// ── pure helpers ────────────────────────────────────────────────────────
function pruneBuf(b: Buf, cutoff: number) {
  let drop = 0
  // Keep at least the most recent sample even if it's older than the cutoff:
  // a change-only source (Firmata report mode, siggen-at-rest, an idle input)
  // may not emit for a long time, and the renderer holds its last value. If we
  // pruned to empty the trace would vanish; instead the last sample persists so
  // the level draws as a flat line indefinitely.
  while (drop < b.t.length - 1 && b.t[drop] < cutoff) drop++
  if (drop > 0) { b.t.splice(0, drop); b.v.splice(0, drop) }
}
function windowStats(b: Buf | undefined, t0: number, t1: number) {
  if (!b || !b.t.length) return null
  let min = Infinity, max = -Infinity, sum = 0, n = 0
  let crossings = 0, firstT = NaN, lastT = NaN, prev = NaN
  for (let i = 0; i < b.t.length; i++) {
    const t = b.t[i]; if (t < t0 || t > t1) continue
    const v = b.v[i]; if (!Number.isFinite(v)) continue
    if (v < min) min = v; if (v > max) max = v; sum += v; n++
    if (isNaN(firstT)) firstT = t; lastT = t
    prev = v
  }
  if (!n) return null
  const mean = sum / n
  // frequency via mean crossings (rising)
  prev = NaN
  for (let i = 0; i < b.t.length; i++) {
    const t = b.t[i]; if (t < t0 || t > t1) continue
    const v = b.v[i]
    if (!isNaN(prev) && prev < mean && v >= mean) crossings++
    prev = v
  }
  const span = (lastT - firstT) || 0
  const freq = span > 0 ? crossings / span : 0
  return { min, max, mean, pp: max - min, freq, n }
}

export default function OscopeView({ proxy }: { proxy: ServiceProxy }) {
  const ws = useWsClient()
  const apiFetch = useApiFetch()
  const proxyId = proxy.id ?? proxy.name ?? ''
  const stateTopic = `/oscope/${proxyId}/state`
  const frameTopic = `/oscope/${proxyId}/frame`
  const trigTopic = `/oscope/${proxyId}/trigger`
  const controlTopic = `/oscope/${proxyId}/control`
  const ctl = useServiceRequest<any>(controlTopic, { timeoutMs: 8000, errorField: 'error' })

  const [state, setState] = useState<OscopeState | null>(null)
  const [cursorsOn, setCursorsOn] = useState(false)
  const [picker, setPicker] = useState(false)
  const [now, setNow] = useState(Date.now() / 1000)   // ticks measurements panel

  const stateRef = useRef<OscopeState | null>(null)
  const bufs = useRef<Map<string, Buf>>(new Map())
  const lastFrame = useRef<{ t: number; clientMs: number }>({ t: Date.now() / 1000, clientMs: performance.now() })
  const lastTrig = useRef<TrigEvent | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const cursors = useRef<{ a: number; b: number; drag: 'a' | 'b' | null }>({ a: 0.35, b: 0.65, drag: null })
  // Global viewport transform (independent of per-channel scale): vertical
  // zoom around centre + vertical pan (px) + horizontal pan back through the
  // buffer (seconds). Drag pans, wheel zooms, ⟲ resets.
  const view = useRef<{ vZoom: number; vPan: number; tPan: number }>({ vZoom: 1, vPan: 0, tPan: 0 })
  const panDrag = useRef<{ x: number; y: number } | null>(null)
  const [, setViewNudge] = useState(0)   // re-render toolbar's "offset?" indicator
  const resetView = () => { view.current = { vZoom: 1, vPan: 0, tPan: 0 }; setViewNudge((n) => n + 1) }

  const send = useCallback((action: string, args: Record<string, any> = {}) => {
    ws.publish(controlTopic, { action, ...args })
  }, [ws, controlTopic])

  // ── subscriptions ──────────────────────────────────────────────────
  useEffect(() => {
    if (!proxyId) return
    const offState = ws.subscribe(stateTopic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      const s = f.payload as OscopeState
      setState(s); stateRef.current = s
      // drop buffers for removed channels
      const live = new Set((s.channels || []).map((c) => c.id))
      for (const id of [...bufs.current.keys()]) if (!live.has(id)) bufs.current.delete(id)
    })
    const offFrame = ws.subscribe(frameTopic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      const fr = f.payload as Frame
      lastFrame.current = { t: fr.t, clientMs: performance.now() }
      const bufS = stateRef.current?.buffer_s ?? 20
      for (const [cid, samples] of Object.entries(fr.ch || {})) {
        let b = bufs.current.get(cid)
        if (!b) { b = { t: [], v: [] }; bufs.current.set(cid, b) }
        for (const [t, v] of samples) { b.t.push(t); b.v.push(v) }
      }
      // Prune EVERY buffer to the window (a channel whose source stalled stops
      // appearing in fr.ch, so it must still age out here — not just the active ones).
      const cutoff = fr.t - bufS
      for (const b of bufs.current.values()) pruneBuf(b, cutoff)
      if (fr.trig) lastTrig.current = fr.trig
    })
    const offTrig = ws.subscribe(trigTopic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      lastTrig.current = f.payload as TrigEvent
    })
    return () => { offState(); offFrame(); offTrig() }
  }, [proxyId, stateTopic, frameTopic, trigTopic, ws])

  // ── render loop ────────────────────────────────────────────────────
  useEffect(() => {
    let raf = 0
    const draw = () => {
      raf = requestAnimationFrame(draw)
      const cv = canvasRef.current, s = stateRef.current
      if (!cv || !s) return
      const ctx = cv.getContext('2d'); if (!ctx) return
      const dpr = window.devicePixelRatio || 1
      const cssW = cv.clientWidth, cssH = cv.clientHeight
      if (cv.width !== Math.round(cssW * dpr) || cv.height !== Math.round(cssH * dpr)) {
        cv.width = Math.round(cssW * dpr); cv.height = Math.round(cssH * dpr)
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      drawScope(ctx, cssW, cssH, s, bufs.current, lastFrame.current, lastTrig.current, cursorsOn ? cursors.current : null, view.current)
    }
    raf = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(raf)
  }, [cursorsOn])

  // tick the measurements panel ~4Hz
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1000), 250)
    return () => clearInterval(id)
  }, [])

  // ── canvas interaction: cursors (when on) OR pan (drag) + zoom (wheel) ──
  const onCanvasDown = (e: ReactPointerEvent) => {
    if (!canvasRef.current) return
    ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
    if (cursorsOn) {
      const rect = canvasRef.current.getBoundingClientRect()
      const fx = (e.clientX - rect.left) / rect.width
      const c = cursors.current
      c.drag = Math.abs(fx - c.a) <= Math.abs(fx - c.b) ? 'a' : 'b'
      c[c.drag] = Math.min(1, Math.max(0, fx))
    } else {
      panDrag.current = { x: e.clientX, y: e.clientY }   // drag to pan the viewport
    }
  }
  const onCanvasMove = (e: ReactPointerEvent) => {
    if (!canvasRef.current) return
    if (cursorsOn) {
      const c = cursors.current
      if (!c.drag) return
      const rect = canvasRef.current.getBoundingClientRect()
      c[c.drag] = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width))
    } else if (panDrag.current) {
      const rect = canvasRef.current.getBoundingClientRect()
      const dx = e.clientX - panDrag.current.x, dy = e.clientY - panDrag.current.y
      panDrag.current = { x: e.clientX, y: e.clientY }
      view.current.vPan += dy                               // vertical pan (px)
      const tb = (stateRef.current?.timebase_ms ?? 2000) / 1000
      view.current.tPan = Math.max(0, view.current.tPan + (dx / rect.width) * tb)  // drag right → older
    }
  }
  const onCanvasUp = () => { cursors.current.drag = null; panDrag.current = null }
  const onCanvasWheel = (e: ReactWheelEvent) => {
    e.stopPropagation()                                     // don't zoom the node canvas
    const f = e.deltaY < 0 ? 1.15 : 1 / 1.15
    view.current.vZoom = Math.min(50, Math.max(0.05, view.current.vZoom * f))  // vertical zoom
  }

  const s = state
  const channels = s?.channels ?? []
  const trig = s?.trigger
  const tbSec = (s?.timebase_ms ?? 2000) / 1000
  const stats = useMemo(() => {
    const t1 = lastFrame.current.t, t0 = t1 - tbSec
    return channels.map((c) => ({ c, m: windowStats(bufs.current.get(c.id), t0, t1) }))
  }, [now, channels, tbSec])

  const exportCsv = () => {
    const t1 = lastFrame.current.t, t0 = t1 - (s?.buffer_s ?? 20)
    // union of timestamps → wide CSV
    const rows: string[] = []
    const header = ['t', ...channels.map((c) => c.label || c.id)]
    rows.push(header.join(','))
    const cols = channels.map((c) => bufs.current.get(c.id) || { t: [], v: [] })
    // Merge on a small time GRID (0.5ms) rather than float equality: samples a
    // source publishes together share a timestamp, and near-simultaneous samples
    // across sources group into one row — float === would split them by rounding.
    const GRID = 5e-4
    const byKey = new Map<number, { t: number; vals: string[] }>()
    for (let i = 0; i < cols.length; i++) {
      for (let j = 0; j < cols[i].t.length; j++) {
        const t = cols[i].t[j]; if (t < t0 || t > t1) continue
        const key = Math.round(t / GRID)
        let r = byKey.get(key)
        if (!r) { r = { t, vals: new Array(cols.length).fill('') }; byKey.set(key, r) }
        r.vals[i] = String(cols[i].v[j])
      }
    }
    for (const key of [...byKey.keys()].sort((a, b) => a - b)) {
      const r = byKey.get(key)!
      rows.push([r.t.toFixed(4), ...r.vals].join(','))
    }
    const blob = new Blob([rows.join('\n')], { type: 'text/csv' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob)
    a.download = `oscope-${proxyId}-${Math.round(t1)}.csv`; a.click(); URL.revokeObjectURL(a.href)
  }

  if (!s) return <div className="p-3 text-xs text-slate-400">Connecting to scope…</div>
  const running = s.running

  return (
    <div className="flex flex-col gap-2 p-2 text-slate-200">
      {/* toolbar */}
      <div className="flex flex-wrap items-center gap-2">
        <button className={`${btn} ${running ? 'bg-emerald-600' : 'bg-slate-700'}`} onClick={() => send('set_running', { running: !running })}>
          {running ? <Square size={13} /> : <Play size={13} />} {running ? 'Stop' : 'Run'}
        </button>
        <button className={`${btn} bg-amber-600 disabled:bg-slate-700`} disabled={!trig?.channel_id}
          title={trig?.channel_id ? 'Arm single-shot' : 'Set a trigger channel first'} onClick={() => send('arm_single')}>
          <Zap size={13} /> Single
        </button>
        <button className={`${btn} bg-slate-700`} onClick={() => send('clear')}><Trash2 size={13} /> Clear</button>
        <span className="mx-1 h-4 w-px bg-slate-700" />
        <label className="text-xs text-slate-400">Time</label>
        <select className={field} value={s.timebase_ms} onChange={(e) => send('set_timebase', { timebase_ms: Number(e.target.value) })}>
          {TB_PRESETS.map((p) => <option key={p} value={p}>{p >= 1000 ? `${p / 1000}s` : `${p}ms`}</option>)}
          {!TB_PRESETS.includes(s.timebase_ms) && <option value={s.timebase_ms}>{s.timebase_ms}ms</option>}
        </select>
        <span className="mx-1 h-4 w-px bg-slate-700" />
        <button className={`${btn} ${cursorsOn ? 'bg-sky-600' : 'bg-slate-700'}`} onClick={() => setCursorsOn((x) => !x)}>
          <Crosshair size={13} /> Cursors
        </button>
        <button className={`${btn} bg-slate-700`} onClick={exportCsv}><Download size={13} /> CSV</button>
        <button className={`${btn} bg-slate-700`} onClick={resetView}
          title="Reset view. On the plot: drag = pan, wheel = vertical zoom, drag the bottom edge to resize.">
          <RotateCcw size={13} /> Fit
        </button>
        <span className="ml-auto text-xs text-slate-400">{s.mode.toUpperCase()}{s.last_trigger ? ` · trig#${s.last_trigger.seq}` : ''}</span>
      </div>

      {/* trigger bar */}
      <div className="flex flex-wrap items-center gap-2 rounded bg-slate-900/60 px-2 py-1">
        <span className="text-xs font-medium text-slate-400">Trigger</span>
        <select className={field} value={trig?.channel_id ?? ''} onChange={(e) => send('set_trigger', { channel_id: e.target.value || null })}>
          <option value="">— off (roll) —</option>
          {channels.map((c) => <option key={c.id} value={c.id}>{c.label || c.id}</option>)}
        </select>
        <select className={field} value={trig?.edge} disabled={!trig?.channel_id} onChange={(e) => send('set_trigger', { edge: e.target.value })}>
          <option value="rising">↑ rising</option><option value="falling">↓ falling</option><option value="either">↕ either</option>
        </select>
        <label className="text-xs text-slate-400">level</label>
        <input className={`${field} w-20`} type="number" step="0.1" value={trig?.level ?? 0} disabled={!trig?.channel_id}
          onChange={(e) => send('set_trigger', { level: Number(e.target.value) })} />
        <select className={field} value={trig?.mode} disabled={!trig?.channel_id} onChange={(e) => send('set_trigger', { mode: e.target.value })}>
          <option value="auto">auto</option><option value="normal">normal</option><option value="single">single</option>
        </select>
        <label className="flex items-center gap-1 text-xs text-slate-400">
          <input type="checkbox" checked={!!trig?.publish} disabled={!trig?.channel_id} onChange={(e) => send('set_trigger', { publish: e.target.checked })} />
          publish
        </label>
      </div>

      {/* scope canvas */}
      <div ref={wrapRef} className="relative h-[340px] min-h-[180px] w-full resize-y overflow-hidden rounded border border-slate-700 bg-slate-950">
        <canvas ref={canvasRef} className="nowheel nodrag nopan h-full w-full touch-none cursor-crosshair"
          onPointerDown={onCanvasDown} onPointerMove={onCanvasMove} onPointerUp={onCanvasUp}
          onPointerLeave={onCanvasUp} onWheel={onCanvasWheel} />
      </div>

      {/* measurements */}
      {stats.some((x) => x.m) && (
        <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 rounded bg-slate-900/60 px-2 py-1 text-[11px] md:grid-cols-3">
          {stats.filter((x) => x.m).map(({ c, m }) => (
            <div key={c.id} className="flex items-center gap-1.5 truncate">
              <span className="inline-block h-2 w-2 rounded-sm" style={{ background: c.color }} />
              <span className="truncate text-slate-300">{c.label || c.id}</span>
              <span className="ml-auto tabular-nums text-slate-400">
                μ{m!.mean.toFixed(2)} pp{m!.pp.toFixed(2)} {m!.freq >= 0.05 ? `${m!.freq.toFixed(1)}Hz` : ''}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* channels */}
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-slate-400">Channels</span>
          <button className={`${btn} bg-slate-700`} onClick={() => setPicker((x) => !x)}><Plus size={13} /> Add channel</button>
        </div>
        {channels.length === 0 && <div className="text-xs text-slate-500">No channels — add one to start plotting.</div>}
        {channels.map((c) => (
          <div key={c.id} className="flex flex-wrap items-center gap-1.5 rounded bg-slate-900/60 px-2 py-1">
            <input type="color" value={c.color} className="h-5 w-6 cursor-pointer rounded bg-transparent"
              onChange={(e) => send('set_channel', { id: c.id, color: e.target.value })} />
            <input className={`${field} w-24`} value={c.label} placeholder={c.id}
              onChange={(e) => send('set_channel', { id: c.id, label: e.target.value })} />
            <span className="max-w-[210px] truncate text-[10px] text-slate-500" title={`${c.source_topic} · ${c.field}`}>{c.source_topic} · {c.field}</span>
            <select className={field} value={c.kind} onChange={(e) => send('set_channel', { id: c.id, kind: e.target.value })}>
              <option value="analog">analog</option><option value="digital">digital</option>
            </select>
            {c.kind === 'analog' && <>
              <label className="flex items-center gap-1 text-[10px] text-slate-500" title="auto-fit the trace to its range">
                <input type="checkbox" checked={c.auto} onChange={(e) => send('set_channel', { id: c.id, auto: e.target.checked })} /> auto
              </label>
              {!c.auto && <>
                <label className="text-[10px] text-slate-500">×/div</label>
                <input className={`${field} w-16`} type="number" step="0.1" value={c.scale}
                  onChange={(e) => send('set_channel', { id: c.id, scale: Number(e.target.value) || 1, auto: false })} />
                <label className="text-[10px] text-slate-500">off</label>
                <input className={`${field} w-16`} type="number" step="0.1" value={c.offset}
                  onChange={(e) => send('set_channel', { id: c.id, offset: Number(e.target.value), auto: false })} />
              </>}
            </>}
            <label className="flex items-center gap-1 text-[10px] text-slate-500">
              <input type="checkbox" checked={c.enabled} onChange={(e) => send('set_channel', { id: c.id, enabled: e.target.checked })} /> on
            </label>
            <button className="ml-auto rounded p-1 text-slate-400 hover:text-rose-300" onClick={() => send('remove_channel', { id: c.id })}><X size={13} /></button>
          </div>
        ))}
      </div>

      {picker && <SourcePicker apiFetch={apiFetch} ctl={ctl} onAdd={(a) => { send('add_channel', a); setPicker(false) }} onClose={() => setPicker(false)} />}
    </div>
  )
}

// ── source picker: discover signal_source proxies + manual entry ─────────
function SourcePicker({ apiFetch, onAdd, onClose }: {
  apiFetch: <T = any>(p: string, i?: RequestInit) => Promise<T>
  ctl: ReturnType<typeof useServiceRequest<any>>
  onAdd: (a: Record<string, any>) => void
  onClose: () => void
}) {
  const ws = useWsClient()
  const [found, setFound] = useState<{ topic: string; field: string; label: string; kind: string; src: string }[]>([])
  const [loading, setLoading] = useState(true)
  const [mTopic, setMTopic] = useState(''); const [mField, setMField] = useState('value'); const [mKind, setMKind] = useState('analog')

  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const metas = await apiFetch<any[]>('/v1/service-meta-list').catch(() => [])
        const proxies = await apiFetch<any[]>('/v1/service-proxy-list').catch(() => [])
        const types = new Set((Array.isArray(metas) ? metas : [])
          .filter((m) => Array.isArray(m?.implements) && m.implements.includes('signal_source'))
          .map((m) => m.name ?? m.type).filter(Boolean))
        const typeOf = (p: any) => p?.type ?? p?.typeKey ?? (typeof p?.name === 'string' ? p.name.split('-')[0] : undefined)
        const srcs = (Array.isArray(proxies) ? proxies : []).filter((p) => types.has(typeOf(p)))
        const out: any[] = []
        for (const p of srcs) {
          const pid = p.id ?? p.name; const ptype = p.type ?? String(pid).split('-')[0]
          try {
            const rep = await requestOnce(ws, `/${ptype}/${pid}/control`, { action: 'list_signals' }, 4000)
            for (const sig of rep?.signals ?? []) out.push({ ...sig, src: String(pid) })
          } catch { /* source unreachable — skip */ }
        }
        if (alive) { setFound(out); setLoading(false) }
      } catch { if (alive) setLoading(false) }
    })()
    return () => { alive = false }
  }, [apiFetch, ws])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div className="max-h-[80vh] w-[460px] overflow-auto rounded-lg border border-slate-700 bg-slate-900 p-3 text-slate-200" onClick={(e) => e.stopPropagation()}>
        <div className="mb-2 flex items-center justify-between">
          <span className="text-sm font-medium">Add channel</span>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-200"><X size={16} /></button>
        </div>
        <div className="mb-2 text-xs font-medium text-slate-400">Discovered signals</div>
        {loading && <div className="text-xs text-slate-500">Scanning signal sources…</div>}
        {!loading && found.length === 0 && <div className="text-xs text-slate-500">No signal_source services running. Use manual binding below (e.g. a raspi/arduino pin topic).</div>}
        <div className="flex flex-col gap-1">
          {found.map((sig, i) => (
            <button key={i} className="flex items-center gap-2 rounded bg-slate-800 px-2 py-1 text-left text-xs hover:bg-slate-700"
              onClick={() => onAdd({ source_topic: sig.topic, field: sig.field, kind: sig.kind, label: sig.label })}>
              <span className={`rounded px-1 text-[10px] ${sig.kind === 'digital' ? 'bg-fuchsia-700' : 'bg-sky-700'}`}>{sig.kind}</span>
              <span className="truncate">{sig.label}</span>
              <span className="ml-auto truncate text-[10px] text-slate-500">{sig.src}</span>
            </button>
          ))}
        </div>
        <div className="mt-3 mb-1 text-xs font-medium text-slate-400">Manual binding (any topic)</div>
        <div className="flex flex-wrap items-center gap-1.5">
          <input className={`${field} flex-1`} placeholder="/arduino/arduino-1/pin/5" value={mTopic} onChange={(e) => setMTopic(e.target.value)} />
          <input className={`${field} w-24`} placeholder="value" value={mField} onChange={(e) => setMField(e.target.value)} />
          <select className={field} value={mKind} onChange={(e) => setMKind(e.target.value)}>
            <option value="analog">analog</option><option value="digital">digital</option>
          </select>
          <button className={`${btn} bg-sky-600 disabled:bg-slate-700`} disabled={!mTopic}
            onClick={() => onAdd({ source_topic: mTopic, field: mField || 'value', kind: mKind })}>Add</button>
        </div>
      </div>
    </div>
  )
}

// request/reply on an arbitrary control topic (the picker calls other services' list_signals)
function requestOnce(ws: ReturnType<typeof useWsClient>, topic: string, payload: Record<string, any>, timeoutMs: number): Promise<any> {
  return new Promise((resolve, reject) => {
    const reply = `/reply/oscope-picker/${Math.random().toString(36).slice(2)}`
    const timer = setTimeout(() => { off(); reject(new Error('timeout')) }, timeoutMs)
    const off = ws.subscribe(reply, (f: InboundFrame) => {
      if (f.method !== 'message') return
      clearTimeout(timer); off(); resolve(f.payload)
    })
    ws.publish(topic, { ...payload, reply_to: reply })
  })
}

// ── canvas renderer (hybrid: analog overlaid + digital logic rows) ───────
function drawScope(
  ctx: CanvasRenderingContext2D, W: number, H: number, s: OscopeState,
  bufs: Map<string, Buf>, lastFrame: { t: number; clientMs: number },
  lastTrig: TrigEvent | null, cursors: { a: number; b: number } | null,
  view: { vZoom: number; vPan: number; tPan: number },
) {
  ctx.clearRect(0, 0, W, H)
  ctx.fillStyle = '#020617'; ctx.fillRect(0, 0, W, H)

  const padL = 8, padR = 8, padT = 8, padB = 8
  const plotL = padL, plotR = W - padR, plotW = plotR - plotL
  const plotT = padT, plotB = H - padB
  const analogCh = s.channels.filter((c) => c.enabled && c.kind === 'analog')
  const digitalCh = s.channels.filter((c) => c.enabled && c.kind === 'digital')
  const rowH = 22, dGap = 6
  const digTop = plotB - digitalCh.length * rowH
  const aT = plotT, aB = digitalCh.length ? digTop - dGap : plotB
  const aH = Math.max(20, aB - aT), aMid = (aT + aB) / 2
  const divPx = aH / ANALOG_DIVS

  // time window (roll: right edge = now minus the horizontal pan-back)
  const tbSec = s.timebase_ms / 1000
  let tEnd = lastFrame.t - view.tPan
  const trigAlign = lastTrig && s.trigger.channel_id && s.trigger.mode !== 'auto'
  let tStart: number
  if (trigAlign) { tStart = lastTrig!.t - tbSec * 0.3; tEnd = lastTrig!.t + tbSec * 0.7 }
  else { tStart = tEnd - tbSec }
  // Global vertical transform applied on top of each channel's mapping:
  // zoom about the centre + pan (px). Lets you zoom into / move any trace.
  const applyView = (y: number) => aMid + (y - aMid) * view.vZoom + view.vPan
  const xOf = (t: number) => plotL + ((t - tStart) / (tEnd - tStart)) * plotW

  // graticule
  ctx.strokeStyle = '#1e293b'; ctx.lineWidth = 1
  ctx.beginPath()
  for (let i = 0; i <= 10; i++) { const x = plotL + (plotW * i) / 10; ctx.moveTo(x, aT); ctx.lineTo(x, aB) }
  for (let i = 0; i <= ANALOG_DIVS; i++) { const y = aT + (aH * i) / ANALOG_DIVS; ctx.moveTo(plotL, y); ctx.lineTo(plotR, y) }
  ctx.stroke()
  // centre line
  ctx.strokeStyle = '#334155'; ctx.beginPath(); ctx.moveTo(plotL, aMid); ctx.lineTo(plotR, aMid); ctx.stroke()

  // Per-channel vertical mapping. AUTO channels fit their windowed min/max to
  // ~75% of the analog height, so a 0-1 siggen wave and a 0-1023 arduino pin
  // both fill the screen; manual channels use scale (units/div) + offset.
  const yMap = (c: Channel): ((v: number) => number) => {
    if (c.auto) {
      const b = bufs.get(c.id)
      let mn = Infinity, mx = -Infinity
      if (b) for (let i = 0; i < b.t.length; i++) {
        const t = b.t[i]; if (t < tStart || t > tEnd) continue
        const v = b.v[i]; if (!Number.isFinite(v)) continue
        if (v < mn) mn = v; if (v > mx) mx = v
      }
      if (!isFinite(mn)) return () => aMid
      let range = mx - mn
      if (range < 1e-9) range = Math.max(1, Math.abs(mx))   // flat line → keep it centred
      const upd = range / (ANALOG_DIVS * 0.75)
      const center = (mn + mx) / 2
      return (v) => applyView(aMid - ((v - center) / upd) * divPx)
    }
    const scale = c.scale || 1
    return (v) => applyView(aMid - (v / scale + c.offset) * divPx)
  }

  // analog traces (clipped to the analog region)
  ctx.save(); ctx.beginPath(); ctx.rect(plotL, aT, plotW, aH); ctx.clip()
  for (const c of analogCh) {
    const b = bufs.get(c.id); if (!b || !b.t.length) continue
    const yOf = yMap(c)
    ctx.strokeStyle = c.color; ctx.lineWidth = 1.4; ctx.beginPath()
    // Hold-last-value: carry the newest sample from BEFORE the window so a pin
    // that went quiet still renders as a flat line across the screen instead of
    // blanking once its last point scrolls off the left edge.
    let started = false
    let carry: number | null = null
    for (let i = 0; i < b.t.length; i++) {
      if (b.t[i] < tStart - 0.05) { carry = b.v[i]; continue }
      const x = xOf(b.t[i]), y = yOf(b.v[i])
      if (!started) {
        if (carry !== null && Number.isFinite(carry)) { ctx.moveTo(xOf(tStart), yOf(carry)); ctx.lineTo(x, y) }
        else ctx.moveTo(x, y)
        started = true
      } else ctx.lineTo(x, y)
      if (b.t[i] > tEnd) break
    }
    // No in-window samples at all → draw the held level flat across the window.
    if (!started && carry !== null && Number.isFinite(carry)) {
      const y = yOf(carry); ctx.moveTo(xOf(tStart), y); ctx.lineTo(xOf(tEnd), y)
    }
    ctx.stroke()
  }
  // trigger level line (if the trigger source is analog)
  const trCh = analogCh.find((c) => c.id === s.trigger.channel_id)
  if (trCh) {
    const y = yMap(trCh)(s.trigger.level)
    ctx.strokeStyle = '#f59e0b'; ctx.setLineDash([4, 3]); ctx.lineWidth = 1
    ctx.beginPath(); ctx.moveTo(plotL, y); ctx.lineTo(plotR, y); ctx.stroke(); ctx.setLineDash([])
    ctx.fillStyle = '#f59e0b'; ctx.font = '10px monospace'; ctx.fillText('T', plotR - 10, y - 2)
  }
  ctx.restore()

  // trigger marker (vertical) when aligned
  if (trigAlign) {
    const x = xOf(lastTrig!.t)
    ctx.strokeStyle = '#f59e0b'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x, plotT); ctx.lineTo(x, plotB); ctx.stroke()
  }

  // digital logic rows
  ctx.font = '10px monospace'
  digitalCh.forEach((c, i) => {
    const rT = digTop + i * rowH, hi = rT + 3, lo = rT + rowH - 5
    ctx.strokeStyle = '#0f172a'; ctx.beginPath(); ctx.moveTo(plotL, rT); ctx.lineTo(plotR, rT); ctx.stroke()
    const b = bufs.get(c.id)
    if (b && b.t.length) {
      ctx.strokeStyle = c.color; ctx.lineWidth = 1.5; ctx.beginPath()
      // Hold-last-value for logic rows: carry the pre-window level so a steady
      // digital line (a resting switch) stays drawn instead of blanking.
      let started = false, py = 0, havePre = false
      for (let j = 0; j < b.t.length; j++) {
        if (b.t[j] < tStart - 0.05) { py = b.v[j] >= 0.5 ? hi : lo; havePre = true; continue }
        const x = xOf(b.t[j]); const y = b.v[j] >= 0.5 ? hi : lo
        if (!started) {
          if (havePre) { ctx.moveTo(xOf(tStart), py); ctx.lineTo(x, py); ctx.lineTo(x, y) }
          else ctx.moveTo(x, y)
          started = true
        } else { ctx.lineTo(x, py); ctx.lineTo(x, y) }
        py = y
        if (b.t[j] > tEnd) break
      }
      if (!started && havePre) { ctx.moveTo(xOf(tStart), py); ctx.lineTo(xOf(tEnd), py) }
      ctx.stroke()
    }
    ctx.fillStyle = c.color; ctx.fillText(c.label || c.id, plotL + 3, rT + 12)
  })

  // cursors
  if (cursors) {
    for (const key of ['a', 'b'] as const) {
      const x = plotL + cursors[key] * plotW
      ctx.strokeStyle = '#38bdf8'; ctx.lineWidth = 1; ctx.setLineDash([2, 2])
      ctx.beginPath(); ctx.moveTo(x, plotT); ctx.lineTo(x, plotB); ctx.stroke(); ctx.setLineDash([])
    }
    const dt = Math.abs(cursors.b - cursors.a) * (tEnd - tStart)
    ctx.fillStyle = '#38bdf8'; ctx.font = '11px monospace'
    const txt = `Δt ${(dt * 1000).toFixed(1)}ms${dt > 0 ? `  ${(1 / dt).toFixed(1)}Hz` : ''}`
    ctx.fillText(txt, plotL + 4, plotT + 12)
  }

  // timebase label
  ctx.fillStyle = '#64748b'; ctx.font = '10px monospace'
  ctx.fillText(`${s.timebase_ms >= 1000 ? s.timebase_ms / 1000 + 's' : s.timebase_ms + 'ms'} / screen`, plotR - 90, plotB - 3)
  if (!s.running) { ctx.fillStyle = '#f87171'; ctx.fillText('STOPPED', plotL + 4, plotB - 3) }
}
