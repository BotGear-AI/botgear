import { useCallback, useEffect, useState, type FormEvent } from 'react'
import type { ServiceProxy, InboundFrame } from '@rlx/ui'
import { useWsClient, useServiceRequest } from '@rlx/ui'

// TasmotaView — power_switch control panel for one Tasmota device.
//
// Two modes, driven by whether /tasmota/<id>/state carries a host:
//   * unset  → discovery: "Scan network" (best-effort LAN sweep, see
//     tasmota_client.scan_subnet's docstring for why there's no mDNS
//     path) with per-candidate Connect, plus a manual host/user/pass
//     form as the fallback for anything the scan can't see (password-
//     protected devices, a different subnet, etc.) — same dual
//     discovered-vs-manual shape as the runtime ConnectDialog, for the
//     same reason: not everything is reliably discoverable.
//   * set    → one toggle per relay from /state.relays, a reachable
//     indicator, and a Reconfigure affordance back to discovery mode.

interface Relay {
  index: number
  on: boolean
  label?: string
}

interface TasmotaState {
  host?: string | null
  reachable?: boolean
  last_seen?: number | null
  friendly_name?: string | null
  module?: string | null
  relays?: Relay[]
}

interface ScanCandidate {
  host: string
  friendly_name?: string | null
  module?: string | null
}

interface ScanReply {
  candidates?: ScanCandidate[]
  subnet?: string
  error?: string
}

interface ConnectReply {
  error?: string
}

export default function TasmotaView({ proxy }: { proxy: ServiceProxy }) {
  const wsClient = useWsClient()
  const proxyId = proxy.id ?? proxy.name ?? ''
  const stateTopic = `/tasmota/${proxyId}/state`
  const controlTopic = `/tasmota/${proxyId}/control`

  const [state, setState] = useState<TasmotaState>({})
  const [candidates, setCandidates] = useState<ScanCandidate[] | null>(null)
  const [manualHost, setManualHost] = useState('')
  const [manualUser, setManualUser] = useState('')
  const [manualPass, setManualPass] = useState('')
  const [showManual, setShowManual] = useState(false)

  const scan = useServiceRequest<ScanReply>(controlTopic, { replyPrefix: 'tasmota-scan', timeoutMs: 15_000 })
  const connect = useServiceRequest<ConnectReply>(controlTopic, { replyPrefix: 'tasmota-connect', timeoutMs: 8_000 })

  useEffect(() => {
    if (!proxyId) return
    const off = wsClient.subscribe(stateTopic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      const p = f.payload
      if (!p || typeof p !== 'object') return
      setState(p as TasmotaState)
    })
    return off
  }, [proxyId, stateTopic, wsClient])

  const sendAction = useCallback(
    (action: string, args: Record<string, unknown> = {}) => {
      wsClient.publish(controlTopic, { action, ...args })
    },
    [wsClient, controlTopic],
  )

  const onScan = useCallback(async () => {
    setCandidates(null)
    const reply = await scan.request('scan')
    setCandidates(reply?.candidates ?? [])
  }, [scan])

  const onConnectHost = useCallback(async (host: string, username?: string, password?: string) => {
    await connect.request('connect', {
      host,
      username: username || undefined,
      password: password || undefined,
    })
  }, [connect])

  const onManualSubmit = useCallback((e?: FormEvent) => {
    e?.preventDefault()
    const host = manualHost.trim()
    if (!host) return
    void onConnectHost(host, manualUser.trim(), manualPass)
  }, [manualHost, manualUser, manualPass, onConnectHost])

  const onReconfigure = useCallback(() => {
    sendAction('disconnect')
    setCandidates(null)
    setShowManual(false)
  }, [sendAction])

  const relays = state.relays ?? []
  const connected = Boolean(state.host)

  return (
    <div className="flex flex-col gap-3 p-3 text-sm text-slate-200">
      {!connected ? (
        <>
          <div className="flex items-center justify-between">
            <h3 className="text-[10px] uppercase tracking-wider text-slate-400">
              Discover Tasmota devices
            </h3>
            <button
              type="button"
              onClick={() => void onScan()}
              disabled={scan.inFlight}
              className="rounded border border-slate-700 px-2 py-0.5 text-[11px] text-slate-300 hover:border-sky-600 hover:text-sky-300 disabled:opacity-40"
            >
              {scan.inFlight ? 'Scanning…' : 'Scan network ↻'}
            </button>
          </div>
          {scan.error && <div className="text-[11px] text-rose-300">{scan.error}</div>}
          {candidates !== null && candidates.length === 0 && !scan.error && (
            <div className="rounded border border-slate-800 bg-slate-950/60 px-2 py-1.5 text-[11px] text-slate-500">
              No unauthenticated Tasmota devices found on this subnet. Password-protected
              devices won't show up here — add them manually below.
            </div>
          )}
          {candidates && candidates.length > 0 && (
            <ul className="space-y-1">
              {candidates.map((c) => (
                <li
                  key={c.host}
                  className="flex items-center gap-2 rounded border border-slate-800 bg-slate-950/40 px-2 py-1"
                >
                  <span className="flex-1 truncate">
                    <span className="font-medium">{c.friendly_name || c.host}</span>
                    <span className="ml-1 font-mono text-[10px] text-slate-500">{c.host}</span>
                  </span>
                  <button
                    type="button"
                    onClick={() => void onConnectHost(c.host)}
                    disabled={connect.inFlight}
                    className="rounded bg-emerald-600 px-2 py-0.5 text-[11px] font-medium text-white hover:bg-emerald-500 disabled:opacity-40"
                  >
                    {connect.inFlight ? '…' : 'Connect'}
                  </button>
                </li>
              ))}
            </ul>
          )}

          <button
            type="button"
            onClick={() => setShowManual((s) => !s)}
            className="self-start text-[11px] text-slate-400 hover:text-slate-200"
          >
            {showManual ? 'Hide manual entry' : 'Enter host manually'}
          </button>
          {showManual && (
            <form onSubmit={onManualSubmit} className="flex flex-col gap-1.5 rounded border border-slate-800 p-2">
              <input
                value={manualHost}
                onChange={(e) => setManualHost(e.target.value)}
                placeholder="IP or hostname, e.g. 192.168.0.50"
                className="rounded border border-slate-700 bg-slate-950 px-2 py-1 text-[11px]"
              />
              <div className="flex gap-1.5">
                <input
                  value={manualUser}
                  onChange={(e) => setManualUser(e.target.value)}
                  placeholder="username (if set)"
                  className="w-1/2 rounded border border-slate-700 bg-slate-950 px-2 py-1 text-[11px]"
                />
                <input
                  value={manualPass}
                  onChange={(e) => setManualPass(e.target.value)}
                  placeholder="password (if set)"
                  type="password"
                  className="w-1/2 rounded border border-slate-700 bg-slate-950 px-2 py-1 text-[11px]"
                />
              </div>
              {connect.error && <div className="text-[11px] text-rose-300">{connect.error}</div>}
              <button
                type="submit"
                disabled={connect.inFlight || !manualHost.trim()}
                className="self-start rounded bg-sky-600 px-2 py-0.5 text-[11px] font-medium text-white hover:bg-sky-500 disabled:opacity-40"
              >
                {connect.inFlight ? 'Connecting…' : 'Connect'}
              </button>
            </form>
          )}
        </>
      ) : (
        <>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-1.5">
              <span
                className={`h-2 w-2 rounded-full ${state.reachable ? 'bg-emerald-500' : 'bg-rose-500'}`}
                title={state.reachable ? 'reachable' : 'unreachable'}
              />
              <span className="font-medium">{state.friendly_name || state.host}</span>
              <span className="font-mono text-[10px] text-slate-500">{state.host}</span>
            </div>
            <button
              type="button"
              onClick={onReconfigure}
              className="text-[11px] text-slate-400 hover:text-slate-200"
            >
              Reconfigure
            </button>
          </div>
          {!state.reachable && (
            <div className="text-[11px] text-rose-300">
              Not responding — will keep retrying every poll.
            </div>
          )}
          <ul className="space-y-1">
            {relays.length === 0 && (
              <li className="text-[11px] text-slate-500">
                No relay state yet — waiting on the first poll.
              </li>
            )}
            {relays.map((r) => (
              <li
                key={r.index}
                className="flex items-center justify-between rounded border border-slate-800 bg-slate-950/40 px-2 py-1.5"
              >
                <span>{r.label || `Relay ${r.index}`}</span>
                <button
                  type="button"
                  onClick={() => sendAction('power_set', { relay: r.index, on: !r.on })}
                  className={`w-14 rounded px-2 py-0.5 text-[11px] font-medium text-white ${
                    r.on ? 'bg-emerald-600 hover:bg-emerald-500' : 'bg-slate-700 hover:bg-slate-600'
                  }`}
                >
                  {r.on ? 'ON' : 'OFF'}
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}
