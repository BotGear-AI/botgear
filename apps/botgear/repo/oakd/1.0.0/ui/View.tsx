// OAK-D — view_full panel for the oakd service.
//
// Purpose-built layout (not a copy of video's view): two side-by-side
// live feeds (RGB + colorized depth), a device/connection status strip
// that surfaces the facts an
// operator actually needs to debug a choppy/dead stream (usb_speed
// especially — the #1 likely support question), and the same
// FilterPipeline *pattern* video's view uses (applied to the RGB stream
// only), written fresh against oakd's own topics.
//
// Scope cut vs video's view: no interactive point/rect/points picking
// overlay — oakd's v1 filter catalog (just `overlay`) doesn't need
// spatial params, so that ~600-line subsystem isn't ported. A future
// filter needing it would need this view extended, same as any other
// service-UI evolution.
import {
  useCallback, useEffect, useMemo, useState, type FormEvent,
} from 'react'
import type { ServiceProxy } from '@rlx/ui'
import { type InboundFrame } from '@rlx/ui'
import { useWsClient, useActiveRuntime } from '@rlx/ui'

interface OakdState {
  connected?: boolean
  autoreconnect?: boolean
  retrying?: boolean
  mxid?: string | null
  device_name?: string | null
  usb_speed?: string | null
  has_imu?: boolean | null
  rgb_resolution?: string | null
  depth_enabled?: boolean
  mono_stream_camera?: 'left' | 'right' | null
  observed_fps?: number
  dropped?: number
  error?: string | null
}

interface SnapshotPayload {
  request_id?: string | null
  ts?: number
  resolution?: [number, number]
  jpeg_b64?: string
  error?: string | null
}

interface ParamSchema {
  name: string
  type: 'int' | 'float' | 'bool' | 'enum' | 'string' | 'point' | 'rect' | 'points'
  default: number | boolean | string | number[] | number[][]
  min?: number
  max?: number
  step?: number
  choices?: string[]
  label?: string
  help?: string
  placeholder?: string
}

interface CatalogEntry {
  type: string
  title: string
  description: string
  publishes_telemetry: boolean
  param_schema: ParamSchema[]
  telemetry_schema?: Record<string, unknown> | null
}

interface FilterSpec {
  id: string
  type: string
  enabled: boolean
  params: Record<string, unknown>
  title?: string
  publishes_telemetry?: boolean
  telemetry_topic?: string
}

export default function OakdFullView({ proxy }: { proxy: ServiceProxy }) {
  const wsClient = useWsClient()
  const { connection } = useActiveRuntime()
  const proxyId = proxy.id ?? proxy.name ?? ''
  const stateTopic = `/oakd/${proxyId}/state`
  const controlTopic = `/oakd/${proxyId}/control`
  const rgbStreamId = `oakd/${proxyId}/rgb`
  const depthStreamId = `oakd/${proxyId}/depth`

  const [state, setState] = useState<OakdState>({})
  const [reloadTick, setReloadTick] = useState(0)
  // Which SINGLE mono camera to show — for visually checking that
  // camera's own view isn't physically occluded (a mount bracket,
  // cable), something the fused colorized depth preview can't reveal
  // on its own. ONE AT A TIME, not both together — confirmed live on
  // worke's real (USB2-limited) OAK-D Lite that running both
  // simultaneously on top of the existing RGB+depth load exceeds
  // available USB bandwidth ("Couldn't open stream"); see the worke
  // hardware doc in the Obsidian vault.
  const [monoMode, setMonoMode] = useState<'off' | 'left' | 'right'>('off')
  const [snapshotOpen, setSnapshotOpen] = useState(false)
  const [snapshotPayload, setSnapshotPayload] = useState<SnapshotPayload | null>(null)
  const [snapshotError, setSnapshotError] = useState<string | null>(null)
  const [snapshotPending, setSnapshotPending] = useState(false)

  useEffect(() => {
    if (!proxyId) return
    return wsClient.subscribe(stateTopic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      const p = f.payload
      if (p && typeof p === 'object') setState(p as OakdState)
    })
  }, [proxyId, stateTopic, wsClient])

  const streamUrl = useCallback((streamId: string) => {
    if (!connection) return null
    const token = connection.getAccessToken()
    if (!token) return null
    return `${connection.url}/v1/stream/${encodeURIComponent(streamId)}/mjpeg?token=${encodeURIComponent(token)}&_=${reloadTick}`
  }, [connection, reloadTick])

  const rgbUrl = useMemo(() => streamUrl(rgbStreamId), [streamUrl, rgbStreamId])
  const depthUrl = useMemo(() => streamUrl(depthStreamId), [streamUrl, depthStreamId])
  // Only one topic exists at a time server-side, named for whichever
  // side is actually active (state.mono_stream_camera, not the local
  // monoMode selection — that's a request, this is confirmed reality).
  const monoStreamId = state.mono_stream_camera ? `oakd/${proxyId}/mono_${state.mono_stream_camera}` : null
  const monoUrl = useMemo(() => (monoStreamId ? streamUrl(monoStreamId) : null), [streamUrl, monoStreamId])

  const sendAction = useCallback((action: string, args: Record<string, unknown> = {}) => {
    wsClient.publish(controlTopic, { action, ...args })
  }, [wsClient, controlTopic])

  const onSetMonoMode = useCallback((mode: 'off' | 'left' | 'right') => {
    setMonoMode(mode)
    const wantCamera = mode === 'off' ? null : mode
    if (wantCamera !== (state.mono_stream_camera ?? null)) {
      sendAction('set_mono_stream', { camera: wantCamera })
      setReloadTick((t) => t + 1)
    }
  }, [sendAction, state.mono_stream_camera])

  const onConnect = useCallback((e?: FormEvent) => {
    e?.preventDefault()
    sendAction('connect')
    setReloadTick((t) => t + 1)
  }, [sendAction])
  const onDisconnect = useCallback(() => sendAction('disconnect'), [sendAction])
  const onToggleAutoConnect = useCallback(
    (checked: boolean) => sendAction('set_auto_connect', { enabled: checked }),
    [sendAction],
  )

  const onSnapshot = useCallback(() => {
    if (snapshotPending) return
    const requestId = (typeof crypto !== 'undefined' && 'randomUUID' in crypto)
      ? crypto.randomUUID() : `snap-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`
    setSnapshotOpen(true)
    setSnapshotPending(true)
    setSnapshotPayload(null)
    setSnapshotError(null)
    const off = wsClient.subscribe(`/oakd/${proxyId}/snapshot`, (f: InboundFrame) => {
      if (f.method !== 'message') return
      const p = f.payload as SnapshotPayload | undefined
      if (!p || p.request_id !== requestId) return
      off()
      window.clearTimeout(timeoutId)
      setSnapshotPending(false)
      if (p.error) setSnapshotError(p.error)
      else setSnapshotPayload(p)
    })
    const timeoutId = window.setTimeout(() => {
      off()
      setSnapshotPending(false)
      setSnapshotError('snapshot timed out — no response from oakd service in 5s')
    }, 5000)
    sendAction('snapshot', { request_id: requestId })
  }, [snapshotPending, wsClient, proxyId, sendAction])

  const serviceRunning = proxy.status === 'running' || proxy.status === 'starting'
  const connected = !!state.connected

  return (
    <div className="flex min-w-[560px] flex-col gap-2 p-3 text-xs" onPointerDown={(e) => e.stopPropagation()}>
      {/* ── status strip ── */}
      <div className="rounded border border-slate-800 bg-slate-900/40 p-2">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
          <Field
            label="status"
            value={connected ? '● connected' : (state.retrying ? '○ reconnecting…' : '○ disconnected')}
            valueClass={connected ? 'text-emerald-400' : (state.retrying ? 'text-amber-400' : 'text-rose-400')}
            hint={state.retrying ? 'Auto-connect is on — retrying every few seconds in the background (replug the camera, or a bad cable/port at boot both recover on their own).' : undefined}
          />
          <Field label="device" value={state.device_name ?? '—'} />
          <Field label="mxid" value={state.mxid ?? '—'} mono />
          <Field
            label="usb"
            value={state.usb_speed ?? '—'}
            valueClass={state.usb_speed && !['SUPER', 'SUPER_PLUS'].includes(state.usb_speed) ? 'text-amber-400' : undefined}
            hint="OAK-D wants USB3 (SUPER/SUPER_PLUS). Anything else (HIGH = USB2) means reduced bandwidth — expect a choppier/less reliable stream. Check the cable/port, not botgear config."
          />
          <Field
            label="imu"
            value={state.has_imu == null ? 'unknown' : (state.has_imu ? 'present' : 'not present')}
            hint="Queried from the device at connect time — OAK-D Lite units vary on whether they shipped with an IMU, so this is reported rather than assumed."
          />
          <Field label="rgb res" value={state.rgb_resolution ?? '—'} />
          <Field label="depth" value={state.depth_enabled ? 'on' : 'off'} />
          <Field label="fps" value={state.observed_fps != null ? state.observed_fps.toFixed(1) : '—'} />
        </div>
        {state.error && (
          <div className="mt-2 rounded border border-rose-800 bg-rose-950/40 px-2 py-1 font-mono text-[10px] text-rose-200 whitespace-pre-wrap">
            {state.error}
          </div>
        )}
        <div className="mt-2 flex items-center gap-2">
          {/* Auto-connect toggle — keeps retrying every few seconds
              whenever disconnected, including after the camera is
              unplugged and plugged back in (possibly on a different
              port/cable — DepthAI matches by the device's own serial,
              not the USB path). */}
          <label
            className="flex items-center gap-1.5 font-mono text-[10px] text-slate-400"
            onPointerDown={(e) => e.stopPropagation()}
          >
            <input
              type="checkbox"
              checked={!!state.autoreconnect}
              onChange={(e) => onToggleAutoConnect(e.target.checked)}
              disabled={!serviceRunning}
              className="nodrag nopan accent-emerald-500"
            />
            auto-reconnect
          </label>
          <button type="button" onClick={onConnect} onPointerDown={(e) => e.stopPropagation()} disabled={!serviceRunning}
                  className="nodrag nopan rounded bg-sky-600 px-2 py-1 text-[10px] font-medium text-white hover:bg-sky-500 disabled:cursor-not-allowed disabled:opacity-40">
            {connected ? 'Reconnect' : 'Connect'}
          </button>
          <button type="button" onClick={onDisconnect} onPointerDown={(e) => e.stopPropagation()} disabled={!serviceRunning || !connected}
                  className="nodrag nopan rounded border border-slate-700 px-2 py-1 text-[10px] text-slate-300 hover:bg-slate-800 disabled:opacity-40">
            Disconnect
          </button>
          <button type="button" onClick={onSnapshot} onPointerDown={(e) => e.stopPropagation()} disabled={!serviceRunning || !connected}
                  className="nodrag nopan rounded border border-slate-700 px-2 py-1 text-[10px] text-slate-300 hover:bg-slate-800 disabled:opacity-40">
            Snapshot
          </button>
        </div>
      </div>

      {/* ── dual live feed ── */}
      <div className="grid grid-cols-2 gap-2">
        <FeedPanel label="RGB" url={rgbUrl} connected={connected} />
        <FeedPanel label={`Depth (${state.depth_enabled ? 'on' : 'off'})`} url={state.depth_enabled ? depthUrl : null} connected={connected} />
      </div>

      {/* ── mono camera check (self-occlusion) ── */}
      <div className="rounded border border-slate-800 bg-slate-900/40 p-2">
        <div
          className="flex items-center gap-2"
          title="Raw mono-camera feed, untouched by stereo matching — for confirming that camera's own view isn't blocked by a mount bracket, cable, or anything else on the robot. Left/right switch, not both at once — this robot's OAK-D Lite can't sustain both simultaneously on top of the existing RGB+depth load over USB2 (see the worke hardware doc)."
        >
          <span className="text-[9px] uppercase tracking-wider text-slate-500">mono camera</span>
          {(['off', 'left', 'right'] as const).map((mode) => (
            <button
              key={mode} type="button"
              onClick={() => onSetMonoMode(mode)}
              onPointerDown={(e) => e.stopPropagation()}
              disabled={!serviceRunning || !state.depth_enabled}
              className={`nodrag nopan rounded border px-2 py-1 text-[10px] capitalize disabled:cursor-not-allowed disabled:opacity-40 ${
                monoMode === mode
                  ? 'border-sky-500 bg-sky-950/60 text-sky-300'
                  : 'border-slate-700 text-slate-300 hover:bg-slate-800'
              }`}
            >
              {mode}
            </button>
          ))}
          {!state.depth_enabled && <span className="text-[9px] text-slate-600">needs depth enabled</span>}
        </div>
        {monoMode !== 'off' && (
          <div className="mt-2">
            <FeedPanel label={`Mono ${monoMode === 'left' ? 'Left' : 'Right'}`} url={monoUrl} connected={connected} />
          </div>
        )}
      </div>

      <FilterPipeline proxyId={proxyId} />

      {snapshotOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={() => setSnapshotOpen(false)}>
          <div className="max-w-[80vw] rounded border border-slate-700 bg-slate-900 p-3" onClick={(e) => e.stopPropagation()}>
            <div className="mb-2 flex items-center justify-between gap-4">
              <span className="text-[11px] text-slate-300">Snapshot</span>
              <button type="button" onClick={() => setSnapshotOpen(false)} className="text-[11px] text-slate-500 hover:text-slate-200">✕</button>
            </div>
            {snapshotPending && <div className="text-[11px] text-slate-400">waiting…</div>}
            {snapshotError && <div className="text-[11px] text-rose-300">{snapshotError}</div>}
            {snapshotPayload?.jpeg_b64 && (
              <img alt="snapshot" src={`data:image/jpeg;base64,${snapshotPayload.jpeg_b64}`} className="max-h-[70vh] max-w-full" />
            )}
          </div>
        </div>
      )}
    </div>
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

function FeedPanel({ label, url, connected }: { label: string; url: string | null; connected: boolean }) {
  return (
    <div className="rounded border border-slate-800 bg-slate-950/60">
      <div className="border-b border-slate-800 px-2 py-1 text-[9px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className="flex aspect-video items-center justify-center overflow-hidden">
        {url && connected ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={url} alt={label} className="max-h-full max-w-full" />
        ) : (
          <span className="text-[10px] text-slate-600">{connected ? 'stream unavailable' : 'not connected'}</span>
        )}
      </div>
    </div>
  )
}

// ─── filter pipeline — same pattern as video's, applied to the RGB
// stream only, written fresh against oakd's own topics (no shared
// import across separately-bundled service UIs) ───────────────────────

function FilterPipeline({ proxyId }: { proxyId: string }) {
  const wsClient = useWsClient()
  const [catalog, setCatalog] = useState<CatalogEntry[]>([])
  const [filters, setFilters] = useState<FilterSpec[]>([])
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [addType, setAddType] = useState<string>('')

  useEffect(() => {
    const offCat = wsClient.subscribe(`/oakd/${proxyId}/filter_catalog`, (f: InboundFrame) => {
      if (f.method !== 'message') return
      const p = f.payload as { filters?: CatalogEntry[] } | undefined
      if (p && Array.isArray(p.filters)) setCatalog(p.filters)
    })
    const offFil = wsClient.subscribe(`/oakd/${proxyId}/filters`, (f: InboundFrame) => {
      if (f.method !== 'message') return
      const p = f.payload as { filters?: FilterSpec[] } | undefined
      if (p && Array.isArray(p.filters)) setFilters(p.filters)
    })
    return () => { offCat(); offFil() }
  }, [proxyId, wsClient])

  const sendControl = useCallback((action: string, args: Record<string, unknown> = {}) => {
    wsClient.publish(`/oakd/${proxyId}/control`, { action, ...args })
  }, [wsClient, proxyId])

  const onAdd = useCallback(() => {
    if (!addType) return
    const id = (typeof crypto !== 'undefined' && 'randomUUID' in crypto)
      ? crypto.randomUUID() : `${addType}-${Date.now()}`
    sendControl('add_filter', { type: addType, id })
    setExpandedId(id)
  }, [addType, sendControl])

  const onRemove = useCallback((id: string) => {
    if (expandedId === id) setExpandedId(null)
    sendControl('remove_filter', { id })
  }, [sendControl, expandedId])

  const onUpdate = useCallback((id: string, patch: { params?: Record<string, unknown>; enabled?: boolean }) => {
    sendControl('update_filter', { id, ...patch })
  }, [sendControl])

  const catalogByType = useMemo(() => new Map(catalog.map((c) => [c.type, c])), [catalog])

  return (
    <section className="rounded border border-slate-800 bg-slate-900/40">
      <header className="flex items-center justify-between border-b border-slate-800 px-3 py-1.5">
        <div className="flex items-baseline gap-2">
          <h3 className="text-[10px] uppercase tracking-wider text-slate-400">Filters (RGB)</h3>
          <span className="font-mono text-[10px] text-slate-500">{filters.length}</span>
        </div>
        <div className="flex items-center gap-1">
          <select
            value={addType}
            onChange={(e) => setAddType(e.target.value)}
            onPointerDown={(e) => e.stopPropagation()}
            className="nodrag nopan rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 text-[10px]"
          >
            <option value="">add filter…</option>
            {catalog.map((c) => <option key={c.type} value={c.type}>{c.title}</option>)}
          </select>
          <button type="button" onClick={onAdd} onPointerDown={(e) => e.stopPropagation()} disabled={!addType}
                  className="nodrag nopan rounded bg-sky-700 px-2 py-0.5 text-[10px] text-white hover:bg-sky-600 disabled:opacity-40">
            Add
          </button>
        </div>
      </header>
      <div className="flex flex-col gap-1 p-2">
        {filters.length === 0 && <div className="text-[10px] text-slate-600">no filters — RGB stream is unmodified</div>}
        {filters.map((spec) => {
          const cat = catalogByType.get(spec.type)
          const expanded = expandedId === spec.id
          return (
            <div key={spec.id} className="rounded border border-slate-800 bg-slate-950/40">
              <div className="flex items-center justify-between gap-2 px-2 py-1">
                <button type="button" onClick={() => setExpandedId(expanded ? null : spec.id)}
                        onPointerDown={(e) => e.stopPropagation()}
                        className="nodrag nopan flex-1 truncate text-left text-[11px] text-slate-200">
                  {expanded ? '▾' : '▸'} {spec.title ?? cat?.title ?? spec.type}
                </button>
                <button type="button" onClick={() => onUpdate(spec.id, { enabled: !spec.enabled })}
                        onPointerDown={(e) => e.stopPropagation()}
                        className={`nodrag nopan rounded px-1.5 py-0.5 text-[9px] ${spec.enabled ? 'bg-emerald-900/50 text-emerald-300' : 'bg-slate-800 text-slate-500'}`}>
                  {spec.enabled ? 'ON' : 'OFF'}
                </button>
                <button type="button" onClick={() => onRemove(spec.id)} onPointerDown={(e) => e.stopPropagation()}
                        className="nodrag nopan text-[11px] text-slate-500 hover:text-rose-400">✕</button>
              </div>
              {expanded && cat && (
                <div className="flex flex-col gap-1.5 border-t border-slate-800 p-2">
                  {cat.param_schema.map((schema) => (
                    <ParamInput
                      key={schema.name}
                      schema={schema}
                      value={spec.params[schema.name]}
                      onChange={(v) => onUpdate(spec.id, { params: { [schema.name]: v } })}
                    />
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </section>
  )
}

function ParamInput({ schema, value, onChange }: { schema: ParamSchema; value: unknown; onChange: (v: unknown) => void }) {
  const label = schema.label ?? schema.name
  const current = value === undefined ? schema.default : value

  if (schema.type === 'point' || schema.type === 'rect' || schema.type === 'points') {
    // No oakd v1 filter uses a spatial param — see this file's header
    // comment. Fallback: raw JSON so a future filter isn't unusable,
    // just unpolished, until a picking overlay is built for this view.
    return (
      <label className="flex items-center gap-2">
        <span className="w-24 text-[10px] uppercase tracking-wider text-slate-500">{label}</span>
        <input
          type="text"
          defaultValue={JSON.stringify(current ?? [])}
          onBlur={(e) => { try { onChange(JSON.parse(e.target.value)) } catch { /* ignore invalid JSON */ } }}
          onPointerDown={(e) => e.stopPropagation()}
          className="nodrag nopan flex-1 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono text-[10px]"
        />
      </label>
    )
  }
  if (schema.type === 'bool') {
    return (
      <label className="flex items-center gap-2">
        <input type="checkbox" checked={!!current} onChange={(e) => onChange(e.target.checked)}
               onPointerDown={(e) => e.stopPropagation()} className="nodrag nopan" />
        <span className="text-[11px] text-slate-300">{label}</span>
      </label>
    )
  }
  if (schema.type === 'enum') {
    return (
      <label className="flex items-center gap-2">
        <span className="w-24 text-[10px] uppercase tracking-wider text-slate-500">{label}</span>
        <select value={String(current)} onChange={(e) => onChange(e.target.value)} onPointerDown={(e) => e.stopPropagation()}
                className="nodrag nopan rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 text-[11px]">
          {(schema.choices ?? []).map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      </label>
    )
  }
  if (schema.type === 'string') {
    return (
      <label className="flex items-center gap-2">
        <span className="w-24 shrink-0 text-[10px] uppercase tracking-wider text-slate-500">{label}</span>
        <input type="text" value={typeof current === 'string' ? current : ''} onChange={(e) => onChange(e.target.value)}
               onPointerDown={(e) => e.stopPropagation()} placeholder={schema.placeholder}
               className="nodrag nopan flex-1 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono text-[11px]" />
      </label>
    )
  }
  const num = typeof current === 'number' ? current : Number(current) || 0
  return (
    <div className="flex items-center gap-2">
      <span className="w-24 text-[10px] uppercase tracking-wider text-slate-500">{label}</span>
      {schema.min != null && schema.max != null && (
        <input type="range" min={schema.min} max={schema.max} step={schema.step ?? (schema.type === 'int' ? 1 : 0.01)}
               value={num} onChange={(e) => onChange(schema.type === 'int' ? parseInt(e.target.value, 10) : parseFloat(e.target.value))}
               onPointerDown={(e) => e.stopPropagation()} className="nodrag nopan flex-1" />
      )}
      <input type="number" min={schema.min} max={schema.max} step={schema.step ?? (schema.type === 'int' ? 1 : 0.01)} value={num}
             onChange={(e) => { const v = e.target.value; if (v !== '') onChange(schema.type === 'int' ? parseInt(v, 10) : parseFloat(v)) }}
             onPointerDown={(e) => e.stopPropagation()} className="nodrag nopan w-16 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 text-right font-mono text-[11px]" />
    </div>
  )
}
