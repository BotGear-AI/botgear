// LcusRelayFullView — driver UI for the lcus_relay service (LCUS-1/2/4/8
// USB relay boards, e.g. the NOYITO 8-channel module).
//
//   1. Connection — serial port picker (dropdown *and* a free-text
//      override, since port auto-detection doesn't always match what
//      the OS actually named the device), baud, channel count
//      (LCUS-1/2/4/8), Connect / Disconnect, connectivity light.
//   2. Relays — one toggle button per channel + All on / All off +
//      a best-effort "query hardware" refresh (not every clone board
//      answers the 0xFF status query).
//
// This is a leaf device with no promoted capability interface — the
// wire actions (relay_set / relay_toggle / relay_all) are this
// service's own contract.
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Loader2, Power, RefreshCw } from 'lucide-react'

import type { ServiceProxy } from '@rlx/ui'
import type { InboundFrame } from '@rlx/ui'
import { useWsClient } from '@rlx/ui'
import { useServiceRequest } from '@rlx/ui'


const BAUD_OPTIONS = [2400, 4800, 9600, 19200, 38400, 57600, 115200]
const CHANNEL_COUNT_OPTIONS = [1, 2, 4, 8]


interface PortInfo {
  device: string
  description?: string
  hwid?: string
  holders?: { proxy_id?: string | null; pid: number; service_type?: string | null; name?: string | null }[]
}

interface LcusRelayState {
  connected?: boolean
  port?: string | null
  baudrate?: number
  channel_count?: number
  command_spacing_ms?: number
  channels?: number[]
  relays?: Record<string, boolean>
  ports?: PortInfo[]
  last_port?: string | null
  last_baud?: number
  autoreconnect?: boolean
  retrying?: boolean
  errors?: number
  last_error?: string | null
  connected_at?: string | null
}


export default function LcusRelayFullView({ proxy }: { proxy: ServiceProxy }) {
  const wsClient = useWsClient()
  const proxyId = proxy.id ?? proxy.name ?? ''
  const stateTopic = `/lcus_relay/${proxyId}/state`
  const controlTopic = `/lcus_relay/${proxyId}/control`

  const [state, setState] = useState<LcusRelayState>({})

  // Connect-form drafts (separate from authoritative state). ``portDraft``
  // is a free-text field — the dropdown is just a convenience that fills
  // it in, so a device the auto-scan mis-names or misses can still be
  // typed directly (e.g. /dev/ttyUSB2, COM7).
  const [portDraft, setPortDraft] = useState<string>('')
  const [baudDraft, setBaudDraft] = useState<number>(9600)
  const [channelCountDraft, setChannelCountDraft] = useState<number>(8)

  // ─── subscribe to /state ───────────────────────────────────────────
  useEffect(() => {
    if (!proxyId) return
    const off = wsClient.subscribe(stateTopic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      setState(f.payload as LcusRelayState)
    })
    return off
  }, [proxyId, stateTopic, wsClient])

  // Pre-fill connect drafts from state while not connected.
  useEffect(() => {
    if (state.connected) return
    if (state.last_port && !portDraft) setPortDraft(state.last_port)
    if (state.last_baud) setBaudDraft(state.last_baud)
    if (typeof state.channel_count === 'number') setChannelCountDraft(state.channel_count)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.last_port, state.last_baud, state.channel_count, state.connected])

  // ─── actions ───────────────────────────────────────────────────────
  const sendAction = useCallback(
    (payload: Record<string, unknown>) => { wsClient.publish(controlTopic, payload) },
    [controlTopic, wsClient],
  )
  const connectRequest = useServiceRequest<LcusRelayState>(controlTopic, {
    timeoutMs: 10_000, errorField: 'last_error', replyPrefix: `lcus_relay-${proxyId}-connect`,
  })
  const disconnectRequest = useServiceRequest<LcusRelayState>(controlTopic, {
    timeoutMs: 5_000, errorField: 'last_error', replyPrefix: `lcus_relay-${proxyId}-disconnect`,
  })
  const queryRequest = useServiceRequest<LcusRelayState>(controlTopic, {
    timeoutMs: 3_000, errorField: 'last_error', replyPrefix: `lcus_relay-${proxyId}-query`,
  })

  const onConnect = useCallback((e?: FormEvent) => {
    e?.preventDefault()
    if (!portDraft || connectRequest.inFlight) return
    void connectRequest.request('connect', {
      port: portDraft, baudrate: baudDraft, channel_count: channelCountDraft,
    })
  }, [portDraft, baudDraft, channelCountDraft, connectRequest])

  const onDisconnect = useCallback(() => {
    if (disconnectRequest.inFlight) return
    void disconnectRequest.request('disconnect')
  }, [disconnectRequest])

  const onRefreshPorts = useCallback(() => sendAction({ action: 'list_ports' }), [sendAction])

  const onToggleAutoConnect = useCallback(
    (enabled: boolean) => sendAction({ action: 'set_auto_connect', enabled }),
    [sendAction],
  )

  const onQueryStatus = useCallback(() => {
    if (queryRequest.inFlight) return
    void queryRequest.request('query_status')
  }, [queryRequest])

  // Clear stale client-side errors the moment /state reports a fresh
  // connection, same as sabertooth — a late reply or an autoreconnect
  // shouldn't leave an old timeout message on screen.
  const wasConnectedRef = useRef(false)
  useEffect(() => {
    if (state.connected && !wasConnectedRef.current) {
      connectRequest.clearError()
      disconnectRequest.clearError()
    }
    wasConnectedRef.current = !!state.connected
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.connected])

  const connected = !!state.connected
  const retrying = !!state.retrying
  const channels = state.channels ?? CHANNEL_COUNT_OPTIONS_FALLBACK(state.channel_count)
  const relays = state.relays ?? {}
  const error = connectRequest.error ?? disconnectRequest.error ?? queryRequest.error ?? state.last_error

  const ports = state.ports ?? []
  const portDevices = new Set(ports.map((p) => p.device))

  return (
    <div
      className="flex min-w-[420px] flex-col gap-3 p-3 text-xs"
      onPointerDown={(e) => e.stopPropagation()}
    >
      {/* ── connection ─────────────────────────────────────────────── */}
      <Section title="Connection">
        <form onSubmit={onConnect} className="flex flex-wrap items-end gap-2">
          <label className="flex flex-col">
            <span className="mb-0.5 text-[10px] uppercase tracking-wider text-slate-500">port</span>
            <div className="flex items-center gap-1">
              {/* Free-text override — always wins, the dropdown just fills
                  it in. Auto-detection can miss/misname the CH340 device,
                  so typing a path directly must always work. */}
              <input
                type="text"
                value={portDraft}
                onChange={(e) => setPortDraft(e.target.value)}
                disabled={connected}
                placeholder="/dev/ttyUSB0"
                onPointerDown={(e) => e.stopPropagation()}
                className="nodrag nopan w-40 rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-100 focus:border-slate-500 focus:outline-none disabled:opacity-50"
              />
              <select
                value={portDevices.has(portDraft) ? portDraft : ''}
                onChange={(e) => { if (e.target.value) setPortDraft(e.target.value) }}
                disabled={connected}
                title="Detected ports — picking one fills the text field"
                onPointerDown={(e) => e.stopPropagation()}
                className="nodrag nopan w-8 rounded border border-slate-700 bg-slate-950 px-1 py-1 text-[11px] text-slate-100 focus:border-slate-500 focus:outline-none disabled:opacity-50"
              >
                <option value="">▾</option>
                {ports.map((p) => {
                  const others = (p.holders ?? []).filter((h) => h.proxy_id !== proxyId)
                  const ownedByOther = others.length > 0
                  return (
                    <option key={p.device} value={p.device} disabled={ownedByOther}>
                      {p.device}{p.description ? `  — ${p.description}` : ''}
                      {ownedByOther ? '  (in use)' : ''}
                    </option>
                  )
                })}
              </select>
              <button
                type="button" onClick={onRefreshPorts}
                onPointerDown={(e) => e.stopPropagation()} title="Refresh port list"
                className="nodrag nopan rounded border border-slate-700 p-1 hover:border-slate-500"
              ><RefreshCw className="h-3 w-3" /></button>
            </div>
          </label>
          <label className="flex flex-col">
            <span className="mb-0.5 text-[10px] uppercase tracking-wider text-slate-500">baud</span>
            <select
              value={baudDraft} onChange={(e) => setBaudDraft(Number(e.target.value))} disabled={connected}
              onPointerDown={(e) => e.stopPropagation()}
              className="nodrag nopan w-24 rounded border border-slate-700 bg-slate-950 px-1 py-1 font-mono text-[11px] text-slate-100 disabled:opacity-50"
            >
              {BAUD_OPTIONS.map((b) => <option key={b} value={b}>{b}</option>)}
            </select>
          </label>
          <label className="flex flex-col">
            <span className="mb-0.5 text-[10px] uppercase tracking-wider text-slate-500">module</span>
            <select
              value={channelCountDraft}
              onChange={(e) => {
                const n = Number(e.target.value)
                setChannelCountDraft(n)
                if (connected) sendAction({ action: 'set_channel_count', channel_count: n })
              }}
              onPointerDown={(e) => e.stopPropagation()}
              className="nodrag nopan w-24 rounded border border-slate-700 bg-slate-950 px-1 py-1 font-mono text-[11px] text-slate-100"
            >
              {CHANNEL_COUNT_OPTIONS.map((n) => <option key={n} value={n}>LCUS-{n}</option>)}
            </select>
          </label>
          <div className="ml-auto flex items-center gap-2">
            <label
              className="flex items-center gap-1.5 font-mono text-[10px] text-slate-400"
              onPointerDown={(e) => e.stopPropagation()}
            >
              <input
                type="checkbox"
                checked={!!state.autoreconnect}
                onChange={(e) => onToggleAutoConnect(e.target.checked)}
                className="nodrag nopan accent-emerald-500"
              />
              auto-connect
            </label>
            <span className="flex items-center gap-1.5 font-mono text-[10px]">
              <span
                className={`inline-block h-2.5 w-2.5 rounded-full ${
                  connected
                    ? 'bg-emerald-400 shadow-[0_0_6px_1px] shadow-emerald-500/60'
                    : retrying
                      ? 'animate-pulse bg-amber-400'
                      : 'bg-slate-600'
                }`}
              />
              <span className={connected ? 'text-emerald-300' : retrying ? 'text-amber-300' : 'text-slate-500'}>
                {connected ? 'connected' : retrying ? 'reconnecting…' : 'offline'}
              </span>
            </span>
            {!connected ? (
              <button
                type="submit" disabled={!portDraft || connectRequest.inFlight} aria-busy={connectRequest.inFlight}
                onPointerDown={(e) => e.stopPropagation()}
                className="nodrag nopan inline-flex items-center gap-1.5 rounded bg-emerald-600 px-3 py-1 text-[11px] font-medium text-white hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {connectRequest.inFlight && <Loader2 className="h-3 w-3 animate-spin" />}
                {connectRequest.inFlight ? 'Connecting…' : 'Connect'}
              </button>
            ) : (
              <button
                type="button" onClick={onDisconnect} disabled={disconnectRequest.inFlight} aria-busy={disconnectRequest.inFlight}
                onPointerDown={(e) => e.stopPropagation()}
                className="nodrag nopan inline-flex items-center gap-1.5 rounded border border-slate-700 px-3 py-1 text-[11px] hover:border-rose-500 hover:text-rose-300 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {disconnectRequest.inFlight && <Loader2 className="h-3 w-3 animate-spin" />}
                {disconnectRequest.inFlight ? 'Disconnecting…' : 'Disconnect'}
              </button>
            )}
          </div>
        </form>
        {error && (
          <div className="mt-2 truncate font-mono text-[10px] text-rose-300" title={error}>
            error: {error}
          </div>
        )}
      </Section>

      {/* ── relays ─────────────────────────────────────────────────── */}
      <Section title="Relays">
        {!connected ? (
          <div className="rounded border border-slate-800 bg-slate-950/70 p-2 text-slate-500">
            Connect to switch relays.
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            <div className="grid grid-cols-4 gap-2">
              {channels.map((ch) => (
                <RelayButton
                  key={ch}
                  channel={ch}
                  on={!!relays[String(ch)]}
                  onToggle={() => sendAction({ action: 'relay_toggle', channel: ch })}
                />
              ))}
            </div>
            <SpacingControl
              valueMs={state.command_spacing_ms ?? 30}
              onCommit={(ms) => sendAction({ action: 'set_command_spacing', command_spacing_ms: ms })}
            />
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => sendAction({ action: 'relay_all', on: true })}
                onPointerDown={(e) => e.stopPropagation()}
                className="nodrag nopan rounded bg-emerald-700 px-3 py-1.5 text-[11px] font-medium text-white hover:bg-emerald-600"
              >All on</button>
              <button
                type="button"
                onClick={() => sendAction({ action: 'relay_all', on: false })}
                onPointerDown={(e) => e.stopPropagation()}
                className="nodrag nopan rounded bg-slate-700 px-3 py-1.5 text-[11px] font-medium text-white hover:bg-slate-600"
              >All off</button>
              <button
                type="button"
                onClick={onQueryStatus}
                disabled={queryRequest.inFlight}
                aria-busy={queryRequest.inFlight}
                title="Ask the board for its live relay state (best-effort — not every clone board answers)"
                onPointerDown={(e) => e.stopPropagation()}
                className="nodrag nopan ml-auto inline-flex items-center gap-1.5 rounded border border-slate-700 px-2 py-1 text-[11px] hover:border-slate-500 disabled:opacity-40"
              >
                {queryRequest.inFlight ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
                query hardware
              </button>
            </div>
          </div>
        )}
      </Section>
    </div>
  )
}

// Fallback channel list before the first /state arrives, from the draft
// channel-count select rather than a hardcoded guess.
function CHANNEL_COUNT_OPTIONS_FALLBACK(channelCount: number | undefined): number[] {
  const n = channelCount ?? 8
  return Array.from({ length: n }, (_, i) => i + 1)
}


// ─────────────────────────────────────────────────────────────────────
// Inter-command spacing used by All on/All off. See command_spacing_ms
// on the backend config for the full rationale — short version: a
// burst of channel commands with no pacing can outrun cheap LCUS-clone
// firmware, which reads as "All on/off is flaky" but is a timing issue,
// not (necessarily) a hardware fault.
// ─────────────────────────────────────────────────────────────────────

function SpacingControl({ valueMs, onCommit }: { valueMs: number; onCommit: (ms: number) => void }) {
  const [draft, setDraft] = useState<string>(String(valueMs))
  useEffect(() => { setDraft(String(valueMs)) }, [valueMs])
  return (
    <div className="flex items-center gap-2 text-[10px] text-slate-500">
      <label className="flex items-center gap-1.5" onPointerDown={(e) => e.stopPropagation()}>
        <span className="uppercase tracking-wider">spacing</span>
        <input
          type="number" min={0} max={1000} step={5} value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => onCommit(Math.max(0, Math.min(1000, Number(draft) || 0)))}
          onPointerDown={(e) => e.stopPropagation()}
          className="nodrag nopan w-16 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono text-[11px] text-slate-100"
        />
        <span>ms between All on/off commands</span>
      </label>
    </div>
  )
}


// ─────────────────────────────────────────────────────────────────────
// One relay channel's toggle button.
// ─────────────────────────────────────────────────────────────────────

function RelayButton({ channel, on, onToggle }: { channel: number; on: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      onPointerDown={(e) => e.stopPropagation()}
      className={`nodrag nopan flex flex-col items-center gap-1 rounded border px-2 py-2 font-mono text-[11px] transition-colors ${
        on
          ? 'border-emerald-500 bg-emerald-600/20 text-emerald-300'
          : 'border-slate-700 bg-slate-950/70 text-slate-400 hover:border-slate-500'
      }`}
    >
      <Power className={`h-4 w-4 ${on ? 'text-emerald-400' : 'text-slate-600'}`} />
      <span className="uppercase tracking-wider">CH {channel}</span>
      <span className={on ? 'text-emerald-300' : 'text-slate-500'}>{on ? 'ON' : 'OFF'}</span>
    </button>
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
