// MotorControlFullView — high-level UI for the motor_control service.
//
// motor_control manages a set of *channels*, each bound to a
// motor_controller (e.g. a Sabertooth) and one of its motor channels.
// This view lets the operator:
//   * add / remove channels and bind each to a running motor_controller
//   * command a signed throttle (-1 … +1) per channel with a live
//     output readout (post-slew)
//   * set per-channel safety limits (min/max output) + slew rate
//   * invert / enable-disable a channel
//   * see a per-channel connectivity light (from the bound controller's
//     own /state) and position feedback when the controller reports it
//   * hit a big STOP button — also bound to Space / Enter while the view
//     is focused — which latches an emergency stop until Reset.
//
// The motor_controller wire contract (motor_set / motor_stop /
// motor_stop_all) is published by the service, not here; this view only
// drives the motor_control service's own control topic.
import {
  useCallback, useEffect, useMemo, useState,
  type FormEvent, type KeyboardEvent,
} from 'react'

import type { ServiceProxy } from '@rlx/ui'
import type { ServiceMeta } from '@rlx/ui'
import type { InboundFrame } from '@rlx/ui'
import type { RuntimeConnection } from '@rlx/ui'
import { useWsClient, useApiFetch, useActiveRuntime, useRuntimeConnections } from '@rlx/ui'


// The interface this service consumes — the attach dropdown is filtered
// to services whose package.yml declares ``implements: [motor_controller]``.
const MOTOR_CONTROLLER_INTERFACE = 'motor_controller'

// Live-stream input sources are discovered from running joystick
// services. Kept simple/general: any joystick proxy can drive any
// channel; the operator picks the axis (or button) index + scale.
const JOYSTICK_META_PREFIX = 'joystick@'


interface InputSourceState {
  topic: string
  field: string
  index: number
  scale: number
  offset: number
  deadzone: number
}

interface ChannelState {
  id: string
  controller_type?: string | null
  controller_id?: string | null
  motor: number
  value: number          // commanded target
  output: number         // live, post-slew output being sent
  min_output: number
  max_output: number
  slew_rate: number
  invert: boolean
  enabled: boolean
  bound: boolean
  input_source?: InputSourceState | null
}

interface JoystickCandidate {
  id: string
  name: string
  // Federation id of the runtime this joystick actually lives on, e.g.
  // "sturdy-byte" — null once resolved to mean "same runtime as this
  // motor_control instance" (kept null rather than duplicating our own
  // id so a straight `runtimeId !== myRuntimeId` check can't misfire
  // while our own id is still resolving). See runtimeId resolution in
  // refreshJoysticks below.
  runtimeId: string | null
}

// One entry from GET /v1/peers (see botgear/services/peer_service.py) —
// only the fields this view actually reads.
interface PeerRecord {
  remote_id?: string | null
  state?: string
}

interface MotorControlState {
  estopped?: boolean
  channels?: ChannelState[]
}

interface ControllerCandidate {
  id: string
  type: string
  status: string
}

// What we glean from a bound controller's own /state topic — used for
// the per-channel connectivity light + feedback readout.
interface ControllerLiveState {
  connected?: boolean
  has_feedback?: boolean
  // Optional position/feedback map keyed by motor channel, if the
  // controller publishes one. Sabertooth doesn't (open-loop).
  feedback?: Record<string, number>
}


export default function MotorControlFullView({ proxy }: { proxy: ServiceProxy }) {
  const wsClient = useWsClient()
  const apiFetch = useApiFetch()
  // Every known runtime connection (this one included) — the joystick
  // picker below scans ALL of them, not just the one hosting this
  // motor_control instance, so a channel can be driven by a joystick on
  // a peered runtime.
  const { connections } = useRuntimeConnections()
  const { connection: activeConnection } = useActiveRuntime()
  // Our OWN runtime's federation id, once learned — used to decide
  // whether a discovered joystick is "local" (bare topic) or "remote"
  // (needs the `@<runtime-id>` federation suffix). Left null until
  // /runtime/info arrives; see JoystickCandidate.runtimeId above for why
  // a stale/unresolved id is treated as "assume remote" rather than
  // guessed.
  const myRuntimeId = activeConnection?.meta.runtime_id ?? null
  const proxyId = proxy.id ?? proxy.name ?? ''
  const stateTopic = `/motor_control/${proxyId}/state`
  const controlTopic = `/motor_control/${proxyId}/control`

  const [state, setState] = useState<MotorControlState>({})
  const [candidates, setCandidates] = useState<ControllerCandidate[]>([])
  const [joysticks, setJoysticks] = useState<JoystickCandidate[]>([])
  // Federation ids this runtime currently has a live (state=connected)
  // peer bridge to, from GET /v1/peers on the ACTIVE connection. A
  // joystick candidate on a runtime NOT in this set would bind fine
  // (set_input doesn't validate reachability) but sit silently idle
  // until a bridge exists — surfaced as a warning rather than blocked,
  // since the peer link can come up moments later on its own (mDNS
  // auto-connect) with no action needed here.
  const [peerLinks, setPeerLinks] = useState<Set<string>>(new Set())
  const [candidatesError, setCandidatesError] = useState<string | null>(null)
  // Per-controller live state keyed by `${type}/${id}`.
  const [controllerStates, setControllerStates] = useState<Record<string, ControllerLiveState>>({})

  const sendAction = useCallback(
    (payload: Record<string, unknown>) => { wsClient.publish(controlTopic, payload) },
    [controlTopic, wsClient],
  )

  // ─── subscribe to our /state ───────────────────────────────────────
  useEffect(() => {
    if (!proxyId) return
    const off = wsClient.subscribe(stateTopic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      setState(f.payload as MotorControlState)
    })
    return off
  }, [proxyId, stateTopic, wsClient])

  const channels = useMemo(() => state.channels ?? [], [state.channels])

  // ─── subscribe to each bound controller's /state ───────────────────
  // Distinct controllers across all channels → one subscription each.
  const boundControllers = useMemo(() => {
    const seen = new Map<string, { type: string; id: string }>()
    for (const c of channels) {
      if (c.controller_type && c.controller_id) {
        seen.set(`${c.controller_type}/${c.controller_id}`, { type: c.controller_type, id: c.controller_id })
      }
    }
    return Array.from(seen.values())
  }, [channels])

  useEffect(() => {
    if (boundControllers.length === 0) return
    const offs = boundControllers.map(({ type, id }) => {
      const key = `${type}/${id}`
      return wsClient.subscribe(`/${type}/${id}/state`, (f: InboundFrame) => {
        if (f.method !== 'message') return
        const p = (f.payload ?? {}) as ControllerLiveState
        setControllerStates((prev) => ({ ...prev, [key]: p }))
      })
    })
    return () => { offs.forEach((off) => off()) }
    // Re-subscribe when the SET of bound controllers changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boundControllers.map((c) => `${c.type}/${c.id}`).join('|'), wsClient])

  // ─── fetch motor_controller candidates (every 5s) ──────────────────
  // motor_controller attach stays scoped to THIS runtime — a channel's
  // hardware binding (controller_type/controller_id) has no federation
  // suffix in the data model, unlike the input_source topic below.
  const refreshCandidates = useCallback(async () => {
    try {
      const [metas, proxies] = await Promise.all([
        apiFetch<ServiceMeta[]>('/v1/service-meta-list'),
        apiFetch<ServiceProxy[]>('/v1/service-proxy-list'),
      ])
      const typeForMeta = new Map<string, string>()
      const compatible = new Set<string>()
      for (const m of metas) {
        const impls = Array.isArray(m.implements) ? m.implements : []
        if (impls.includes(MOTOR_CONTROLLER_INTERFACE)) {
          const id = `${m.name}@${m.version}`
          compatible.add(id)
          typeForMeta.set(id, m.name)
        }
      }
      const found: ControllerCandidate[] = []
      for (const p of proxies) {
        const pid = p.id ?? p.name
        if (!pid) continue
        if (compatible.has(p.service_meta_id)) {
          found.push({
            id: pid,
            type: typeForMeta.get(p.service_meta_id) ?? p.service_meta_id.split('@')[0],
            status: p.status ?? 'unknown',
          })
        }
      }
      setCandidates(found)
      setCandidatesError(null)
    } catch (e) {
      setCandidatesError(e instanceof Error ? e.message : String(e))
    }
  }, [apiFetch])

  useEffect(() => {
    refreshCandidates()
    const t = setInterval(refreshCandidates, 5000)
    return () => clearInterval(t)
  }, [refreshCandidates])

  // ─── fetch joystick candidates ACROSS every connected runtime ──────
  // Unlike motor_controller attach above, an input_source binding is
  // just a bus topic string — the federation bridge (`@<runtime-id>`
  // suffix) makes any joystick on any peered runtime reachable, so the
  // picker scans every RuntimeConnection the UI knows about, not only
  // the one hosting this motor_control instance. One dead/unreachable
  // connection is skipped (try/catch per-connection) rather than
  // failing the whole refresh.
  const refreshJoysticks = useCallback(async () => {
    const sticks: JoystickCandidate[] = []
    await Promise.all(
      connections.map(async (conn: RuntimeConnection) => {
        // A connection whose federation id hasn't resolved yet can't be
        // addressed with an `@<runtime-id>` suffix (and might BE us,
        // pre-resolution) — skip it this tick; the 5s poll picks it up
        // once /runtime/info arrives.
        const runtimeId = conn.meta.runtime_id
        if (!runtimeId) return
        try {
          const proxies = await conn.apiFetch<ServiceProxy[]>('/v1/service-proxy-list')
          for (const p of proxies) {
            if (!(p.service_meta_id ?? '').startsWith(JOYSTICK_META_PREFIX)) continue
            const pid = p.id ?? p.name
            if (!pid) continue
            sticks.push({
              id: pid,
              name: p.name ?? pid,
              runtimeId: runtimeId === myRuntimeId ? null : runtimeId,
            })
          }
        } catch {
          // Unreachable / unauthenticated connection — just omit its
          // joysticks this tick, don't blow up the whole refresh.
        }
      }),
    )
    setJoysticks(sticks)
  }, [connections, myRuntimeId])

  useEffect(() => {
    refreshJoysticks()
    const t = setInterval(refreshJoysticks, 5000)
    return () => clearInterval(t)
  }, [refreshJoysticks])

  // ─── fetch this runtime's live federation peer links ───────────────
  // Informs the "peer not connected" warning in InputSourceRow — a
  // binding to an unlinked runtime still gets SAVED (set_input doesn't
  // validate reachability), it just sits idle until a bridge exists.
  const refreshPeerLinks = useCallback(async () => {
    try {
      const peers = await apiFetch<PeerRecord[]>('/v1/peers')
      const linked = new Set<string>()
      for (const p of peers) {
        if (p.remote_id && p.state === 'connected') linked.add(p.remote_id)
      }
      setPeerLinks(linked)
    } catch {
      // Peers endpoint is Admin-only — a non-admin viewer simply sees no
      // warnings rather than an error.
    }
  }, [apiFetch])

  useEffect(() => {
    refreshPeerLinks()
    const t = setInterval(refreshPeerLinks, 5000)
    return () => clearInterval(t)
  }, [refreshPeerLinks])

  // ─── emergency stop ────────────────────────────────────────────────
  const estopped = !!state.estopped
  const onStopAll = useCallback(() => sendAction({ action: 'stop_all' }), [sendAction])
  const onClearEstop = useCallback(() => sendAction({ action: 'clear_estop' }), [sendAction])

  // Space / Enter = STOP while the view is focused — but NOT when the
  // operator is typing in a form control or tabbing through buttons
  // (Space/Enter activate those natively, hijacking would be hostile).
  const onKeyDown = useCallback((e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== ' ' && e.key !== 'Enter') return
    const tag = (e.target as HTMLElement)?.tagName
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'BUTTON') return
    e.preventDefault()
    e.stopPropagation()
    onStopAll()
  }, [onStopAll])

  return (
    <div
      tabIndex={0}
      onKeyDown={onKeyDown}
      onPointerDown={(e) => e.stopPropagation()}
      className="flex min-w-[520px] flex-col gap-3 p-3 text-xs outline-none focus:ring-1 focus:ring-slate-600 rounded"
    >
      {/* ── emergency stop banner / button ─────────────────────────── */}
      <section
        className={`flex items-center justify-between gap-3 rounded border p-2 ${
          estopped ? 'border-rose-600 bg-rose-950/50' : 'border-slate-800 bg-slate-900/40'
        }`}
      >
        <div className="flex flex-col">
          {estopped ? (
            <span className="font-mono text-[11px] font-bold uppercase tracking-wider text-rose-300">
              ⛔ E-STOPPED — motion blocked
            </span>
          ) : (
            <span className="font-mono text-[10px] text-slate-500">
              Space / Enter also triggers STOP when this view is focused
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {estopped && (
            <button
              type="button" onClick={onClearEstop} onPointerDown={(e) => e.stopPropagation()}
              className="nodrag nopan rounded border border-slate-600 px-3 py-1 text-[11px] text-slate-200 hover:border-slate-400"
            >Reset</button>
          )}
          <button
            type="button" onClick={onStopAll} onPointerDown={(e) => e.stopPropagation()}
            className="nodrag nopan rounded bg-rose-600 px-5 py-2 text-sm font-bold uppercase tracking-wide text-white hover:bg-rose-500"
          >■ STOP</button>
        </div>
      </section>

      {/* ── add channel ────────────────────────────────────────────── */}
      <AddChannel
        candidates={candidates}
        candidatesError={candidatesError}
        existingIds={channels.map((c) => c.id)}
        onRefresh={refreshCandidates}
        onAdd={(payload) => sendAction({ action: 'add_channel', ...payload })}
      />

      {/* ── channels ───────────────────────────────────────────────── */}
      {channels.length === 0 ? (
        <div className="rounded border border-slate-800 bg-slate-950/70 p-3 text-slate-500">
          No channels yet. Add one above and bind it to a running motor_controller (e.g. a Sabertooth).
        </div>
      ) : (
        channels.map((ch) => (
          <ChannelCard
            key={ch.id}
            ch={ch}
            estopped={estopped}
            joysticks={joysticks}
            myRuntimeId={myRuntimeId}
            peerLinks={peerLinks}
            controllerState={
              ch.controller_type && ch.controller_id
                ? controllerStates[`${ch.controller_type}/${ch.controller_id}`]
                : undefined
            }
            onSet={(v) => sendAction({ action: 'set', id: ch.id, value: v })}
            onStop={() => sendAction({ action: 'stop', id: ch.id })}
            onRemove={() => sendAction({ action: 'remove_channel', id: ch.id })}
            onSetLimits={(lo, hi, slew) => sendAction({ action: 'set_limits', id: ch.id, min_output: lo, max_output: hi, slew_rate: slew })}
            onUpdate={(patch) => sendAction({ action: 'update_channel', id: ch.id, ...patch })}
            onSetInput={(src) => sendAction({ action: 'set_input', id: ch.id, ...src })}
            onClearInput={() => sendAction({ action: 'clear_input', id: ch.id })}
          />
        ))
      )}
    </div>
  )
}


// ─────────────────────────────────────────────────────────────────────
// Add-channel form
// ─────────────────────────────────────────────────────────────────────

function AddChannel({
  candidates, candidatesError, existingIds, onRefresh, onAdd,
}: {
  candidates: ControllerCandidate[]
  candidatesError: string | null
  existingIds: string[]
  onRefresh: () => void
  onAdd: (payload: { id: string; controller_type: string; controller_id: string; motor: number }) => void
}) {
  const [label, setLabel] = useState('')
  const [controller, setController] = useState('')
  const [motor, setMotor] = useState('1')

  useEffect(() => {
    if (!controller && candidates.length > 0) setController(candidates[0].id)
  }, [candidates, controller])

  const dupId = existingIds.includes(label.trim())
  const valid = label.trim().length > 0 && !dupId && !!controller

  const submit = (e?: FormEvent) => {
    e?.preventDefault()
    if (!valid) return
    const c = candidates.find((x) => x.id === controller)
    if (!c) return
    onAdd({ id: label.trim(), controller_type: c.type, controller_id: c.id, motor: Number.parseInt(motor, 10) || 1 })
    setLabel('')
    setMotor('1')
  }

  return (
    <Section title="Add channel">
      <form className="flex flex-wrap items-end gap-2" onSubmit={submit} onPointerDown={(e) => e.stopPropagation()}>
        <label className="flex flex-col">
          <span className="mb-0.5 text-[10px] uppercase tracking-wider text-slate-500">label</span>
          <input
            type="text" value={label} placeholder="e.g. left"
            onChange={(e) => setLabel(e.target.value)}
            onPointerDown={(e) => e.stopPropagation()}
            className="nodrag nopan w-28 rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-100 focus:border-slate-500 focus:outline-none"
          />
        </label>
        <label className="flex flex-col">
          <span className="mb-0.5 text-[10px] uppercase tracking-wider text-slate-500">controller</span>
          <div className="flex items-center gap-1">
            <select
              value={controller} onChange={(e) => setController(e.target.value)}
              disabled={candidates.length === 0}
              onPointerDown={(e) => e.stopPropagation()}
              className="nodrag nopan w-52 rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-200 focus:border-slate-500 focus:outline-none disabled:opacity-50"
            >
              {candidates.length === 0 && <option value="">(no motor_controller services running)</option>}
              {candidates.map((c) => (
                <option key={c.id} value={c.id}>{c.id} — {c.type} · {c.status}</option>
              ))}
            </select>
            <button
              type="button" onClick={onRefresh} onPointerDown={(e) => e.stopPropagation()}
              className="nodrag nopan rounded border border-slate-700 px-2 py-1 text-[11px] hover:border-slate-500"
            >↻</button>
          </div>
        </label>
        <label className="flex flex-col">
          <span className="mb-0.5 text-[10px] uppercase tracking-wider text-slate-500">motor</span>
          <input
            type="number" min={1} value={motor}
            onChange={(e) => setMotor(e.target.value)}
            onPointerDown={(e) => e.stopPropagation()}
            className="nodrag nopan w-16 rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-100 focus:border-slate-500 focus:outline-none"
          />
        </label>
        <button
          type="submit" disabled={!valid}
          onPointerDown={(e) => e.stopPropagation()}
          className="nodrag nopan rounded bg-emerald-600 px-3 py-1 text-[11px] font-medium text-white hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-40"
        >Add</button>
        {dupId && <span className="text-amber-300">id already in use</span>}
      </form>
      {candidatesError && (
        <div className="mt-2 rounded border border-rose-700 bg-rose-950/40 px-2 py-1 font-mono text-[10px] text-rose-200">
          could not load controllers — {candidatesError}
        </div>
      )}
      {candidates.length === 0 && !candidatesError && (
        <div className="mt-1 text-slate-500">
          No services declaring <span className="font-mono">implements: [motor_controller]</span> are running.
          Start a Sabertooth (or any other implementation) and click ↻.
        </div>
      )}
    </Section>
  )
}


// ─────────────────────────────────────────────────────────────────────
// One channel card — speed slider, readout, limits, flags, stop.
// ─────────────────────────────────────────────────────────────────────

function ChannelCard({
  ch, estopped, joysticks, myRuntimeId, peerLinks, controllerState, onSet, onStop, onRemove, onSetLimits, onUpdate, onSetInput, onClearInput,
}: {
  ch: ChannelState
  estopped: boolean
  joysticks: JoystickCandidate[]
  myRuntimeId: string | null
  peerLinks: Set<string>
  controllerState?: ControllerLiveState
  onSet: (v: number) => void
  onStop: () => void
  onRemove: () => void
  onSetLimits: (lo: number, hi: number, slew: number) => void
  onUpdate: (patch: Record<string, unknown>) => void
  onSetInput: (src: { topic: string; field: string; index: number; scale: number; offset: number; deadzone: number }) => void
  onClearInput: () => void
}) {
  const [draft, setDraft] = useState<number>(ch.value)
  const [dragging, setDragging] = useState(false)
  useEffect(() => { if (!dragging) setDraft(ch.value) }, [ch.value, dragging])

  // Limit drafts
  const [loDraft, setLoDraft] = useState<string>(String(ch.min_output))
  const [hiDraft, setHiDraft] = useState<string>(String(ch.max_output))
  const [slewDraft, setSlewDraft] = useState<string>(String(ch.slew_rate))
  useEffect(() => { setLoDraft(String(ch.min_output)) }, [ch.min_output])
  useEffect(() => { setHiDraft(String(ch.max_output)) }, [ch.max_output])
  useEffect(() => { setSlewDraft(String(ch.slew_rate)) }, [ch.slew_rate])
  const limitsDirty = Number(loDraft) !== ch.min_output || Number(hiDraft) !== ch.max_output || Number(slewDraft) !== ch.slew_rate

  const connected = !!controllerState?.connected
  const hasFeedback = !!controllerState?.has_feedback
  const feedback = controllerState?.feedback?.[String(ch.motor)]

  const outputPct = Math.round(ch.output * 100)
  const targetPct = Math.round(ch.value * 100)
  const inputBound = !!ch.input_source
  // The slider is a read-only readout while a live input stream drives
  // the channel — the operator can't fight the joystick with the slider.
  const disabled = estopped || !ch.enabled || inputBound
  // The slider track spans the channel's own limit window so the
  // operator can't drag past a configured safety bound.
  const sliderMin = ch.min_output
  const sliderMax = ch.max_output
  const draftPct = Math.round(draft * 100)
  // Step from the displayed percent, not draft*100 + delta/100, so
  // repeated clicks can't drift off-integer from float accumulation.
  const step = (delta: number) => {
    const v = Math.max(sliderMin, Math.min(sliderMax, (draftPct + delta) / 100))
    setDraft(v)
    onSet(v)
  }

  return (
    <section className={`rounded border p-3 ${ch.enabled ? 'border-slate-800 bg-slate-900/40' : 'border-slate-800 bg-slate-950/60 opacity-70'}`}>
      {/* header */}
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className={`inline-block h-2.5 w-2.5 rounded-full ${ch.bound ? (connected ? 'bg-emerald-400 shadow-[0_0_6px_1px] shadow-emerald-500/60' : 'bg-amber-500') : 'bg-slate-600'}`}
            title={ch.bound ? (connected ? 'controller connected' : 'controller offline') : 'unbound'} />
          <span className="font-mono text-[12px] font-semibold text-slate-200">{ch.id}</span>
          <span className="font-mono text-[10px] text-slate-500">
            {ch.bound ? `${ch.controller_type}/${ch.controller_id} · m${ch.motor}` : 'unbound'}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1 font-mono text-[10px] text-slate-400">
            <input
              type="checkbox" checked={ch.enabled}
              onChange={(e) => onUpdate({ enabled: e.target.checked })}
              onPointerDown={(e) => e.stopPropagation()}
              className="nodrag nopan accent-emerald-500"
            />enabled
          </label>
          <label className="flex items-center gap-1 font-mono text-[10px] text-slate-400">
            <input
              type="checkbox" checked={ch.invert}
              onChange={(e) => onUpdate({ invert: e.target.checked })}
              onPointerDown={(e) => e.stopPropagation()}
              className="nodrag nopan accent-sky-500"
            />invert
          </label>
          <button
            type="button" onClick={onRemove} onPointerDown={(e) => e.stopPropagation()}
            title="Remove channel"
            className="nodrag nopan rounded border border-slate-700 px-1.5 py-0.5 text-[10px] text-slate-400 hover:border-rose-500 hover:text-rose-300"
          >✕</button>
        </div>
      </div>

      {/* speed slider */}
      <div className="flex items-center gap-3">
        <span className="w-10 text-right font-mono text-[10px] text-slate-500">{Math.round(sliderMin * 100)}%</span>
        <button
          type="button" onClick={() => step(-1)}
          onPointerDown={(e) => e.stopPropagation()}
          disabled={disabled || draftPct <= Math.round(sliderMin * 100)}
          aria-label={`${ch.id} decrease 1%`}
          className="nodrag nopan rounded border border-slate-700 p-0.5 text-slate-400 hover:border-emerald-500 hover:text-emerald-300 disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:border-slate-700 disabled:hover:text-slate-400"
        >&lt;</button>
        <input
          type="range" min={sliderMin} max={sliderMax} step={0.01} value={draft}
          disabled={disabled}
          className="nodrag nopan flex-1 accent-emerald-500 disabled:cursor-not-allowed disabled:opacity-50"
          onPointerDown={() => setDragging(true)}
          onPointerUp={() => setDragging(false)}
          onChange={(e) => { const v = Number(e.target.value); setDraft(v); onSet(v) }}
        />
        <button
          type="button" onClick={() => step(1)}
          onPointerDown={(e) => e.stopPropagation()}
          disabled={disabled || draftPct >= Math.round(sliderMax * 100)}
          aria-label={`${ch.id} increase 1%`}
          className="nodrag nopan rounded border border-slate-700 p-0.5 text-slate-400 hover:border-emerald-500 hover:text-emerald-300 disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:border-slate-700 disabled:hover:text-slate-400"
        >&gt;</button>
        <span className="w-10 font-mono text-[10px] text-slate-500">{Math.round(sliderMax * 100)}%</span>
        <button
          type="button" onClick={() => { setDraft(0); onStop() }}
          onPointerDown={(e) => e.stopPropagation()}
          className="nodrag nopan rounded border border-slate-700 px-2 py-1 text-[10px] hover:border-rose-500 hover:text-rose-300"
        >stop</button>
      </div>

      {/* readout */}
      <div className="mt-1 flex items-center gap-4 font-mono text-[10px] text-slate-400">
        <span>target <span className={targetPct === 0 ? 'text-slate-400' : 'text-slate-200'}>{targetPct > 0 ? '+' : ''}{targetPct}%</span></span>
        <span>output <span className={outputPct === 0 ? 'text-slate-500' : outputPct > 0 ? 'text-emerald-300' : 'text-amber-300'}>{outputPct > 0 ? '+' : ''}{outputPct}%</span>
          {ch.value !== ch.output && <span className="ml-1 animate-pulse text-amber-400/70">ramping</span>}
        </span>
        <span className="ml-auto">
          {!ch.bound ? <span className="text-slate-600">no controller</span>
            : hasFeedback ? <span>fb {typeof feedback === 'number' ? `${Math.round(feedback * 100)}%` : '—'}</span>
            : <span className="text-slate-600">open-loop (no feedback)</span>}
        </span>
      </div>

      {/* limits */}
      <div className="mt-2 flex flex-wrap items-end gap-2 border-t border-slate-800 pt-2">
        <LimitInput label="min" value={loDraft} onChange={setLoDraft} />
        <LimitInput label="max" value={hiDraft} onChange={setHiDraft} />
        <LimitInput label="slew /s" value={slewDraft} onChange={setSlewDraft} min={0} step={0.05} />
        <button
          type="button" disabled={!limitsDirty}
          onClick={() => {
            const lo = Number(loDraft), hi = Number(hiDraft), slew = Number(slewDraft)
            if (!Number.isNaN(lo) && !Number.isNaN(hi) && !Number.isNaN(slew)) onSetLimits(lo, hi, Math.max(0, slew))
          }}
          onPointerDown={(e) => e.stopPropagation()}
          className="nodrag nopan rounded bg-amber-600 px-2 py-1 text-[10px] font-medium text-white hover:bg-amber-500 disabled:cursor-not-allowed disabled:opacity-40"
        >Apply limits</button>
        <span className="text-[10px] text-slate-500">slew 0 = instant</span>
      </div>

      {/* input source */}
      <InputSourceRow
        ch={ch}
        joysticks={joysticks}
        myRuntimeId={myRuntimeId}
        peerLinks={peerLinks}
        onSetInput={onSetInput}
        onClearInput={onClearInput}
      />
    </section>
  )
}


// ─────────────────────────────────────────────────────────────────────
// Per-channel input-source row — bind a live joystick axis to the target
// ─────────────────────────────────────────────────────────────────────

// Composite key for the stick <select> — a bare proxy id (e.g.
// "joystick-1") isn't unique across runtimes, so candidates are keyed
// by "<runtimeId>::<id>" (local candidates use "" for runtimeId).
function stickKey(j: Pick<JoystickCandidate, 'id' | 'runtimeId'>): string {
  return `${j.runtimeId ?? ''}::${j.id}`
}

// Pull the `@<runtime-id>` federation suffix off an already-bound
// topic, if any, so the bound-state display can flag an unlinked peer
// the same way the picker does. Mirrors bus.py's parse_id_suffix.
function peerSuffixOf(topic: string): string | null {
  const at = topic.lastIndexOf('@')
  if (at < 0) return null
  const suffix = topic.slice(at + 1)
  return /^[a-z][a-z0-9-]{1,62}$/.test(suffix) ? suffix : null
}

function InputSourceRow({
  ch, joysticks, myRuntimeId, peerLinks, onSetInput, onClearInput,
}: {
  ch: ChannelState
  joysticks: JoystickCandidate[]
  myRuntimeId: string | null
  peerLinks: Set<string>
  onSetInput: (src: { topic: string; field: string; index: number; scale: number; offset: number; deadzone: number }) => void
  onClearInput: () => void
}) {
  const src = ch.input_source
  const [stick, setStick] = useState('')
  const [field, setField] = useState('axes')
  const [index, setIndex] = useState('0')
  const [scale, setScale] = useState('1')
  const [deadzone, setDeadzone] = useState('0.05')

  useEffect(() => {
    if (!stick && joysticks.length > 0) setStick(stickKey(joysticks[0]))
  }, [joysticks, stick])

  const picked = useMemo(
    () => joysticks.find((j) => stickKey(j) === stick),
    [joysticks, stick],
  )
  // A candidate is "remote" when it's tagged with a runtime id that
  // isn't ours — null means "same runtime as this motor_control
  // instance" (see JoystickCandidate.runtimeId).
  const pickedUnlinked = !!(picked?.runtimeId && !peerLinks.has(picked.runtimeId))

  if (src) {
    const boundPeer = peerSuffixOf(src.topic)
    const boundUnlinked = !!(boundPeer && boundPeer !== myRuntimeId && !peerLinks.has(boundPeer))
    return (
      <div className="mt-2 flex items-center gap-2 border-t border-slate-800 pt-2 font-mono text-[10px]">
        <span className="inline-block h-2 w-2 rounded-full bg-sky-400 shadow-[0_0_5px_1px] shadow-sky-500/60" title="input stream bound" />
        <span className="text-sky-300">live input</span>
        <span className="text-slate-400">
          {src.topic} · {src.field}[{src.index}] × {src.scale}
          {src.offset ? ` ${src.offset >= 0 ? '+' : ''}${src.offset}` : ''}
          {src.deadzone ? ` · dz ${src.deadzone}` : ''}
        </span>
        {boundUnlinked && (
          <span className="text-amber-400" title={`No live federation link to '${boundPeer}' — this binding is idle until one connects.`}>
            ⚠ peer not connected
          </span>
        )}
        <button
          type="button" onClick={onClearInput} onPointerDown={(e) => e.stopPropagation()}
          title="Unbind input — return to manual slider control"
          className="nodrag nopan ml-auto rounded border border-slate-700 px-2 py-0.5 text-slate-400 hover:border-rose-500 hover:text-rose-300"
        >✕ unbind</button>
      </div>
    )
  }

  const valid = !!picked
  const submit = () => {
    if (!picked) return
    const suffix = picked.runtimeId && picked.runtimeId !== myRuntimeId ? `@${picked.runtimeId}` : ''
    onSetInput({
      topic: `/joystick/${picked.id}/input${suffix}`,
      field,
      index: Number.parseInt(index, 10) || 0,
      scale: Number(scale) || 1,
      offset: 0,
      deadzone: Math.max(0, Number(deadzone) || 0),
    })
  }

  return (
    <div className="mt-2 flex flex-col gap-2 border-t border-slate-800 pt-2">
      <div className="flex flex-wrap items-end gap-2">
        <span className="text-[10px] uppercase tracking-wider text-slate-500">input source</span>
        <select
          value={stick} onChange={(e) => setStick(e.target.value)}
          disabled={joysticks.length === 0}
          onPointerDown={(e) => e.stopPropagation()}
          className="nodrag nopan w-52 rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-200 focus:border-slate-500 focus:outline-none disabled:opacity-50"
        >
          {joysticks.length === 0 && <option value="">(no joystick running)</option>}
          {joysticks.map((j) => (
            <option key={stickKey(j)} value={stickKey(j)}>
              {j.id}{j.name && j.name !== j.id ? ` — ${j.name}` : ''}
              {j.runtimeId ? ` @${j.runtimeId}${peerLinks.has(j.runtimeId) ? '' : ' (peer not connected)'}` : ''}
            </option>
          ))}
        </select>
        <select
          value={field} onChange={(e) => setField(e.target.value)}
          onPointerDown={(e) => e.stopPropagation()}
          className="nodrag nopan rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-200 focus:border-slate-500 focus:outline-none"
        >
          <option value="axes">axis</option>
          <option value="buttons">button</option>
          <option value="hats">hat</option>
        </select>
        <SmallNum label="index" value={index} onChange={setIndex} min={0} step={1} width="w-14" />
        <SmallNum label="scale" value={scale} onChange={setScale} step={0.1} width="w-16" />
        <SmallNum label="deadzone" value={deadzone} onChange={setDeadzone} min={0} step={0.01} width="w-16" />
        <button
          type="button" disabled={!valid} onClick={submit}
          onPointerDown={(e) => e.stopPropagation()}
          className="nodrag nopan rounded bg-sky-600 px-2 py-1 text-[10px] font-medium text-white hover:bg-sky-500 disabled:cursor-not-allowed disabled:opacity-40"
        >Bind input</button>
      </div>
      {pickedUnlinked && (
        <span className="font-mono text-[10px] text-amber-400">
          ⚠ no live federation link from this runtime to '{picked?.runtimeId}' yet — binding will save but sit idle until one connects (mDNS auto-connect usually brings it up on its own within a few seconds).
        </span>
      )}
    </div>
  )
}


function SmallNum({
  label, value, onChange, min, step = 1, width = 'w-16',
}: {
  label: string
  value: string
  onChange: (s: string) => void
  min?: number
  step?: number
  width?: string
}) {
  return (
    <label className="flex flex-col gap-0.5">
      <span className="text-[10px] uppercase tracking-wider text-slate-500">{label}</span>
      <input
        type="number" min={min} step={step} value={value}
        onChange={(e) => onChange(e.target.value)}
        onPointerDown={(e) => e.stopPropagation()}
        className={`nodrag nopan ${width} rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-100 focus:border-slate-500 focus:outline-none`}
      />
    </label>
  )
}


function LimitInput({
  label, value, onChange, min = -1, step = 0.05,
}: {
  label: string
  value: string
  onChange: (s: string) => void
  min?: number
  step?: number
}) {
  return (
    <label className="flex flex-col gap-0.5">
      <span className="text-[10px] uppercase tracking-wider text-slate-500">{label}</span>
      <input
        type="number" min={min} max={1} step={step} value={value}
        onChange={(e) => onChange(e.target.value)}
        onPointerDown={(e) => e.stopPropagation()}
        className="nodrag nopan w-20 rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-100 focus:border-slate-500 focus:outline-none"
      />
    </label>
  )
}


function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded border border-slate-800 bg-slate-900/40 p-3">
      <h3 className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-slate-400">{title}</h3>
      {children}
    </section>
  )
}
