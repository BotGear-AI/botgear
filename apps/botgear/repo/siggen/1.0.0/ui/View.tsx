import { useCallback, useEffect, useRef, useState } from 'react'
import { Play, Square, Plus, X } from 'lucide-react'
import type { ServiceProxy, InboundFrame } from '@rlx/ui'
import { useWsClient } from '@rlx/ui'

// ── siggen — bus function generator ─────────────────────────────────────
// A bank of waveforms streamed on /siggen/{id}/ch/{waveId}. Controls publish
// to /siggen/{id}/control; a tiny live sparkline per wave reads the stream.

interface Wave {
  id: string; label: string; shape: string; freq_hz: number; amplitude: number
  offset: number; duty: number; phase_deg: number; enabled: boolean
}
interface SiggenState { running: boolean; sample_hz: number; waves: Wave[] }

const SHAPES = ['sine', 'square', 'triangle', 'sawtooth', 'noise', 'dc', 'clock']
const field = 'rounded border border-slate-700 bg-slate-900 px-1.5 py-0.5 text-xs text-slate-200'
const btn = 'inline-flex items-center gap-1 rounded px-2 py-1 text-xs disabled:opacity-40'

function Spark({ ws, topic, digital }: { ws: ReturnType<typeof useWsClient>; topic: string; digital: boolean }) {
  const ref = useRef<HTMLCanvasElement | null>(null)
  const buf = useRef<number[]>([])
  useEffect(() => {
    const off = ws.subscribe(topic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      const v = (f.payload as any)?.value
      if (typeof v === 'number') { buf.current.push(v); if (buf.current.length > 120) buf.current.shift() }
    })
    let raf = 0
    const draw = () => {
      raf = requestAnimationFrame(draw)
      const cv = ref.current; if (!cv) return
      const ctx = cv.getContext('2d'); if (!ctx) return
      const dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight
      if (cv.width !== W * dpr || cv.height !== H * dpr) { cv.width = W * dpr; cv.height = H * dpr }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H)
      ctx.fillStyle = '#020617'; ctx.fillRect(0, 0, W, H)
      const b = buf.current; if (b.length < 2) return
      let lo = Math.min(...b), hi = Math.max(...b); if (hi - lo < 1e-6) { hi += 1; lo -= 1 }
      ctx.strokeStyle = digital ? '#e879f9' : '#38bdf8'; ctx.lineWidth = 1.2; ctx.beginPath()
      b.forEach((v, i) => { const x = (i / (b.length - 1)) * W, y = H - 2 - ((v - lo) / (hi - lo)) * (H - 4); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y) })
      ctx.stroke()
    }
    raf = requestAnimationFrame(draw)
    return () => { off(); cancelAnimationFrame(raf) }
  }, [ws, topic, digital])
  return <canvas ref={ref} className="h-8 w-24 rounded border border-slate-700" />
}

export default function SiggenView({ proxy }: { proxy: ServiceProxy }) {
  const ws = useWsClient()
  const proxyId = proxy.id ?? proxy.name ?? ''
  const stateTopic = `/siggen/${proxyId}/state`
  const controlTopic = `/siggen/${proxyId}/control`
  const [state, setState] = useState<SiggenState | null>(null)

  const send = useCallback((action: string, args: Record<string, any> = {}) => {
    ws.publish(controlTopic, { action, ...args })
  }, [ws, controlTopic])

  useEffect(() => {
    if (!proxyId) return
    return ws.subscribe(stateTopic, (f: InboundFrame) => { if (f.method === 'message') setState(f.payload as SiggenState) })
  }, [proxyId, stateTopic, ws])

  if (!state) return <div className="p-3 text-xs text-slate-400">Connecting to generator…</div>
  const running = state.running

  return (
    <div className="flex flex-col gap-2 p-2 text-slate-200">
      <div className="flex flex-wrap items-center gap-2">
        <button className={`${btn} ${running ? 'bg-emerald-600' : 'bg-slate-700'}`} onClick={() => send('set_running', { running: !running })}>
          {running ? <Square size={13} /> : <Play size={13} />} {running ? 'Running' : 'Stopped'}
        </button>
        <label className="text-xs text-slate-400">Sample rate</label>
        <input className={`${field} w-20`} type="number" value={state.sample_hz} min={1} max={500}
          onChange={(e) => send('set_sample_rate', { sample_hz: Number(e.target.value) || 100 })} />
        <span className="text-xs text-slate-500">Hz</span>
        <button className={`${btn} bg-slate-700 ml-auto`} onClick={() => send('add_wave', { shape: 'sine', freq_hz: 1 })}><Plus size={13} /> Add wave</button>
      </div>

      {state.waves.length === 0 && <div className="text-xs text-slate-500">No waves — add one to start generating.</div>}
      {state.waves.map((w) => (
        <div key={w.id} className="flex flex-wrap items-center gap-1.5 rounded bg-slate-900/60 px-2 py-1">
          <input className={`${field} w-24`} value={w.label} placeholder={w.id} onChange={(e) => send('set_wave', { id: w.id, label: e.target.value })} />
          <select className={field} value={w.shape} onChange={(e) => send('set_wave', { id: w.id, shape: e.target.value })}>
            {SHAPES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <label className="text-[10px] text-slate-500">Hz</label>
          <input className={`${field} w-16`} type="number" step="0.1" value={w.freq_hz} onChange={(e) => send('set_wave', { id: w.id, freq_hz: Number(e.target.value) })} />
          {w.shape !== 'clock' && w.shape !== 'dc' && <>
            <label className="text-[10px] text-slate-500">amp</label>
            <input className={`${field} w-14`} type="number" step="0.1" value={w.amplitude} onChange={(e) => send('set_wave', { id: w.id, amplitude: Number(e.target.value) })} />
          </>}
          {w.shape !== 'clock' && <>
            <label className="text-[10px] text-slate-500">off</label>
            <input className={`${field} w-14`} type="number" step="0.1" value={w.offset} onChange={(e) => send('set_wave', { id: w.id, offset: Number(e.target.value) })} />
          </>}
          {(w.shape === 'square' || w.shape === 'clock') && <>
            <label className="text-[10px] text-slate-500">duty</label>
            <input className={`${field} w-14`} type="number" step="0.05" min={0} max={1} value={w.duty} onChange={(e) => send('set_wave', { id: w.id, duty: Number(e.target.value) })} />
          </>}
          <Spark ws={ws} topic={`/siggen/${proxyId}/ch/${w.id}`} digital={w.shape === 'clock'} />
          <label className="flex items-center gap-1 text-[10px] text-slate-500">
            <input type="checkbox" checked={w.enabled} onChange={(e) => send('set_wave', { id: w.id, enabled: e.target.checked })} /> on
          </label>
          <button className="rounded p-1 text-slate-400 hover:text-rose-300" onClick={() => send('remove_wave', { id: w.id })}><X size={13} /></button>
        </div>
      ))}
    </div>
  )
}
