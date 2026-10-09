// XrView — full-page host shell for a service's immersive WebXR client
// (xr.js). Opened ON THE HEADSET at /r/:runtimeId/xr/:proxyId.
//
// Same auth + per-runtime bus wiring as DockView (ProtectedRoute +
// ActiveRuntimeProvider, no RuntimeLayout chrome). Unlike a normal
// Option-B bundle (ui.js, loaded via React.lazy + rendered as a JSX
// child sharing this app's React), xr.js is a STANDALONE bundle — its
// default export is a plain `mount(container, props, onError) =>
// unmountFn` function, not a component (see xr/View.tsx's top-of-file
// comment for why: @react-three/fiber vendors its own react-reconciler,
// and sharing this app's React singleton across that boundary broke
// repeatedly on React's unstable internals as the two versions drifted).
// So this shell dynamically imports the module, then imperatively mounts
// it into a plain <div> it owns — passing `ws` as a prop since the
// mounted tree has no access to this app's Context providers.
import { useEffect, useReducer, useRef, useState, type ReactNode } from 'react'
import { useParams } from 'react-router-dom'

import { useActiveRuntime } from '../contexts/ActiveRuntimeContext'
import type { ServiceProxy } from '../models/ServiceProxy'
import type { WsClient } from '../runtime/wsClient'

type XrMount = (
  container: HTMLElement,
  props: { proxy: ServiceProxy; ws: WsClient },
  onError?: (error: unknown) => void,
) => () => void

// Memoize the import per metaId so a remount doesn't re-fetch the
// (large) immersive bundle.
const moduleCache = new Map<string, Promise<{ default: XrMount }>>()
// Per-page-load cache-bust — see DynamicServiceView for the rationale
// (constant bundle URL would otherwise serve a stale module after a
// rebuild despite the backend's no-cache header).
const LOAD_TOKEN = Date.now().toString(36)
function moduleFor(metaId: string): Promise<{ default: XrMount }> {
  let mod = moduleCache.get(metaId)
  if (!mod) {
    const [name, version] = metaId.split('@')
    const url = `/repo/${encodeURIComponent(name)}/${encodeURIComponent(version)}/xr.js?t=${LOAD_TOKEN}`
    mod = import(/* @vite-ignore */ url) as Promise<{ default: XrMount }>
    moduleCache.set(metaId, mod)
  }
  return mod
}


export default function XrView() {
  const { proxyId = '' } = useParams<{ proxyId: string }>()
  const { runtimeId, connection } = useActiveRuntime()
  const decodedId = (() => { try { return decodeURIComponent(proxyId) } catch { return proxyId } })()

  const [proxy, setProxy] = useState<ServiceProxy | null>(null)
  const [error, setError] = useState<string | null>(null)

  // RuntimeConnection mutates state in place; subscribe to re-render on
  // connect (same pattern as DockView) and kick the socket open.
  const [, forceRender] = useReducer((n: number) => n + 1, 0)
  useEffect(() => {
    if (!connection) return
    const off = connection.subscribe(forceRender)
    connection.ws.connect()
    return off
  }, [connection])

  useEffect(() => {
    document.title = `${decodedId} — WebXR`
  }, [decodedId])

  useEffect(() => {
    if (!connection || connection.state !== 'connected') return
    let cancelled = false
    setError(null)
    connection
      .apiFetch<ServiceProxy[]>('/v1/service-proxy-list')
      .then((list) => {
        if (cancelled) return
        const found = (list ?? []).find((p) => (p.id ?? p.name) === decodedId)
        if (found) setProxy(found)
        else setError(`No service "${decodedId}" on ${runtimeId}.`)
      })
      .catch((err: Error) => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [connection, connection?.state, decodedId, runtimeId])

  const metaId = proxy?.service_meta_id
  const ws = connection?.ws
  const containerRef = useRef<HTMLDivElement | null>(null)
  const [mountError, setMountError] = useState<unknown>(null)
  const [mounted, setMounted] = useState(false)

  useEffect(() => {
    if (!metaId || !ws || !proxy) return
    let cancelled = false
    let unmount: (() => void) | null = null
    setMountError(null)
    setMounted(false)
    moduleFor(metaId)
      .then((mod) => {
        if (cancelled || !containerRef.current) return
        unmount = mod.default(containerRef.current, { proxy, ws }, (err) => {
          if (!cancelled) setMountError(err)
        })
        setMounted(true)
      })
      .catch((err: unknown) => { if (!cancelled) setMountError(err) })
    return () => {
      cancelled = true
      unmount?.()
    }
  }, [metaId, ws, proxy])

  if (!connection) return <Msg>Not connected to runtime {runtimeId}. Open from the main window first.</Msg>
  if (connection.state !== 'connected' && !proxy) return <Msg>Connecting to {runtimeId}…</Msg>
  if (error && !proxy) return <Msg tone="error">{error}</Msg>
  if (!proxy) return <Msg>Loading…</Msg>
  if (!metaId) return <Msg tone="error">Service has no bundle id.</Msg>

  return (
    <>
      {mountError != null && (
        <Msg tone="error">
          Failed to load the immersive bundle (xr.js).
          <br />
          <span style={{ fontSize: 11, opacity: 0.8, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
            {formatLoadError(mountError)}
          </span>
        </Msg>
      )}
      {mountError == null && !mounted && <Msg>Loading immersive client…</Msg>}
      {/* Always rendered (not conditionally, so containerRef exists before the
          effect above needs it) — xr.js's own mount() fills this and owns
          everything inside it full-page; the Msg overlays above sit on top
          via their own position:fixed until it's ready. */}
      <div ref={containerRef} style={{ position: 'fixed', inset: 0 }} />
    </>
  )
}

// Headsets can't open devtools, so this on-screen text is often the ONLY
// place the real failure is ever visible — include the stack (not just
// the message) since a bare "x is not a function" without a location is
// rarely enough to act on.
function formatLoadError(err: unknown): string {
  if (err instanceof Error) return err.stack ?? `${err.name}: ${err.message}`
  try { return JSON.stringify(err) } catch { return String(err) }
}


function Msg({ children, tone = 'muted' }: { children: ReactNode; tone?: 'muted' | 'error' }) {
  return (
    <div style={{ position: 'fixed', inset: 0, display: 'flex', alignItems: 'center',
      justifyContent: 'center', background: '#0b1220', padding: 24, textAlign: 'center',
      fontFamily: 'system-ui, sans-serif', fontSize: 14,
      color: tone === 'error' ? '#fda4af' : '#94a3b8' }}>
      {children}
    </div>
  )
}
