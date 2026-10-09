// Agent service panel — run checked agent workflows on the live robot.
//
// State (/agent/{id}/state, retained) drives the header + active runs;
// actions go to /agent/{id}/control with a reply_to topic (dispatch below);
// a followed run streams /agent/{id}/runs/{run}/events live.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Play, Square, RefreshCw, ChevronDown, ChevronRight, AlertTriangle } from 'lucide-react'
import type { ServiceProxy, InboundFrame } from '@rlx/ui'
import { useWsClient } from '@rlx/ui'

interface LiveRun {
  run_id: string; workflow: string; agent: string; status: string; phase: string
  phase_detail?: string | null; started_at: string; events: number; tool_calls: number; last_message: string
}
interface RunSummary {
  run_id: string; workflow: string; agent?: string; status: string; error?: string | null
  checks_passed?: number; checks_total?: number; cost_usd?: number | null; duration_s?: number
  ended_at?: string | null; deliverables?: Record<string, string>; phase?: string
}
interface AgentState {
  default_agent: string; residency: string; mcp_key_set: boolean; mcp_endpoint: string
  announce_topic?: string | null; halted: boolean; workflows: string[]; active_runs: LiveRun[]
}
interface WorkflowInfo {
  name: string; description?: string; error?: string
  inputs?: Record<string, { type: string; required: boolean; description?: string; auto?: boolean }>
  mcp?: Record<string, string[]>
}
interface AgentEventMsg { type: string; ts: number; data: Record<string, unknown> }

const btn = 'inline-flex items-center gap-1 rounded px-2 py-1 text-xs disabled:opacity-40'
const field = 'w-full rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 text-xs text-slate-200'
const STATUS_TONE: Record<string, string> = {
  running: 'text-sky-300', completed: 'text-emerald-300', partial: 'text-amber-300',
  failed_checks: 'text-rose-300', error: 'text-rose-300', cancelled: 'text-slate-400',
}

function ago(iso?: string | null): string {
  if (!iso) return ''
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  return s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : new Date(iso).toLocaleString()
}

function eventLine(e: AgentEventMsg): string {
  const d = e.data || {}
  switch (e.type) {
    case 'tool_call': return `tool  ${String(d.tool ?? '')}`
    case 'tool_result': return `  ↳ ${d.is_error ? 'error: ' : ''}${String(d.content ?? '').slice(0, 160)}`
    case 'command': return `run   ${String(d.command ?? '')}`
    case 'file_change': return `write ${String(d.path ?? '')}`
    case 'message': return `say   ${String(d.text ?? '').slice(0, 200)}`
    case 'usage': return `usage $${Number(d.total_cost_usd ?? 0).toFixed(3)}`
    case 'error': return `ERROR ${String(d.message ?? '')}`
    case 'completed': return 'done'
    default: return `${e.type} ${JSON.stringify(d).slice(0, 120)}`
  }
}

export default function AgentView({ proxy }: { proxy: ServiceProxy }) {
  const ws = useWsClient()
  const proxyId = proxy.id ?? proxy.name ?? ''
  const root = `/agent/${proxyId}`
  const [state, setState] = useState<AgentState | null>(null)
  const [workflows, setWorkflows] = useState<WorkflowInfo[]>([])
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [inputs, setInputs] = useState<Record<string, Record<string, string>>>({})
  const [followed, setFollowed] = useState<string | null>(null)
  const [events, setEvents] = useState<AgentEventMsg[]>([])
  const [open, setOpen] = useState<Record<string, RunSummary | null>>({})
  const [error, setError] = useState<string | null>(null)
  const counter = useRef(0)

  const dispatch = useCallback(<T,>(action: string, args: Record<string, unknown> = {}): Promise<T> => {
    const replyTo = `/cli/reply/agent-${proxyId}-${++counter.current}-${Date.now()}`
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => { off(); reject(new Error(`${action}: no reply`)) }, 30000)
      const off = ws.subscribe(replyTo, (f: InboundFrame) => {
        if (f.method !== 'message') return
        clearTimeout(timer); off()
        const p = f.payload as { error?: string } | T
        if (p && typeof p === 'object' && 'error' in (p as object) && (p as { error?: string }).error) {
          reject(new Error((p as { error: string }).error))
        } else resolve(p as T)
      })
      ws.publish(`${root}/control`, { action, ...args, reply_to: replyTo })
    })
  }, [ws, root, proxyId])

  useEffect(() => ws.subscribe(`${root}/state`, (f: InboundFrame) => {
    if (f.method === 'message' && f.payload) setState(f.payload as AgentState)
  }), [ws, root])

  const refresh = useCallback(async () => {
    try {
      setWorkflows(await dispatch<WorkflowInfo[]>('list_workflows'))
      setRuns(await dispatch<RunSummary[]>('list_runs', { limit: 20 }))
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }, [dispatch])
  useEffect(() => { void refresh() }, [refresh])

  // Refresh the run list whenever the set of active runs changes (one ended).
  const activeKey = (state?.active_runs ?? []).map((r) => r.run_id).join(',')
  useEffect(() => { void refresh() }, [activeKey, refresh])

  // Follow a run's live events (default: the newest active run).
  const target = followed ?? state?.active_runs?.[0]?.run_id ?? null
  useEffect(() => {
    setEvents([])
    if (!target) return
    return ws.subscribe(`${root}/runs/${target}/events`, (f: InboundFrame) => {
      if (f.method === 'message' && f.payload) setEvents((prev) => [...prev.slice(-199), f.payload as AgentEventMsg])
    })
  }, [ws, root, target])

  const start = async (wf: WorkflowInfo) => {
    setError(null)
    const given = Object.fromEntries(Object.entries(inputs[wf.name] ?? {}).filter(([, v]) => v !== ''))
    try {
      const r = await dispatch<{ run_id: string }>('start_run', { workflow: wf.name, inputs: given })
      setFollowed(r.run_id)
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }

  const toggleRun = async (runId: string) => {
    if (open[runId] !== undefined) { setOpen(({ [runId]: _, ...rest }) => rest); return }
    setOpen((o) => ({ ...o, [runId]: null }))
    try { const r = await dispatch<RunSummary>('get_run', { run_id: runId }); setOpen((o) => ({ ...o, [runId]: r })) }
    catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }

  const finished = useMemo(() => runs.filter((r) => r.status !== 'running'), [runs])
  const cloudBlocked = state && state.residency !== 'cloud_ok' && state.default_agent === 'claude_code'

  return (
    <div className="flex h-full flex-col gap-3 overflow-auto p-3 text-xs text-slate-200">
      {/* settings summary */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded border border-slate-800 bg-slate-900/60 px-2 py-1.5">
        <span>agent <span className="font-mono text-slate-300">{state?.default_agent ?? '…'}</span></span>
        <span>residency <span className="font-mono text-slate-300">{state?.residency ?? '…'}</span></span>
        <span>MCP key <span className={state?.mcp_key_set ? 'text-emerald-300' : 'text-amber-300'}>{state?.mcp_key_set ? 'set' : 'not set'}</span></span>
        <span className="truncate text-slate-500" title={state?.mcp_endpoint}>{state?.mcp_endpoint}</span>
        {state?.announce_topic && <span className="text-slate-500">announces on {state.announce_topic}</span>}
        <button type="button" onClick={() => void refresh()} className={`${btn} ml-auto text-slate-400 hover:text-slate-200`} title="Refresh">
          <RefreshCw size={12} />
        </button>
      </div>
      {(cloudBlocked || (state && !state.mcp_key_set) || state?.halted) && (
        <div className="flex items-start gap-1.5 rounded border border-amber-800 bg-amber-950/40 px-2 py-1.5 text-amber-200">
          <AlertTriangle size={13} className="mt-0.5 shrink-0" />
          <span>
            {state?.halted && 'Runtime is HALTED — runs are refused until you resume. '}
            {cloudBlocked && 'Claude Code sends the robot\'s state to the model: set residency to cloud_ok in this service\'s config to allow it. '}
            {state && !state.mcp_key_set && 'Set mcp_api_key (an API client\'s secret) so runs can reach the robot.'}
          </span>
        </div>
      )}
      {error && <div className="rounded border border-rose-800 bg-rose-950/40 px-2 py-1 text-rose-200">{error}</div>}

      {/* workflows */}
      <section className="space-y-1.5">
        <h3 className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Workflows</h3>
        {workflows.map((wf) => {
          const manual = Object.entries(wf.inputs ?? {}).filter(([, s]) => !s.auto)
          return (
            <div key={wf.name} className="rounded border border-slate-800 bg-slate-950/40 p-2">
              <div className="flex items-start gap-2">
                <div className="min-w-0 flex-1">
                  <div className="font-mono text-slate-100">{wf.name}</div>
                  <div className="text-slate-400">{wf.error ?? wf.description}</div>
                  {wf.mcp && Object.keys(wf.mcp).length > 0 && (
                    <div className="mt-0.5 text-[10px] text-slate-500">
                      live tools: {Object.entries(wf.mcp).map(([s, t]) => `${s}: ${t.join(', ')}`).join(' · ')}
                    </div>
                  )}
                </div>
                <button type="button" disabled={!!wf.error || !!state?.halted} onClick={() => void start(wf)}
                        className={`${btn} shrink-0 bg-emerald-600 font-medium text-white hover:bg-emerald-500`}>
                  <Play size={11} /> Run
                </button>
              </div>
              {manual.length > 0 && (
                <div className="mt-1.5 grid gap-1 sm:grid-cols-2">
                  {manual.map(([name, spec]) => (
                    <label key={name} className="space-y-0.5">
                      <span className="text-[10px] text-slate-400">{name}{spec.required ? ' *' : ''}</span>
                      <input className={field} value={inputs[wf.name]?.[name] ?? ''} placeholder={spec.description}
                             onChange={(e) => setInputs((p) => ({ ...p, [wf.name]: { ...(p[wf.name] ?? {}), [name]: e.target.value } }))} />
                    </label>
                  ))}
                </div>
              )}
            </div>
          )
        })}
        {workflows.length === 0 && <div className="text-slate-500">no workflows</div>}
      </section>

      {/* active runs */}
      {(state?.active_runs ?? []).length > 0 && (
        <section className="space-y-1.5">
          <h3 className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Running</h3>
          {state!.active_runs.map((r) => (
            <div key={r.run_id} onClick={() => setFollowed(r.run_id)}
                 className={`cursor-pointer rounded border p-2 ${target === r.run_id ? 'border-sky-700' : 'border-slate-800'} bg-slate-950/40`}>
              <div className="flex items-center gap-2">
                <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-sky-400" />
                <span className="font-mono">{r.workflow}</span>
                <span className="font-mono text-[10px] text-slate-500">{r.run_id}</span>
                <span className="text-slate-400">{r.phase}{r.phase_detail ? ` · ${r.phase_detail}` : ''}</span>
                <span className="ml-auto text-slate-500">{r.tool_calls} tool calls · started {ago(r.started_at)}</span>
                <button type="button" onClick={(e) => { e.stopPropagation(); void dispatch('cancel_run', { run_id: r.run_id }) }}
                        className={`${btn} border border-rose-900 text-rose-300 hover:bg-rose-950/50`}>
                  <Square size={11} /> Cancel
                </button>
              </div>
              {r.last_message && <div className="mt-1 truncate text-slate-400">{r.last_message}</div>}
            </div>
          ))}
        </section>
      )}

      {/* live events of the followed run */}
      {target && (
        <section className="space-y-1">
          <h3 className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Live events · {target}</h3>
          <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded border border-slate-800 bg-slate-950 p-2 font-mono text-[10px] text-slate-300">
            {events.length ? events.map(eventLine).join('\n') : 'waiting for events…'}
          </pre>
        </section>
      )}

      {/* recent runs */}
      <section className="space-y-1">
        <h3 className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Recent runs</h3>
        {finished.map((r) => {
          const detail = open[r.run_id]
          return (
            <div key={r.run_id} className="rounded border border-slate-800 bg-slate-950/40">
              <button type="button" onClick={() => void toggleRun(r.run_id)} className="flex w-full items-center gap-2 px-2 py-1 text-left">
                {detail !== undefined ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                <span className="font-mono">{r.workflow}</span>
                <span className={STATUS_TONE[r.status] ?? 'text-slate-300'}>{r.status}</span>
                {r.checks_total ? <span className="text-slate-500">checks {r.checks_passed}/{r.checks_total}</span> : null}
                {r.cost_usd != null && <span className="text-slate-500">${r.cost_usd.toFixed(3)}</span>}
                <span className="ml-auto text-slate-500">{ago(r.ended_at)}</span>
              </button>
              {detail !== undefined && (
                <div className="space-y-1 border-t border-slate-800 px-2 py-1.5">
                  {detail === null ? <div className="text-slate-500">loading…</div> : (
                    <>
                      {detail.error && <div className="whitespace-pre-wrap text-rose-300">{detail.error}</div>}
                      {Object.entries(detail.deliverables ?? {}).map(([path, text]) => (
                        <div key={path}>
                          <div className="font-mono text-[10px] text-slate-500">{path}</div>
                          <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded bg-slate-950 p-2 text-[11px] text-slate-200">{text}</pre>
                        </div>
                      ))}
                      <div className="font-mono text-[10px] text-slate-500">{r.run_id} · {detail.agent} · {detail.duration_s}s</div>
                    </>
                  )}
                </div>
              )}
            </div>
          )
        })}
        {finished.length === 0 && <div className="text-slate-500">no runs yet</div>}
      </section>
    </div>
  )
}
