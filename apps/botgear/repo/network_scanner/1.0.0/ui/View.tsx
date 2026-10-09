import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ServiceProxy, InboundFrame } from '@rlx/ui'
import { useWsClient, useServiceRequest } from '@rlx/ui'

// NetworkScannerView — roster of every device seen on the local LAN(s),
// with inline labeling ("192.168.1.38" -> "the dishwasher") and a
// collapsible settings panel for scan cadence / alerting / subnet
// overrides. Everything the scan loop finds arrives on /state
// (retained) — this view never needs to poll, just render whatever
// lands there and fire actions on the control topic.

interface Device {
  mac: string
  last_ip?: string | null
  interface?: string | null
  first_seen: number
  last_seen: number
  online: boolean
  missed_scans: number
  offline_since?: number | null
  hostname?: string | null
  vendor?: string | null
  mdns_name?: string | null
  label?: string | null
  notes?: string | null
  device_type?: string | null
  ignored: boolean
  trusted: boolean
  display_name: string
}

interface ScannerState {
  devices?: Device[]
  total?: number
  online?: number
  offline?: number
  subnets?: string[]
  scan_interval_s?: number
  last_scan?: number | null
  alert_target?: string | null
  alert_on?: string[]
}

interface ScanReply {
  error?: string
}

function timeAgo(ts?: number | null): string {
  if (!ts) return 'never'
  const s = Math.max(0, Date.now() / 1000 - ts)
  if (s < 60) return `${Math.round(s)}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

export default function NetworkScannerView({ proxy }: { proxy: ServiceProxy }) {
  const wsClient = useWsClient()
  const proxyId = proxy.id ?? proxy.name ?? ''
  const stateTopic = `/network_scanner/${proxyId}/state`
  const controlTopic = `/network_scanner/${proxyId}/control`

  const [state, setState] = useState<ScannerState>({})
  const [showSettings, setShowSettings] = useState(false)
  const [showIgnored, setShowIgnored] = useState(false)
  const [editingMac, setEditingMac] = useState<string | null>(null)
  const [editValue, setEditValue] = useState('')

  const scanNow = useServiceRequest<ScanReply>(controlTopic, { replyPrefix: 'netscan-scan', timeoutMs: 30_000 })

  useEffect(() => {
    if (!proxyId) return
    const off = wsClient.subscribe(stateTopic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      const p = f.payload
      if (!p || typeof p !== 'object') return
      setState(p as ScannerState)
    })
    return off
  }, [proxyId, stateTopic, wsClient])

  const sendAction = useCallback(
    (action: string, args: Record<string, unknown> = {}) => {
      wsClient.publish(controlTopic, { action, ...args })
    },
    [wsClient, controlTopic],
  )

  const onScanNow = useCallback(() => {
    void scanNow.request('scan_now')
  }, [scanNow])

  const startEdit = useCallback((d: Device) => {
    setEditingMac(d.mac)
    setEditValue(d.label ?? '')
  }, [])

  const commitEdit = useCallback(() => {
    if (editingMac) {
      sendAction('set_label', { mac: editingMac, label: editValue.trim() || null })
    }
    setEditingMac(null)
    setEditValue('')
  }, [editingMac, editValue, sendAction])

  const devices = state.devices ?? []
  const visible = useMemo(
    () => devices.filter((d) => showIgnored || !d.ignored),
    [devices, showIgnored],
  )

  return (
    <div className="flex flex-col gap-3 p-3 text-sm text-slate-200">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-slate-400">
            <span className="font-medium text-emerald-400">{state.online ?? 0}</span> online
            {' · '}
            <span className="text-slate-300">{state.total ?? 0}</span> known
          </span>
          <span className="font-mono text-[10px] text-slate-500">last scan {timeAgo(state.last_scan)}</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setShowSettings((s) => !s)}
            className="text-[11px] text-slate-400 hover:text-slate-200"
          >
            {showSettings ? 'Hide settings' : 'Settings'}
          </button>
          <button
            type="button"
            onClick={onScanNow}
            disabled={scanNow.inFlight}
            className="rounded border border-slate-700 px-2 py-0.5 text-[11px] text-slate-300 hover:border-sky-600 hover:text-sky-300 disabled:opacity-40"
          >
            {scanNow.inFlight ? 'Scanning…' : 'Scan now ↻'}
          </button>
        </div>
      </div>

      {scanNow.error && <div className="text-[11px] text-rose-300">{scanNow.error}</div>}

      {showSettings && (
        <SettingsPanel state={state} sendAction={sendAction} />
      )}

      {state.subnets && state.subnets.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {state.subnets.map((s) => (
            <span key={s} className="rounded bg-slate-800/70 px-1.5 py-0.5 font-mono text-[10px] text-slate-400">
              {s}
            </span>
          ))}
        </div>
      )}

      <div className="flex items-center justify-between">
        <h3 className="text-[10px] uppercase tracking-wider text-slate-400">Devices</h3>
        <label className="flex items-center gap-1 text-[10px] text-slate-500">
          <input type="checkbox" checked={showIgnored} onChange={(e) => setShowIgnored(e.target.checked)} />
          show ignored
        </label>
      </div>

      <ul className="space-y-1">
        {visible.length === 0 && (
          <li className="rounded border border-slate-800 bg-slate-950/60 px-2 py-1.5 text-[11px] text-slate-500">
            No devices yet — run a scan to build the roster.
          </li>
        )}
        {visible.map((d) => (
          <li
            key={d.mac}
            className={`rounded border border-slate-800 bg-slate-950/40 px-2 py-1.5 ${d.ignored ? 'opacity-50' : ''}`}
          >
            <div className="flex items-center gap-2">
              <span
                className={`h-2 w-2 shrink-0 rounded-full ${d.online ? 'bg-emerald-500' : 'bg-rose-500'}`}
                title={d.online ? 'online' : `offline since ${timeAgo(d.offline_since)}`}
              />
              {editingMac === d.mac ? (
                <input
                  autoFocus
                  value={editValue}
                  onChange={(e) => setEditValue(e.target.value)}
                  onBlur={commitEdit}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') commitEdit()
                    if (e.key === 'Escape') { setEditingMac(null); setEditValue('') }
                  }}
                  placeholder="label this device…"
                  className="flex-1 rounded border border-sky-700 bg-slate-950 px-1.5 py-0.5 text-[11px]"
                />
              ) : (
                <button
                  type="button"
                  onClick={() => startEdit(d)}
                  className="flex-1 truncate text-left font-medium hover:text-sky-300"
                  title="click to label"
                >
                  {d.display_name}
                </button>
              )}
              <span className="font-mono text-[10px] text-slate-500">{d.last_ip ?? '—'}</span>
              <button
                type="button"
                onClick={() => sendAction('remove_device', { mac: d.mac })}
                className="text-[10px] text-slate-500 hover:text-rose-300"
                title="forget this device"
              >
                ✕
              </button>
            </div>
            <div className="mt-0.5 flex flex-wrap items-center gap-1 pl-4 text-[10px] text-slate-500">
              <span className="font-mono">{d.mac}</span>
              {d.vendor && <span className="rounded bg-slate-800/70 px-1 py-0.5">{d.vendor}</span>}
              {d.hostname && <span className="rounded bg-slate-800/70 px-1 py-0.5">{d.hostname}</span>}
              {d.mdns_name && d.mdns_name !== d.hostname && (
                <span className="rounded bg-slate-800/70 px-1 py-0.5">{d.mdns_name}</span>
              )}
              {d.interface && <span>{d.interface}</span>}
              <button
                type="button"
                onClick={() => sendAction('set_label', { mac: d.mac, ignored: !d.ignored })}
                className="ml-auto hover:text-slate-300"
              >
                {d.ignored ? 'unignore' : 'ignore'}
              </button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}

function SettingsPanel({
  state, sendAction,
}: {
  state: ScannerState
  sendAction: (action: string, args?: Record<string, unknown>) => void
}) {
  const [interval, setIntervalVal] = useState(String(state.scan_interval_s ?? 60))
  const [alertTarget, setAlertTarget] = useState(state.alert_target ?? '')
  const [subnets, setSubnets] = useState('')
  const alertOn = state.alert_on ?? ['new', 'offline', 'online']

  useEffect(() => setIntervalVal(String(state.scan_interval_s ?? 60)), [state.scan_interval_s])
  useEffect(() => setAlertTarget(state.alert_target ?? ''), [state.alert_target])

  const toggleAlertEvent = useCallback((event: string) => {
    const next = alertOn.includes(event) ? alertOn.filter((e) => e !== event) : [...alertOn, event]
    sendAction('set_alert_on', { events: next })
  }, [alertOn, sendAction])

  return (
    <div className="flex flex-col gap-2 rounded border border-slate-800 p-2 text-[11px]">
      <label className="flex items-center gap-2">
        <span className="w-32 text-slate-400">Scan interval (s)</span>
        <input
          value={interval}
          onChange={(e) => setIntervalVal(e.target.value)}
          onBlur={() => sendAction('set_scan_interval', { seconds: Number(interval) || 60 })}
          className="w-20 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5"
        />
      </label>
      <label className="flex items-center gap-2">
        <span className="w-32 shrink-0 text-slate-400">Alert target topic</span>
        <input
          value={alertTarget}
          onChange={(e) => setAlertTarget(e.target.value)}
          onBlur={() => sendAction('set_alert_target', { topic: alertTarget.trim() || null })}
          placeholder="/chat/chat-1/control"
          className="flex-1 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono"
        />
      </label>
      <div className="flex items-center gap-3">
        <span className="w-32 shrink-0 text-slate-400">Alert on</span>
        {(['new', 'offline', 'online'] as const).map((event) => (
          <label key={event} className="flex items-center gap-1">
            <input type="checkbox" checked={alertOn.includes(event)} onChange={() => toggleAlertEvent(event)} />
            {event}
          </label>
        ))}
      </div>
      <label className="flex items-start gap-2">
        <span className="w-32 shrink-0 text-slate-400">Subnet overrides</span>
        <textarea
          value={subnets}
          onChange={(e) => setSubnets(e.target.value)}
          onBlur={() => {
            const list = subnets.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean)
            if (list.length > 0) sendAction('set_subnets', { subnets: list })
          }}
          placeholder="192.168.1.0/24 (blank = auto-detect all local subnets)"
          rows={2}
          className="flex-1 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono"
        />
      </label>
    </div>
  )
}
