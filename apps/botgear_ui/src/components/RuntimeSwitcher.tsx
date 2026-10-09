import { useCallback, useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { Plus, X } from 'lucide-react'
import { useRuntimeConnections } from '../contexts/RuntimeConnectionsContext'
import { useActiveRuntime } from '../contexts/ActiveRuntimeContext'
import { runtimePrimary } from '../runtime/runtimeColors'
import type { RuntimeConnection, RuntimeConnectionState } from '../runtime/RuntimeConnection'
import { ConnectDialog } from './ConnectDialog'


/**
 * Top-of-page chip bar listing every connected runtime + an Add button.
 *
 * Design language matches the rest of the UI (slate surface, sky-400
 * accent for selection, the four-colour status grammar used by
 * service nodes). Per-runtime identity colour is kept as a 3px left
 * edge — sufficient for at-a-glance distinguishability without
 * competing with the surface theme.
 *
 *   ┌──────────────┐ ┌──────────────┐
 *   │▎● rlx        │ │▎● funny    × │   + Add runtime
 *   └──────────────┘ └──────────────┘
 *     ▲ active        ▲ inactive
 *     - slate-800 fill   - slate-900 / transparent
 *     - sky-400 border   - slate-700 border
 *
 *   ● = connection state dot (emerald/amber/rose)
 *   ▎ = 3px per-runtime colour edge
 *   × = disconnect (always visible at low contrast)
 */


interface DiscoveredPeer {
  key: string
  url: string
  remote_id: string | null
  state: string
}


// Status-dot colours — same grammar as service nodes (see
// pages/Composer.tsx STATUS_DOT). Kept inline so an outside reader
// doesn't need to know about the runtime-connection enum to read this
// component.
const CONN_STATE_DOT: Record<RuntimeConnectionState, string> = {
  connected: 'bg-emerald-500',
  connecting: 'bg-amber-500 animate-pulse',
  authenticating: 'bg-amber-500 animate-pulse',
  disconnected: 'bg-slate-600',
  error: 'bg-rose-500',
}
const CONN_STATE_LABEL: Record<RuntimeConnectionState, string> = {
  connected: 'connected',
  connecting: 'connecting',
  authenticating: 'authenticating',
  disconnected: 'disconnected',
  error: 'error — sign in or check URL',
}


export function RuntimeSwitcher() {
  const { connections, remove } = useRuntimeConnections()
  const { runtimeId: activeId, connection: activeConn } = useActiveRuntime()
  const navigate = useNavigate()
  const location = useLocation()

  const [dialogOpen, setDialogOpen] = useState(false)
  const [peerSuggestions, setPeerSuggestions] = useState<DiscoveredPeer[]>([])

  // Refresh peer suggestions every time the dialog opens — peers come
  // and go on the network in real time, so a stale list from mount
  // would mislead. Soft-fails: unauthed/disconnected runtime → empty.
  //
  // GET /v1/peers (peer_api.py, list_at_root=True) returns the peer
  // list bare — NOT wrapped in {peers: [...]}. This previously read
  // `r.peers`, which is always undefined against a bare array, so this
  // suggestion list was silently empty regardless of what peer_manager
  // actually knew about.
  const refreshPeers = useCallback(() => {
    if (!activeConn) { setPeerSuggestions([]); return }
    activeConn.apiFetch<DiscoveredPeer[]>('/v1/peers')
      .then((r) => setPeerSuggestions(r ?? []))
      .catch(() => setPeerSuggestions([]))
  }, [activeConn])

  useEffect(() => {
    if (!dialogOpen) return
    refreshPeers()
  }, [dialogOpen, refreshPeers])

  // "Rescan network" — rebuilds the runtime's mDNS engine (see
  // mdns.rescan's docstring: a laptop's Wi-Fi routinely isn't up yet
  // when the backend boots, so it never joins multicast on the real
  // interface until something re-triggers it). Auto-connect then picks
  // up any peer the rebuilt browser (re)discovers, so we give it a
  // moment before re-reading /v1/peers rather than expecting the POST
  // itself to return the new peer.
  const rescanPeers = useCallback(async () => {
    if (!activeConn) return
    try {
      await activeConn.apiFetch('/v1/peers-request', {
        method: 'POST',
        body: JSON.stringify({ action: 'rescan' }),
      })
    } catch {
      // best-effort — refreshPeers below still shows whatever's there
    }
    await new Promise((resolve) => setTimeout(resolve, 1500))
    refreshPeers()
  }, [activeConn, refreshPeers])

  const switchTo = useCallback((id: string) => {
    // Preserve the sub-path: ``/r/witty-gizmo/topology`` → ``/r/funny-droid/topology``.
    // location.pathname is something like ``/r/witty-gizmo/topology``;
    // strip the current ``/r/<id>`` prefix and substitute.
    const m = location.pathname.match(/^\/r\/[^/]+(\/.*)?$/)
    const suffix = m?.[1] ?? '/workspaces/runtime'
    navigate(`/r/${encodeURIComponent(id)}${suffix}${location.search}${location.hash}`)
  }, [navigate, location])

  const onAdd = useCallback((newId: string) => {
    setDialogOpen(false)
    switchTo(newId)
  }, [switchTo])

  // Disconnecting the runtime the URL currently points at must navigate
  // AWAY first, before the connection disappears from the map — the
  // active-runtime route (/r/:runtimeId/*) requires a matching
  // connection to exist and everything under it (useApiFetch,
  // useWsClient, etc.) throws when it doesn't. ActiveRuntimeProvider now
  // has a safety-net redirect for this too, but doing it here means the
  // user goes straight to the next connection's canvas instead of
  // bouncing through it.
  const removeConnection = useCallback((id: string) => {
    if (id === activeId) {
      const remaining = connections.filter((c) => c.id !== id)
      if (remaining.length === 0) {
        navigate('/no-runtimes')
      } else {
        const m = location.pathname.match(/^\/r\/[^/]+(\/.*)?$/)
        const suffix = m?.[1] ?? '/workspaces/runtime'
        navigate(`/r/${encodeURIComponent(remaining[0].id)}${suffix}${location.search}${location.hash}`)
      }
    }
    remove(id)
  }, [activeId, connections, remove, navigate, location])

  return (
    <>
      <div className="flex items-center gap-1.5 border-b border-slate-800 bg-slate-950 px-3 py-1.5">
        <div className="flex flex-1 items-center gap-1.5 overflow-x-auto">
          {connections.length === 0 && (
            <span className="text-[11px] text-slate-500">
              No runtimes connected — add one with the + button →
            </span>
          )}
          {connections.map((c) => (
            <RuntimeChip
              key={c.id}
              connection={c}
              isActive={c.id === activeId}
              onClick={() => switchTo(c.id)}
              onRemove={() => removeConnection(c.id)}
            />
          ))}
        </div>
        <button
          type="button"
          onClick={() => setDialogOpen(true)}
          title="Add a runtime"
          className="flex shrink-0 items-center gap-1 rounded border border-sky-700 px-2 py-1 text-[11px] text-sky-300 hover:border-sky-500 hover:bg-sky-900/30 hover:text-sky-200"
        >
          <Plus className="h-3 w-3" />
          <span>Add runtime</span>
        </button>
      </div>

      {dialogOpen && (
        <ConnectDialog
          discoveredPeers={peerSuggestions}
          onRescan={activeConn ? rescanPeers : undefined}
          onClose={() => setDialogOpen(false)}
          onConnected={onAdd}
        />
      )}
    </>
  )
}


/** One runtime chip. Re-renders on connection-state changes via the
 * RuntimeConnection.subscribe listener; that's how the status dot
 * updates live (no polling). */
function RuntimeChip({
  connection, isActive, onClick, onRemove,
}: {
  connection: RuntimeConnection
  isActive: boolean
  onClick: () => void
  onRemove: () => void
}) {
  // Local re-render trigger: bumped whenever the connection emits a
  // state change (auth token refresh, ws reconnect, meta.runtime_id
  // arrival from the bus). Cheap — listener set is tiny.
  const [, force] = useState(0)
  useEffect(() => connection.subscribe(() => force((n) => n + 1)), [connection])

  const id = connection.id
  const label = connection.meta.runtime_id ?? id
  const url = connection.url
  const state = connection.state
  const accent = runtimePrimary(id)
  return (
    <div
      onClick={onClick}
      title={`${label}\n${url}\nstatus: ${CONN_STATE_LABEL[state]}`}
      className={`group relative flex shrink-0 cursor-pointer items-center gap-2 overflow-hidden rounded border pl-2.5 pr-2 py-1 text-[11px] transition-colors ${
        isActive
          ? 'border-sky-400 bg-slate-800 text-slate-100'
          : 'border-slate-700 bg-slate-900 text-slate-300 hover:border-slate-600 hover:bg-slate-850 hover:text-slate-100'
      }`}
    >
      {/* Per-runtime colour stripe — 3px wide, full chip height.
          Identity signal without dominating the surface. */}
      <span
        aria-hidden
        className="absolute left-0 top-0 h-full w-[3px]"
        style={{ background: accent }}
      />
      {/* Connection-state dot. */}
      <span
        aria-label={`connection: ${CONN_STATE_LABEL[state]}`}
        className={`h-2 w-2 shrink-0 rounded-full ${CONN_STATE_DOT[state]}`}
      />
      <span className={`font-mono ${isActive ? 'font-semibold' : ''}`}>
        {label}
      </span>
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); onRemove() }}
        onPointerDown={(e) => e.stopPropagation()}
        title="Disconnect this runtime"
        className="ml-1 rounded p-0.5 text-slate-600 opacity-60 transition-colors hover:bg-rose-900/40 hover:text-rose-300 hover:opacity-100"
      >
        <X className="h-3 w-3" />
      </button>
    </div>
  )
}
