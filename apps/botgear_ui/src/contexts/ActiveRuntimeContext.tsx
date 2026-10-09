import { createContext, useContext, useEffect, useMemo, type ReactNode } from 'react'
import { Navigate, useLocation, useParams } from 'react-router-dom'
import type { RuntimeConnection } from '../runtime/RuntimeConnection'
import type { WsClient } from '../runtime/wsClient'
import { loadActiveId, saveActiveId } from '../runtime/connectionsStorage'
import { useRuntimeConnections } from './RuntimeConnectionsContext'


/**
 * Active-runtime context: which runtime is the current page targeting?
 *
 * Mounted INSIDE each ``/r/:runtimeId/*`` route (see App.tsx). Reads
 * the URL param, looks up the matching RuntimeConnection from
 * RuntimeConnectionsContext, exposes its ws + apiFetch through hooks.
 *
 * Pages that today do ``import { wsClient } from '../runtime/wsClient'``
 * migrate to:
 *
 *   import { useWsClient } from '../contexts/ActiveRuntimeContext'
 *   const wsClient = useWsClient()
 *
 * Same surface area (``subscribe``, ``publish``, ``listTopics``, etc.)
 * — what changes is that the instance is now per-active-runtime
 * instead of a process-singleton.
 *
 * Pages that haven't migrated yet keep using the singleton; the
 * singleton stays bound to ``location.origin`` (today's seed
 * runtime), so they still work — just always against the seed runtime
 * regardless of which one the URL says is active. Phase 5 + 6 finish
 * the migration as those pages get touched.
 */

interface ActiveRuntimeValue {
  /** The id from the URL — may not match a real connection if the
   * user landed via a stale bookmark. */
  runtimeId: string
  /** The resolved connection. Null when ``runtimeId`` doesn't match
   * anything in the connections map — pages should render a "not
   * connected" placeholder in that case. */
  connection: RuntimeConnection | null
}


const Context = createContext<ActiveRuntimeValue | null>(null)


/** Mount inside a ``/r/:runtimeId/*`` route. Reads the param + looks
 * up the connection. Persists the active id back to localStorage so
 * page reload restores the same view. */
export function ActiveRuntimeProvider({ children }: { children: ReactNode }) {
  const { runtimeId = '' } = useParams<{ runtimeId: string }>()
  const { connections, get } = useRuntimeConnections()
  const location = useLocation()

  // Decode the param — runtime ids can contain ``-`` and ``.`` etc.
  // but they pass through URL paths cleanly. Decode in case anything
  // upstream encoded special chars (e.g. ``%40`` for ``@``).
  const decodedId = useMemo(() => {
    try { return decodeURIComponent(runtimeId) } catch { return runtimeId }
  }, [runtimeId])

  const connection = get(decodedId) ?? null

  // Persist active id whenever it changes so reload restores the
  // view. Skips when the URL points at a non-existent connection
  // (don't lock the user into a broken bookmark).
  useEffect(() => {
    if (connection) saveActiveId(decodedId)
  }, [decodedId, connection])

  // All hooks above run unconditionally on every render — the redirect
  // below is a plain early return of JSX, not a hook, so it's safe
  // after this point (Rules of Hooks only constrains hook CALLS).
  const value = useMemo<ActiveRuntimeValue>(() => ({
    runtimeId: decodedId,
    connection,
  }), [decodedId, connection])

  // The URL names a connection that no longer exists — removed via the
  // chip bar's disconnect button, or a stale/bookmarked id. Every
  // strict consumer below (useWsClient/useApiFetch) THROWS on a null
  // connection, and nothing catches that render-time throw, so letting
  // ``children`` render here blanks the entire app (see the crash this
  // fixes: removing the active runtime left RuntimeNav rendering
  // against a dangling id). Bounce to another known connection instead
  // — same fallback order as RedirectToActive — or /no-runtimes if none
  // remain. RuntimeSwitcher's remove flow navigates proactively before
  // calling remove() so this is normally a no-op safety net, not the
  // primary path.
  if (!connection) {
    if (connections.length === 0) {
      return <Navigate to="/no-runtimes" replace />
    }
    const persistedId = loadActiveId()
    const fallback = (persistedId ? connections.find((c) => c.id === persistedId) : undefined) ?? connections[0]
    const m = location.pathname.match(/^\/r\/[^/]+(\/.*)?$/)
    const suffix = m?.[1] ?? ''
    const next = `/r/${encodeURIComponent(fallback.id)}${suffix}${location.search}${location.hash}`
    return <Navigate to={next} replace />
  }

  return <Context.Provider value={value}>{children}</Context.Provider>
}


/** Full active-runtime details. Useful for pages that need to render
 * conditionally on connection state (e.g. show a "log in to this
 * runtime" banner when ``connection.state !== 'connected'``). */
export function useActiveRuntime(): ActiveRuntimeValue {
  const v = useContext(Context)
  if (!v) throw new Error('useActiveRuntime must be used inside <ActiveRuntimeProvider> (a /r/:runtimeId/* route)')
  return v
}


/** Safe variant — returns null when called outside a provider instead
 * of throwing. Use this in chrome components like ConnectDialog that
 * can be rendered EITHER inside a /r/:runtimeId/* route (where they
 * may bridge auth from the active connection) OR from the no-runtimes
 * empty state (where there IS no active connection). The caller is
 * responsible for null-checking before reaching for ``connection``. */
export function useActiveRuntimeOptional(): ActiveRuntimeValue | null {
  return useContext(Context) ?? null
}


/** Shortcut for the common case: just the WsClient. Throws when
 * there's no active connection — page should guard with
 * useActiveRuntime().connection if it wants to render a placeholder
 * instead. */
export function useWsClient(): WsClient {
  const { connection, runtimeId } = useActiveRuntime()
  if (!connection) throw new Error(`useWsClient: no connection for runtime ${runtimeId}`)
  return connection.ws
}


/** Same convenience for apiFetch. */
export function useApiFetch() {
  const { connection, runtimeId } = useActiveRuntime()
  if (!connection) throw new Error(`useApiFetch: no connection for runtime ${runtimeId}`)
  return connection.apiFetch
}
