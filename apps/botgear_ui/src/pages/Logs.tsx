import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Banner from '../components/Banner'
import { useApiFetch } from '../contexts/ActiveRuntimeContext'
import { wsClient, type InboundFrame } from '../runtime/wsClient'
import type { ServiceProxy } from '../models/ServiceProxy'

/**
 * Centralized log viewer.
 *
 * The bus already publishes per-service log lines on
 *     /service_proxy/{id}/log
 * for every service (subprocess stdout/stderr → process_manager pumps;
 * in-process services → framework's emit_log()). This page subscribes
 * via the wildcard `/service_proxy/+/log` so a single view aggregates
 * every service's log stream with no per-service plumbing.
 *
 * Filters are client-side (cheap at our scale; the server sees only one
 * subscriber). Level is heuristic-parsed from the line text — services
 * don't currently emit structured levels.
 */

interface LogPayload {
  stream?: 'stdout' | 'stderr'
  line?: string
  ts?: number  // seconds since epoch
}

interface LogEntry {
  serviceId: string
  ts: number       // ms since epoch (UI sorts on this)
  stream: 'stdout' | 'stderr'
  line: string
  level: LogLevel  // derived
}

type LogLevel = 'error' | 'warn' | 'info' | 'debug'

const MAX_HISTORY = 2000  // ring buffer cap

// stderr defaults to error; otherwise classic level keywords win.
function deriveLevel(line: string, stream: 'stdout' | 'stderr'): LogLevel {
  const m = /\b(ERROR|CRITICAL|FATAL|WARN(?:ING)?|INFO|DEBUG|TRACE)\b/i.exec(line)
  if (m) {
    const tok = m[1].toUpperCase()
    if (tok === 'ERROR' || tok === 'CRITICAL' || tok === 'FATAL') return 'error'
    if (tok.startsWith('WARN')) return 'warn'
    if (tok === 'DEBUG' || tok === 'TRACE') return 'debug'
    return 'info'
  }
  return stream === 'stderr' ? 'error' : 'info'
}

const LEVEL_ORDER: Record<LogLevel, number> = { error: 0, warn: 1, info: 2, debug: 3 }

const LEVEL_TONE: Record<LogLevel, string> = {
  error: 'text-rose-300',
  warn: 'text-amber-300',
  info: 'text-slate-200',
  debug: 'text-slate-500',
}

const LEVEL_BADGE: Record<LogLevel, string> = {
  error: 'bg-rose-700 text-rose-100',
  warn: 'bg-amber-700 text-amber-100',
  info: 'bg-slate-700 text-slate-200',
  debug: 'bg-slate-800 text-slate-400',
}

function extractServiceId(topic: string): string {
  // /service_proxy/<id>/log
  const m = /^\/service_proxy\/([^/]+)\/log$/.exec(topic)
  return m ? m[1] : 'unknown'
}

// ─── runtime log (GET /v1/logs ring buffer) ──────────────────────────
// The per-service ws stream above only carries managed-service output;
// the backend's own log (lifecycle, errors) lives in a ring buffer we
// backfill + poll so the page isn't empty when nothing's actively logging.
interface RuntimeLog { ts: number; level?: string; logger?: string; line?: string }

function backendLevel(s?: string): LogLevel {
  const t = (s ?? '').toUpperCase()
  if (t === 'ERROR' || t === 'CRITICAL' || t === 'FATAL') return 'error'
  if (t.startsWith('WARN')) return 'warn'
  if (t === 'DEBUG' || t === 'TRACE') return 'debug'
  return 'info'
}

function shortLogger(name?: string): string {
  return name ? name.split('.').slice(-2).join('.') : 'runtime'
}

function mapRuntime(d: RuntimeLog): LogEntry {
  const level = backendLevel(d.level)
  return {
    serviceId: 'runtime',
    ts: typeof d.ts === 'number' ? d.ts : Date.now(),
    stream: level === 'error' ? 'stderr' : 'stdout',
    line: `${shortLogger(d.logger)}: ${d.line ?? ''}`,
    level,
  }
}

// Self-referential noise: this page polls GET /v1/logs every 2s (see the
// backfill effect below) to backfill the runtime ring buffer, and the
// backend's HTTP access log records every request — including that one —
// into the SAME ring buffer. Left unfiltered, viewing the log becomes an
// infinite loop of the log viewer logging that it viewed the log. Scoped to
// serviceId==='runtime' (where the HTTP access log lands) + the /v1/logs
// path specifically, so a service that happens to print "/v1/logs" as
// meaningful output elsewhere isn't silently dropped.
const SELF_LOG_RE = /\/v1\/logs(?:[?/]|\s|$)/

function isSelfReferential(e: LogEntry): boolean {
  return e.serviceId === 'runtime' && SELF_LOG_RE.test(e.line)
}

const keyOf = (e: LogEntry) => `${e.ts}|${e.serviceId}|${e.line}`

function mergeEntries(prev: LogEntry[], incoming: LogEntry[]): LogEntry[] {
  if (!incoming.length) return prev
  const seen = new Set(prev.map(keyOf))
  const fresh = incoming.filter((e) => !seen.has(keyOf(e)))
  if (!fresh.length) return prev
  const merged = prev.concat(fresh).sort((a, b) => a.ts - b.ts)
  return merged.length > MAX_HISTORY ? merged.slice(-MAX_HISTORY) : merged
}

export default function Logs() {

  const [entries, setEntries] = useState<LogEntry[]>([])
  const [paused, setPaused] = useState(false)
  const [serviceFilter, setServiceFilter] = useState<string>('') // empty = all
  const [minLevel, setMinLevel] = useState<LogLevel>('debug') // show all by default
  const [textFilter, setTextFilter] = useState('')
  const [autoScroll, setAutoScroll] = useState(true)
  // Default ON — the self-referential /v1/logs polling noise is almost
  // never what anyone opened this page to look at.
  const [hideSelfLogs, setHideSelfLogs] = useState(true)

  // Refs so the WS handler closure sees the latest values without
  // re-subscribing on every keystroke.
  const pausedRef = useRef(paused); pausedRef.current = paused

  // Subscribe to the wildcard log topic. One subscription, every service.
  useEffect(() => {
    const off = wsClient.subscribe('/service_proxy/+/log', (f: InboundFrame) => {
      if (f.method !== 'message') return
      if (pausedRef.current) return
      const topic = f.topic ?? ''
      const payload = f.payload as LogPayload | undefined
      if (!payload || !payload.line) return
      const stream: 'stdout' | 'stderr' = payload.stream === 'stderr' ? 'stderr' : 'stdout'
      const tsMs = typeof payload.ts === 'number' ? payload.ts * 1000 : Date.now()
      const entry: LogEntry = {
        serviceId: extractServiceId(topic),
        ts: tsMs,
        stream,
        line: payload.line,
        level: deriveLevel(payload.line, stream),
      }
      setEntries((prev) => {
        const next = prev.concat([entry])
        return next.length > MAX_HISTORY ? next.slice(-MAX_HISTORY) : next
      })
    })
    return off
  }, [])

  const apiFetch = useApiFetch()

  // Known proxy ids, independent of whether they've logged anything yet.
  // The dropdown used to be built solely from `entries` (below) — a service
  // only appeared once it happened to emit a line while this page was
  // open, so a quiet-but-running service (most subprocess services, most
  // of the time) never showed up. Polling the actual proxy list is the
  // same source of truth Proxies.tsx/Topology.tsx use, so every running
  // service is selectable immediately.
  const [knownServiceIds, setKnownServiceIds] = useState<string[]>([])
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const rows = await apiFetch<ServiceProxy[]>('/v1/service-proxy-list')
        if (cancelled) return
        setKnownServiceIds((rows ?? []).map((r) => r.id).filter((id): id is string => !!id))
      } catch {
        /* transient — next tick retries */
      }
    }
    load()
    const id = window.setInterval(load, 5000)
    return () => { cancelled = true; window.clearInterval(id) }
  }, [apiFetch])

  // Backfill + poll the runtime log ring buffer (GET /v1/logs) so the page
  // shows backend activity immediately, with history — merged/deduped with
  // the live per-service stream above.
  const clearedBeforeRef = useRef(0)
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const rows = await apiFetch<RuntimeLog[]>('/v1/logs?limit=400')
        if (cancelled || pausedRef.current) return
        const mapped = (rows ?? [])
          .filter((d) => (typeof d.ts === 'number' ? d.ts : 0) > clearedBeforeRef.current)
          .map(mapRuntime)
        setEntries((prev) => mergeEntries(prev, mapped))
      } catch {
        /* transient (e.g. auth refresh) — the next tick retries */
      }
    }
    load()
    const id = window.setInterval(load, 2000)
    return () => { cancelled = true; window.clearInterval(id) }
  }, [apiFetch])

  // Auto-scroll on new entries.
  const listRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!autoScroll || !listRef.current) return
    listRef.current.scrollTop = listRef.current.scrollHeight
  }, [entries, autoScroll])

  // Dropdown options: every currently-known proxy (from /v1/service-proxy-list)
  // plus 'runtime' plus anything we've observed on the wire but that has
  // since been released — so a service is selectable the instant it's
  // running, and stays selectable if you're mid-filter when it stops.
  const services = useMemo(() => {
    const s = new Set<string>(knownServiceIds)
    s.add('runtime')
    for (const e of entries) s.add(e.serviceId)
    return Array.from(s).sort()
  }, [knownServiceIds, entries])

  const filtered = useMemo(() => {
    const q = textFilter.trim().toLowerCase()
    return entries.filter((e) => {
      if (hideSelfLogs && isSelfReferential(e)) return false
      if (serviceFilter && e.serviceId !== serviceFilter) return false
      if (LEVEL_ORDER[e.level] > LEVEL_ORDER[minLevel]) return false
      if (q && !e.line.toLowerCase().includes(q)) return false
      return true
    })
  }, [entries, serviceFilter, minLevel, textFilter, hideSelfLogs])

  const onClear = useCallback(() => {
    // Suppress re-backfilling the runtime rows we just cleared.
    clearedBeforeRef.current = Date.now()
    setEntries([])
  }, [])

  return (
    <div className="mx-auto flex h-screen max-w-screen-2xl flex-col p-6">
      <header>
        <h1 className="text-lg font-semibold">Logs</h1>
      </header>

      <div className="mb-2 flex flex-wrap items-center gap-2 rounded-lg border border-slate-700 bg-slate-900/60 p-3 text-xs">
        <button
          type="button"
          onClick={() => setPaused((p) => !p)}
          className={
            paused
              ? 'rounded bg-amber-600 px-2 py-1 font-medium text-white hover:bg-amber-500'
              : 'rounded border border-slate-700 px-2 py-1 text-slate-200 hover:border-slate-500'
          }
        >
          {paused ? 'Resume' : 'Pause'}
        </button>
        <button
          type="button"
          onClick={onClear}
          className="rounded border border-slate-700 px-2 py-1 text-slate-200 hover:border-slate-500"
        >
          Clear
        </button>
        <label className="flex items-center gap-1 text-slate-400">
          <input
            type="checkbox"
            checked={autoScroll}
            onChange={(e) => setAutoScroll(e.target.checked)}
          />
          Auto-scroll
        </label>
        <label
          className="flex items-center gap-1 text-slate-400"
          title="Hide this page's own GET /v1/logs polling — otherwise the log viewer logs that it's viewing the log, forever."
        >
          <input
            type="checkbox"
            checked={hideSelfLogs}
            onChange={(e) => setHideSelfLogs(e.target.checked)}
          />
          Hide /v1/logs noise
        </label>

        <span className="mx-1 text-slate-700">|</span>

        <label className="text-slate-400">service</label>
        <select
          value={serviceFilter}
          onChange={(e) => setServiceFilter(e.target.value)}
          className="rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-xs text-slate-200"
        >
          <option value="">(all)</option>
          {services.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>

        <label className="text-slate-400">level ≥</label>
        <select
          value={minLevel}
          onChange={(e) => setMinLevel(e.target.value as LogLevel)}
          className="rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-xs text-slate-200"
        >
          <option value="error">error</option>
          <option value="warn">warn</option>
          <option value="info">info</option>
          <option value="debug">debug (all)</option>
        </select>

        <span className="mx-1 text-slate-700">|</span>

        <input
          type="text"
          placeholder="filter… (substring match on line)"
          value={textFilter}
          onChange={(e) => setTextFilter(e.target.value)}
          className="flex-1 rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-xs text-slate-200 placeholder-slate-600 focus:border-slate-500 focus:outline-none"
        />
        <span className="text-slate-500">
          {filtered.length}
          {(serviceFilter || minLevel !== 'debug' || textFilter || hideSelfLogs) ? ` / ${entries.length}` : ''}
        </span>
      </div>

      {paused && (
        <Banner tone="info">Capture paused — new log lines are being dropped while paused.</Banner>
      )}

      <div
        ref={listRef}
        className="flex-1 overflow-auto rounded-lg border border-slate-700 bg-slate-950 font-mono text-xs"
      >
        {filtered.length === 0 ? (
          <div className="p-4 text-slate-500">
            No log lines{paused ? ' (paused)' : ''}{entries.length > 0 ? ' match your filters' : ' yet'}.
            Start a service or trigger an action; lines appear here as soon as the bus emits them.
          </div>
        ) : (
          <ul className="divide-y divide-slate-900">
            {filtered.map((entry, i) => (
              <LogRow key={i} entry={entry} />
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

function LogRow({ entry }: { entry: LogEntry }) {
  const t = new Date(entry.ts)
  const hms = `${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}.${pad(t.getMilliseconds(), 3)}`
  return (
    <li className="flex items-baseline gap-2 px-3 py-1 hover:bg-slate-900/50">
      <span className="shrink-0 text-slate-600">{hms}</span>
      <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${LEVEL_BADGE[entry.level]}`}>
        {entry.level}
      </span>
      <span className="shrink-0 truncate text-slate-400" title={entry.serviceId}>
        {entry.serviceId}
      </span>
      <span className={`min-w-0 flex-1 whitespace-pre-wrap break-words ${LEVEL_TONE[entry.level]}`}>
        {entry.line}
      </span>
    </li>
  )
}

function pad(n: number, w = 2): string {
  return String(n).padStart(w, '0')
}
