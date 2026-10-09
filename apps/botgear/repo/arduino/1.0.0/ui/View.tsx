import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { Loader2 } from 'lucide-react'
import type { ServiceProxy } from '@rlx/ui'
import { type InboundFrame } from '@rlx/ui'
import { useWsClient } from '@rlx/ui'
import { useServiceRequest } from '@rlx/ui'


// How long to wait for a connect/disconnect reply before assuming
// something went sideways and unlocking the UI. pymata4's
// arduino_wait is 4s + serial settle; 10s is a comfortable ceiling.
const CONNECT_REPLY_TIMEOUT_MS = 10_000

interface PortHolder {
  pid: number
  proxy_id?: string | null
  service_type?: string | null
  name?: string | null
}

interface SerialPort {
  device: string
  description?: string
  hwid?: string
  /** Other processes currently holding this device open. Populated
   *  by rlx_bus.list_serial_ports() — empty list means the device
   *  is free. Linux-only (uses /proc); macOS/Windows leave it
   *  empty and ``available`` defaults true. */
  holders?: PortHolder[]
  available?: boolean
}

interface PinSnapshot {
  mode?: string
  value?: number
}

interface ArduinoState {
  connected?: boolean
  port?: string | null
  ports?: SerialPort[]
  firmata_version?: string | null
  firmware_name?: string | null
  firmware_version?: string | null
  pins?: Record<string, PinSnapshot>
  polling?: Record<string, number>   // pin label → poll interval_ms
  board_pins?: string[]              // discovered pin labels ('0'..'N','A0'..) for the picklist
  connect_error?: string | null
  last_port?: string | null   // persisted across sessions
  last_baud?: number | null
}

// Digital pins take these; an analog pin ('A<n>') is always analog input.
const DIGITAL_MODES = ['input', 'output', 'pwm', 'servo']
const isAnalogPin = (label: string) => label.trim().toUpperCase().startsWith('A')
// Sort pin labels: digital ('0'..'53') ascending first, then analog ('A0'..).
function pinSort(a: string, b: string): number {
  const aa = isAnalogPin(a), ba = isAnalogPin(b)
  if (aa !== ba) return aa ? 1 : -1
  const an = parseInt(aa ? a.slice(1) : a, 10)
  const bn = parseInt(ba ? b.slice(1) : b, 10)
  return (isNaN(an) ? 0 : an) - (isNaN(bn) ? 0 : bn)
}

// Heartbeat is "fresh" when the last beat was within HEARTBEAT_FRESH_MS.
const HEARTBEAT_FRESH_MS = 2500

export default function ArduinoFullView({ proxy }: { proxy: ServiceProxy }) {
  const wsClient = useWsClient()
  const proxyId = proxy.id ?? proxy.name ?? ''
  const stateTopic = `/arduino/${proxyId}/state`
  const heartbeatTopic = `/arduino/${proxyId}/heartbeat`
  const controlTopic = `/arduino/${proxyId}/control`

  const [state, setState] = useState<ArduinoState>({})
  const [lastBeat, setLastBeat] = useState<number | null>(null)
  // Tick to force re-render so the "fresh" check below stays current.
  const [, setNowTick] = useState(0)

  useEffect(() => {
    if (!proxyId) return
    const offState = wsClient.subscribe(stateTopic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      setState((prev) => ({ ...prev, ...(f.payload as ArduinoState) }))
    })
    const offBeat = wsClient.subscribe(heartbeatTopic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      const p = f.payload as { ts?: number }
      if (typeof p?.ts === 'number') setLastBeat(p.ts * 1000)  // s → ms
    })
    // Tick so heartbeat "fresh" indicator decays even without new beats.
    const timer = setInterval(() => setNowTick((t) => t + 1), 1000)
    return () => { offState(); offBeat(); clearInterval(timer) }
  }, [proxyId, stateTopic, heartbeatTopic, wsClient])

  // Discover every serial proxy on the bus so arduino can offer
  // ``bus:<id>`` virtual ports in its dropdown alongside the real
  // OS devices. Wildcard subscription on ``/serial/+/state`` —
  // each retained state announces the proxy's existence + which
  // real port (if any) it currently owns; we render that as the
  // virtual entry's description ("serial proxy → /dev/ttyACM0").
  // Disconnected serial proxies still appear (description becomes
  // "serial proxy (not connected)") because attaching to one is a
  // valid operator setup step — they'll connect after.
  const [serialProxies, setSerialProxies] = useState<Record<string, { port?: string | null; connected?: boolean; baudrate?: number }>>({})
  useEffect(() => {
    const off = wsClient.subscribe('/serial/+/state', (f: InboundFrame) => {
      if (f.method !== 'message') return
      // Topic is /serial/<id>/state — pluck the id.
      const m = (f.topic ?? '').match(/^\/serial\/([^/]+)\/state$/)
      if (!m) return
      const id = m[1]
      const p = (f.payload ?? {}) as { connected?: boolean; port?: string | null; baudrate?: number }
      setSerialProxies((prev) => ({
        ...prev,
        [id]: { port: p.port, connected: !!p.connected, baudrate: p.baudrate },
      }))
    })
    return off
  }, [wsClient])

  // Per-pin subscriptions for the pin grid. Refreshed when the set of
  // configured pins changes.
  const configuredPins = useMemo(() => Object.keys(state.pins ?? {}).sort(pinSort), [state.pins])
  useEffect(() => {
    if (!proxyId || configuredPins.length === 0) return
    const unsubs: Array<() => void> = []
    for (const p of configuredPins) {
      const topic = `/arduino/${proxyId}/pin/${p}`
      const off = wsClient.subscribe(topic, (f: InboundFrame) => {
        if (f.method !== 'message') return
        const payload = f.payload as { value?: number }
        if (typeof payload?.value !== 'number') return
        setState((prev) => {
          const pins = prev.pins ?? {}
          // Don't RESURRECT a pin that was just removed (release_pin): a
          // polled pin can have in-flight /pin/{p} messages arriving after
          // the removal — only update the value if the pin still exists.
          if (!(String(p) in pins)) return prev
          return { ...prev, pins: { ...pins, [String(p)]: { ...pins[String(p)], value: payload.value } } }
        })
      })
      unsubs.push(off)
    }
    return () => { for (const off of unsubs) off() }
  }, [proxyId, configuredPins.join(','), wsClient])

  const send = useCallback(
    (action: string, args: Record<string, unknown> = {}) => {
      wsClient.publish(controlTopic, { action, ...args })
    },
    [controlTopic, wsClient],
  )

  // Connect/disconnect use the shared reply-tracking hook. It owns
  // the spinner state, debounce, unique reply topic, awaitSubscribed
  // race guard, attempt-id bookkeeping, and 10s timeout — what used
  // to be ~80 lines of attempt tracking lives in one place now and
  // is reused by Serial (and any future serial-port-owning service).
  const connectReq = useServiceRequest<{ connected?: boolean; error?: string }>(controlTopic, {
    timeoutMs: CONNECT_REPLY_TIMEOUT_MS,
    errorField: 'error',
    replyPrefix: `arduino-${proxyId}-connect`,
  })
  const disconnectReq = useServiceRequest<{ connected?: boolean; error?: string }>(controlTopic, {
    timeoutMs: CONNECT_REPLY_TIMEOUT_MS,
    errorField: 'error',
    replyPrefix: `arduino-${proxyId}-disconnect`,
  })
  // Aggregate spinner — Connection subcomponent only cares "is
  // anything in flight"; the hook's per-action errors disambiguate
  // when needed.
  const connecting = connectReq.inFlight || disconnectReq.inFlight

  const sendConnect = useCallback((port: string, baud: number) => {
    void connectReq.request('connect', { port, baud })
  }, [connectReq])

  const sendDisconnect = useCallback(() => {
    void disconnectReq.request('disconnect')
  }, [disconnectReq])

  const serviceRunning = proxy.status === 'running' || proxy.status === 'starting'
  const heartbeatFresh = lastBeat !== null && Date.now() - lastBeat < HEARTBEAT_FRESH_MS
  const heartbeatAge = lastBeat !== null ? Date.now() - lastBeat : null
  const connected = !!state.connected
  // Merge real OS ports (state.ports) with virtual ``bus:*`` ports
  // synthesised from every known serial proxy. The virtual entries
  // are ordered AFTER the real ones so the dropdown reads
  // "real devices ↘ virtual proxies". A serial proxy that's NOT
  // currently connected still surfaces — attaching to it is a
  // valid setup step.
  const ports = useMemo<SerialPort[]>(() => {
    const real = state.ports ?? []
    const virtual: SerialPort[] = Object.entries(serialProxies).map(([id, info]) => ({
      device: `bus:${id}`,
      description: info.connected
        ? `serial proxy → ${info.port ?? '?'}`
        : 'serial proxy (not connected)',
      // hwid kept empty; the bus protocol has no hardware id.
      hwid: '',
      // Virtual ports are always "available" from arduino's POV —
      // the underlying real-port contention is handled inside the
      // serial service. The serial proxy ITSELF can only be
      // attached to once at a time though; if some other process
      // already opened bus:serial-1 via a BusBackedSerial it'd
      // share the same /serial/<id>/rx stream which is actually
      // OK (broadcasting to multiple subscribers). So we don't
      // mark virtual ports as "in use".
      holders: [],
      available: true,
    }))
    return [...real, ...virtual]
  }, [state.ports, serialProxies])

  /** What to actually render in the Firmware section's red banner:
   *   - The hook's latest error (the most recent connect/disconnect
   *     attempt's outcome) is authoritative.
   *   - Else the retained server ``connect_error`` UNLESS something
   *     is currently in flight — that old value is stale until the
   *     in-flight attempt produces its own outcome.
   *   - Cleared on a successful connect (server clears it from
   *     /state; the hook also clears its own error on every fresh
   *     request). */
  const displayedConnectError = connectReq.error
    ?? disconnectReq.error
    ?? (connecting ? null : (state.connect_error ?? null))

  return (
    <div className="flex min-w-[420px] flex-col gap-3 p-3 text-xs">
      <Connection
        ports={ports}
        ownProxyId={proxyId}
        connectedPort={state.port}
        lastPort={state.last_port ?? null}
        lastBaud={state.last_baud ?? null}
        connected={connected}
        connecting={connecting}
        heartbeatFresh={heartbeatFresh}
        heartbeatAge={heartbeatAge}
        serviceRunning={serviceRunning}
        onRefresh={() => send('list_ports')}
        onConnect={sendConnect}
        onDisconnect={sendDisconnect}
        // onPortChange is now a no-op — the hook auto-clears its
        // error on every new ``request()`` and the displayedConnectError
        // hides the retained server error while connecting is true.
        // Kept as a prop so the Connection subcomponent's signature
        // stays stable; callers that want to react to port edits can
        // wire something in later.
        onPortChange={() => { /* see comment above */ }}
      />
      <Firmware
        firmataVersion={state.firmata_version ?? null}
        firmwareName={state.firmware_name ?? null}
        firmwareVersion={state.firmware_version ?? null}
        connectError={displayedConnectError}
        connecting={connecting}
      />
      <Pins
        connected={connected}
        pins={state.pins ?? {}}
        polling={state.polling ?? {}}
        boardPins={state.board_pins ?? []}
        onSetMode={(pin, mode) => send('set_pin_mode', { pin, mode })}
        onDigitalWrite={(pin, value) => send('digital_write', { pin, value })}
        onAnalogWrite={(pin, value) => send('analog_write', { pin, value })}
        onDigitalRead={(pin) => send('digital_read', { pin })}
        onAnalogRead={(pin) => send('analog_read', { pin })}
        onPoll={(pin, interval_ms) => send('poll_pin', { pin, interval_ms })}
        onStopPoll={(pin) => send('stop_poll', { pin })}
        onRelease={(pin) => send('release_pin', { pin })}
      />
      <I2C
        connected={connected}
        onSetup={() => send('i2c_setup')}
        onScan={() => send('i2c_scan')}
        onRead={(addr, reg, count) => send('i2c_read', { addr, reg, count })}
        onWrite={(addr, data) => send('i2c_write', { addr, data })}
      />
      <Sonar
        proxyId={proxyId}
        connected={connected}
        onSetup={(trig, echo) => send('sonar_setup', { trigger_pin: trig, echo_pin: echo })}
        onRead={(trig) => send('sonar_read', { trigger_pin: trig })}
      />
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────
// Connection section
// ─────────────────────────────────────────────────────────────────────
const BAUD_OPTIONS = [9600, 19200, 38400, 57600, 115200] as const

function Connection({
  ports, ownProxyId, connectedPort, lastPort, lastBaud, connected, connecting,
  heartbeatFresh, heartbeatAge, serviceRunning,
  onRefresh, onConnect, onDisconnect, onPortChange,
}: {
  ports: SerialPort[]
  /** This arduino's own proxy id so we can filter ourselves out of
   *  the "held by another service" check — we're allowed to
   *  re-Connect on the same port we're already holding. */
  ownProxyId: string
  connectedPort?: string | null
  lastPort?: string | null
  lastBaud?: number | null
  connected: boolean
  connecting: boolean
  heartbeatFresh: boolean
  heartbeatAge: number | null
  serviceRunning: boolean
  onRefresh: () => void
  onConnect: (port: string, baud: number) => void
  onDisconnect: () => void
  onPortChange: () => void
}) {
  const [selected, setSelected] = useState<string>('')
  const [baud, setBaud] = useState<number>(lastBaud ?? 115200)
  // Preferred selection: lastPort (the persisted last-known-good)
  // when present and currently detected. Falls back to the first
  // detected port. The user can still pick something else after.
  useEffect(() => {
    if (selected) return
    if (lastPort && ports.some((p) => p.device === lastPort)) {
      setSelected(lastPort)
      return
    }
    if (ports.length > 0) setSelected(ports[0].device)
  }, [ports, lastPort, selected])
  // Pick up persisted last_baud whenever the service re-emits state
  // (e.g. after a service restart). Don't clobber the user's in-flight
  // choice once they've changed the dropdown — gated on connected so
  // we only sync between sessions, not during interaction.
  useEffect(() => {
    if (!connected && lastBaud && lastBaud !== baud) setBaud(lastBaud)
  }, [lastBaud, connected])

  const dotColor = !serviceRunning
    ? 'bg-slate-500'
    : connected
      ? (heartbeatFresh ? 'bg-emerald-500' : 'bg-amber-400')
      : 'bg-slate-500'

  return (
    <Section title="Connection">
      <div className="flex items-center gap-2">
        <select
          value={selected}
          onChange={(e) => { setSelected(e.target.value); onPortChange() }}
          disabled={!serviceRunning || connected || connecting}
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => e.stopPropagation()}
          className="nodrag nopan flex-1 rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-xs text-slate-200 focus:border-slate-500 focus:outline-none disabled:opacity-50"
        >
          {ports.length === 0 && <option value="">(no ports detected)</option>}
          {ports.map((p) => {
            // Filter out our own holding from the "in use by other"
            // check — re-Connect on a port we already own is fine.
            const others = (p.holders ?? []).filter((h) => h.proxy_id !== ownProxyId)
            const ownedByOther = others.length > 0
            const ownerLabel = ownedByOther
              ? others
                  .map((h) => h.proxy_id ?? `${h.service_type ?? h.name ?? 'pid'} ${h.pid}`)
                  .join(', ')
              : ''
            return (
              <option key={p.device} value={p.device} disabled={ownedByOther}>
                {p.device}{p.device === lastPort ? '  ★ last' : ''}
                {p.description ? `  — ${p.description}` : ''}
                {ownedByOther ? `  (in use by ${ownerLabel})` : ''}
              </option>
            )
          })}
        </select>
        <ActionButton onClick={onRefresh} disabled={!serviceRunning || connecting}>↻</ActionButton>
        <select
          value={baud}
          onChange={(e) => { setBaud(Number(e.target.value)); onPortChange() }}
          disabled={!serviceRunning || connected || connecting}
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => e.stopPropagation()}
          title="Serial baud rate — FirmataExpress runs at 115200 (default). pymata4 only does its FirmataExpress handshake at 115200; other rates fall through and may report 'Firmware Version Not Found' on a healthy board."
          className="nodrag nopan rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-xs text-slate-200 focus:border-slate-500 focus:outline-none disabled:opacity-50"
        >
          {BAUD_OPTIONS.map((b) => (
            <option key={b} value={b}>
              {b}{b === lastBaud ? '  ★' : ''}
            </option>
          ))}
        </select>
        {!connected ? (
          <ActionButton
            tone="primary"
            onClick={() => selected && onConnect(selected, baud)}
            disabled={!serviceRunning || !selected || connecting}
          >
            {connecting ? (
              <span className="inline-flex items-center gap-1.5">
                <Loader2 className="h-3 w-3 animate-spin" />
                <span>Connecting…</span>
              </span>
            ) : 'Connect'}
          </ActionButton>
        ) : (
          <ActionButton onClick={onDisconnect} disabled={!serviceRunning || connecting}>
            {connecting ? (
              <span className="inline-flex items-center gap-1.5">
                <Loader2 className="h-3 w-3 animate-spin" />
                <span>Disconnecting…</span>
              </span>
            ) : 'Disconnect'}
          </ActionButton>
        )}
      </div>
      <div className="mt-2 flex items-center gap-2 text-[10px] uppercase tracking-wide text-slate-400">
        <span className={`inline-block h-2 w-2 rounded-full ${dotColor}`} />
        {!serviceRunning && <span>service not running</span>}
        {serviceRunning && !connected && <span>disconnected</span>}
        {serviceRunning && connected && (
          <>
            <span>connected on {connectedPort}</span>
            <span className="text-slate-500">
              · heartbeat {heartbeatAge === null ? '—' : `${Math.round(heartbeatAge)}ms ago`}
            </span>
          </>
        )}
      </div>
    </Section>
  )
}

// ─────────────────────────────────────────────────────────────────────
// Firmware section
// ─────────────────────────────────────────────────────────────────────
function Firmware({
  firmataVersion, firmwareName, firmwareVersion, connectError, connecting,
}: {
  firmataVersion: string | null
  firmwareName: string | null
  firmwareVersion: string | null
  connectError: string | null
  connecting: boolean
}) {
  return (
    <Section title="Firmware">
      <div className="rounded border border-slate-800 bg-slate-950/70 p-2 leading-snug text-slate-400">
        <div className="text-slate-300">
          Expected — <span className="font-mono">StandardFirmata</span> (Arduino IDE
          {' → '}Examples → Firmata → StandardFirmata) or{' '}
          <span className="font-mono">FirmataExpress</span> for sonar / I2C / servo
          extensions.
        </div>
        <div className="mt-2 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 font-mono">
          <span className="text-slate-500">firmata</span>
          <span>{firmataVersion ?? '—'}</span>
          <span className="text-slate-500">firmware</span>
          <span>{firmwareName ?? '—'} {firmwareVersion ?? ''}</span>
        </div>
        {/* While a connect is in flight, show a neutral "Connecting…"
            banner instead of a stale error from a previous attempt. */}
        {connecting && (
          <div className="mt-2 inline-flex items-center gap-1.5 rounded border border-amber-700 bg-amber-950/40 px-2 py-1 font-mono text-[11px] text-amber-200">
            <Loader2 className="h-3 w-3 animate-spin" />
            <span>Negotiating with the board…</span>
          </div>
        )}
        {!connecting && connectError && (
          <div className="mt-2 rounded border border-rose-700 bg-rose-950/40 px-2 py-1 font-mono text-[11px] text-rose-200">
            connect failed — {connectError}
          </div>
        )}
      </div>
    </Section>
  )
}

// ─────────────────────────────────────────────────────────────────────
// Pins section
// ─────────────────────────────────────────────────────────────────────
function Pins({
  connected, pins, polling, boardPins,
  onSetMode, onDigitalWrite, onAnalogWrite, onDigitalRead, onAnalogRead, onPoll, onStopPoll, onRelease,
}: {
  connected: boolean
  pins: Record<string, PinSnapshot>
  polling: Record<string, number>
  boardPins: string[]
  onSetMode: (pin: string, mode: string) => void
  onDigitalWrite: (pin: string, value: number) => void
  onAnalogWrite: (pin: string, value: number) => void
  onDigitalRead: (pin: string) => void
  onAnalogRead: (pin: string) => void
  onPoll: (pin: string, interval_ms: number) => void
  onStopPoll: (pin: string) => void
  onRelease: (pin: string) => void
}) {
  // "Add a pin": a typeahead combobox over the board's discovered pins
  // (board_pins) — pick a real pin, or type ANY label ('A0', '13') when the
  // board isn't recognised (empty list). A pin is a LABEL, not a number.
  const [newPin, setNewPin] = useState('')
  const [newMode, setNewMode] = useState('input')
  const listId = useMemo(() => `arduino-pins-${Math.random().toString(36).slice(2)}`, [])
  const newPinAnalog = isAnalogPin(newPin)

  // Seed a default from the discovered pins once they arrive (first analog,
  // else first pin) — leaves the field free-form when nothing was discovered.
  useEffect(() => {
    if (!newPin && boardPins.length) setNewPin(boardPins.find(isAnalogPin) ?? boardPins[0])
  }, [boardPins])   // eslint-disable-line react-hooks/exhaustive-deps
  // An 'A<n>' pin is analog input; keep the mode selector consistent.
  useEffect(() => {
    if (newPinAnalog && newMode !== 'analog') setNewMode('analog')
    else if (!newPinAnalog && newMode === 'analog') setNewMode('input')
  }, [newPinAnalog])  // eslint-disable-line react-hooks/exhaustive-deps

  const rows = useMemo(
    () => Object.keys(pins).sort(pinSort).map((k) => ({ pin: k, ...pins[k] })),
    [pins],
  )

  return (
    <Section title="Pins">
      {!connected && (
        <div className="rounded border border-slate-800 bg-slate-950/70 p-2 text-slate-500">
          Connect to a board first.
        </div>
      )}
      {connected && (
        <>
          <form
            className="mb-2 flex items-center gap-2"
            onSubmit={(e: FormEvent) => {
              e.preventDefault()
              const p = newPin.trim()
              if (p) onSetMode(p, newPinAnalog ? 'analog' : newMode)
            }}
            onPointerDown={(e) => e.stopPropagation()}
          >
            <span className="text-[10px] uppercase tracking-wider text-slate-500">add pin</span>
            <input
              list={listId}
              value={newPin}
              onChange={(e) => setNewPin(e.target.value)}
              placeholder={boardPins.length ? 'pick or type' : 'e.g. A0, 13'}
              onPointerDown={(e) => e.stopPropagation()}
              className="nodrag nopan w-24 rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-xs text-slate-100 focus:border-slate-500 focus:outline-none"
            />
            <datalist id={listId}>
              {boardPins.map((p) => <option key={p} value={p} />)}
            </datalist>
            <select
              value={newPinAnalog ? 'analog' : newMode}
              disabled={newPinAnalog}
              onChange={(e) => setNewMode(e.target.value)}
              title={newPinAnalog ? 'analog pins are analog input' : undefined}
              className="nodrag nopan rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-xs text-slate-100 disabled:opacity-60"
            >
              {(newPinAnalog ? ['analog'] : DIGITAL_MODES).map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
            <ActionButton type="submit" disabled={!newPin.trim()}>Add</ActionButton>
          </form>
          {rows.length === 0 && (
            <div className="text-slate-500">No pins configured yet.</div>
          )}
          {rows.length > 0 && (
            <table className="w-full table-auto font-mono">
              <thead className="text-[10px] uppercase tracking-wider text-slate-500">
                <tr>
                  <th className="text-left">pin</th>
                  <th className="text-left">mode</th>
                  <th className="text-left">value</th>
                  <th className="text-right">action</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <PinRow
                    key={row.pin}
                    pin={row.pin}
                    mode={row.mode ?? ''}
                    value={row.value}
                    pollMs={polling[String(row.pin)]}
                    onSetMode={(m) => onSetMode(row.pin, m)}
                    onDigitalWrite={(v) => onDigitalWrite(row.pin, v)}
                    onAnalogWrite={(v) => onAnalogWrite(row.pin, v)}
                    onDigitalRead={() => onDigitalRead(row.pin)}
                    onAnalogRead={() => onAnalogRead(row.pin)}
                    onPoll={(ms) => onPoll(row.pin, ms)}
                    onStopPoll={() => onStopPoll(row.pin)}
                    onRelease={() => onRelease(row.pin)}
                  />
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </Section>
  )
}

function PinRow({
  pin, mode, value, pollMs,
  onSetMode, onDigitalWrite, onAnalogWrite, onDigitalRead, onAnalogRead, onPoll, onStopPoll, onRelease,
}: {
  pin: string
  mode: string
  value?: number
  pollMs?: number                       // active poll interval (ms), or undefined if not polling
  onSetMode: (mode: string) => void
  onDigitalWrite: (value: number) => void
  onAnalogWrite: (value: number) => void
  onDigitalRead: () => void
  onAnalogRead: () => void
  onPoll: (interval_ms: number) => void
  onStopPoll: () => void
  onRelease: () => void
}) {
  const [pwm, setPwm] = useState('0')
  const [intervalMs, setIntervalMs] = useState('100')
  const isPolling = pollMs !== undefined
  const analogPin = isAnalogPin(pin)   // 'A<n>' — analog input only
  const modeOptions = analogPin ? ['analog'] : DIGITAL_MODES
  return (
    <tr className="border-t border-slate-800">
      <td className="py-1 text-slate-300">{pin}</td>
      <td className="py-1">
        <select
          value={mode}
          onChange={(e) => onSetMode(e.target.value)}
          onPointerDown={(e) => e.stopPropagation()}
          disabled={analogPin}
          className="nodrag nopan rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 text-xs text-slate-100 disabled:opacity-70"
        >
          {modeOptions.map((m) => <option key={m} value={m}>{m}</option>)}
          {mode && !modeOptions.includes(mode) && <option value={mode}>{mode}</option>}
        </select>
      </td>
      <td className="py-1 text-slate-300">
        {value === undefined ? '—' : value}
      </td>
      <td className="py-1">
        <div className="flex items-center justify-end gap-1">
        {(mode === 'input' || mode === 'analog') && (
          <span className="flex items-center justify-end gap-1">
            <ActionButton onClick={mode === 'analog' ? onAnalogRead : onDigitalRead}>read</ActionButton>
            <input
              type="number" min={10} step={10}
              value={isPolling ? String(pollMs) : intervalMs}
              disabled={isPolling}
              onChange={(e) => setIntervalMs(e.target.value)}
              onPointerDown={(e) => e.stopPropagation()}
              title="poll interval (ms)"
              className="nodrag nopan w-14 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono text-xs text-slate-100 disabled:opacity-60"
            />
            <span className="text-[10px] text-slate-500">ms</span>
            {isPolling
              ? <ActionButton tone="primary" onClick={onStopPoll} title="stop continuous polling">◉ stop</ActionButton>
              : <ActionButton onClick={() => { const ms = Number.parseInt(intervalMs, 10); onPoll(!Number.isNaN(ms) && ms >= 10 ? ms : 100) }} title="stream this pin continuously">poll</ActionButton>}
          </span>
        )}
        {mode === 'output' && (
          <span className="flex justify-end gap-1">
            <ActionButton onClick={() => onDigitalWrite(1)}>HIGH</ActionButton>
            <ActionButton onClick={() => onDigitalWrite(0)}>LOW</ActionButton>
          </span>
        )}
        {mode === 'pwm' && (
          <span className="flex justify-end gap-1">
            <input
              type="number" min={0} max={255} step={1}
              value={pwm}
              onChange={(e) => setPwm(e.target.value)}
              onPointerDown={(e) => e.stopPropagation()}
              className="nodrag nopan w-14 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono text-xs text-slate-100"
            />
            <ActionButton
              onClick={() => {
                const v = Number.parseInt(pwm, 10)
                if (!Number.isNaN(v)) onAnalogWrite(v)
              }}
            >
              write
            </ActionButton>
          </span>
        )}
        {mode === 'servo' && (
          <span className="flex justify-end gap-1">
            <input
              type="number" min={0} max={180} step={1}
              value={pwm}
              onChange={(e) => setPwm(e.target.value)}
              onPointerDown={(e) => e.stopPropagation()}
              className="nodrag nopan w-14 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono text-xs text-slate-100"
            />
            <ActionButton
              onClick={() => {
                const v = Number.parseInt(pwm, 10)
                if (!Number.isNaN(v)) onAnalogWrite(v)
              }}
            >
              write
            </ActionButton>
          </span>
        )}
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); onRelease() }}
            onPointerDown={(e) => e.stopPropagation()}
            title="remove pin"
            className="nodrag nopan ml-1 rounded px-1.5 py-1 text-xs text-slate-400 hover:bg-slate-800 hover:text-rose-300"
          >
            ✕
          </button>
        </div>
      </td>
    </tr>
  )
}

// ─────────────────────────────────────────────────────────────────────
// I2C section
// ─────────────────────────────────────────────────────────────────────
function I2C({
  connected, onSetup, onScan, onRead, onWrite,
}: {
  connected: boolean
  onSetup: () => void
  onScan: () => void
  onRead: (addr: number, reg: number, count: number) => void
  onWrite: (addr: number, data: number[]) => void
}) {
  const [addr, setAddr] = useState('0x68')
  const [reg, setReg] = useState('0x00')
  const [count, setCount] = useState('1')
  const [data, setData] = useState('0x00')

  const parseHex = (s: string): number => {
    const t = s.trim()
    return Number.parseInt(t.startsWith('0x') ? t.slice(2) : t, 16)
  }

  return (
    <Section title="I²C">
      {!connected && (
        <div className="rounded border border-slate-800 bg-slate-950/70 p-2 text-slate-500">
          Connect to a board first.
        </div>
      )}
      {connected && (
        <div onPointerDown={(e) => e.stopPropagation()}>
          <div className="mb-2 flex items-center gap-2">
            <ActionButton onClick={onSetup}>Setup</ActionButton>
            <ActionButton onClick={onScan}>Scan</ActionButton>
            <span className="text-slate-500">addresses arrive on the bus (response topic)</span>
          </div>
          <div className="grid grid-cols-[max-content_1fr_max-content_1fr] items-center gap-2">
            <label className="text-[10px] uppercase tracking-wider text-slate-500">addr</label>
            <input
              value={addr} onChange={(e) => setAddr(e.target.value)}
              className="nodrag nopan rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-xs text-slate-100"
            />
            <label className="text-[10px] uppercase tracking-wider text-slate-500">reg</label>
            <input
              value={reg} onChange={(e) => setReg(e.target.value)}
              className="nodrag nopan rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-xs text-slate-100"
            />
            <label className="text-[10px] uppercase tracking-wider text-slate-500">count</label>
            <input
              value={count} onChange={(e) => setCount(e.target.value)}
              className="nodrag nopan rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-xs text-slate-100"
            />
            <ActionButton
              onClick={() => onRead(parseHex(addr), parseHex(reg), Number.parseInt(count, 10) || 1)}
            >
              Read
            </ActionButton>
            <label className="text-[10px] uppercase tracking-wider text-slate-500">data</label>
            <input
              value={data} onChange={(e) => setData(e.target.value)}
              placeholder="0x01 0x02"
              className="nodrag nopan col-span-2 rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-xs text-slate-100"
            />
            <ActionButton
              onClick={() => {
                const bytes = data
                  .trim().split(/\s+/)
                  .map(parseHex)
                  .filter((n) => !Number.isNaN(n))
                onWrite(parseHex(addr), bytes)
              }}
            >
              Write
            </ActionButton>
          </div>
        </div>
      )}
    </Section>
  )
}

// ─────────────────────────────────────────────────────────────────────
// Sonar section
// ─────────────────────────────────────────────────────────────────────
function Sonar({
  proxyId, connected, onSetup, onRead,
}: {
  proxyId: string
  connected: boolean
  onSetup: (trig: number, echo: number) => void
  onRead: (trig: number) => void
}) {
  const wsClient = useWsClient()
  const [trig, setTrig] = useState('')
  const [echo, setEcho] = useState('')
  const [distance, setDistance] = useState<number | null>(null)
  const [lastSeen, setLastSeen] = useState<number | null>(null)

  // Subscribe to the trigger-pin's sonar topic once the user picks one.
  useEffect(() => {
    const t = Number.parseInt(trig, 10)
    if (!proxyId || Number.isNaN(t)) return
    const topic = `/arduino/${proxyId}/sonar/${t}`
    const off = wsClient.subscribe(topic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      const p = f.payload as { distance_cm?: number }
      if (typeof p?.distance_cm === 'number') {
        setDistance(p.distance_cm)
        setLastSeen(Date.now())
      }
    })
    return off
  }, [proxyId, trig, wsClient])

  return (
    <Section title="Sonar">
      {!connected && (
        <div className="rounded border border-slate-800 bg-slate-950/70 p-2 text-slate-500">
          Connect to a board first.
        </div>
      )}
      {connected && (
        <div className="flex items-center gap-2" onPointerDown={(e) => e.stopPropagation()}>
          <label className="text-[10px] uppercase tracking-wider text-slate-500">trigger</label>
          <input
            type="number" value={trig} onChange={(e) => setTrig(e.target.value)} placeholder="7"
            className="nodrag nopan w-12 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono text-xs text-slate-100"
          />
          <label className="text-[10px] uppercase tracking-wider text-slate-500">echo</label>
          <input
            type="number" value={echo} onChange={(e) => setEcho(e.target.value)} placeholder="8"
            className="nodrag nopan w-12 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono text-xs text-slate-100"
          />
          <ActionButton
            onClick={() => {
              const t = Number.parseInt(trig, 10)
              const e = Number.parseInt(echo, 10)
              if (!Number.isNaN(t) && !Number.isNaN(e)) onSetup(t, e)
            }}
            disabled={!trig || !echo}
          >
            Setup
          </ActionButton>
          <ActionButton
            onClick={() => {
              const t = Number.parseInt(trig, 10)
              if (!Number.isNaN(t)) onRead(t)
            }}
            disabled={!trig}
          >
            Read
          </ActionButton>
          <span className="ml-auto font-mono text-slate-200">
            {distance === null ? '—' : `${distance.toFixed(1)} cm`}
          </span>
          {lastSeen !== null && (
            <span className="text-[10px] text-slate-500">
              {Math.round((Date.now() - lastSeen) / 1000)}s ago
            </span>
          )}
        </div>
      )}
    </Section>
  )
}

// ─────────────────────────────────────────────────────────────────────
// Common sub-components
// ─────────────────────────────────────────────────────────────────────
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded border border-slate-800 bg-slate-900/40 p-3">
      <h3 className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-slate-400">
        {title}
      </h3>
      {children}
    </section>
  )
}

function ActionButton({
  children, onClick, disabled, tone = 'normal', type = 'button', title,
}: {
  children: React.ReactNode
  onClick?: () => void
  title?: string
  disabled?: boolean
  tone?: 'normal' | 'primary'
  type?: 'button' | 'submit'
}) {
  const base = tone === 'primary'
    ? 'bg-emerald-600 text-white hover:bg-emerald-500'
    : 'border border-slate-700 text-slate-200 hover:border-slate-500'
  return (
    <button
      type={type}
      onClick={(e) => { e.stopPropagation(); onClick?.() }}
      onPointerDown={(e) => e.stopPropagation()}
      disabled={disabled}
      title={title}
      className={`nodrag nopan rounded px-2 py-1 text-xs font-medium ${base} disabled:cursor-not-allowed disabled:opacity-40`}
    >
      {children}
    </button>
  )
}
