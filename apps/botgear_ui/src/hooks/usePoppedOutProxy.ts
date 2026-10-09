// usePoppedOutProxy — connection + proxy-fetch plumbing for a
// popped-out, single-service browser window (DockView).
//
// Resolves the active runtime connection from the URL, forces a
// re-render across the connecting→connected transition (the
// RuntimeConnection mutates its ``state`` in place rather than
// swapping the object reference, so nothing else would notice), and
// fetches the ServiceProxy record the view needs once connected.
// Extracted from DockView.tsx as its own hook — everything page-specific
// (which composer-view shape to render, lifecycle actions, window
// chrome) stays in the page itself.
import { useEffect, useReducer, useState, type Dispatch, type SetStateAction } from 'react'
import { useParams } from 'react-router-dom'

import { useActiveRuntime } from '../contexts/ActiveRuntimeContext'
import type { RuntimeConnection } from '../runtime/RuntimeConnection'
import type { ServiceProxy } from '../models/ServiceProxy'


interface PoppedOutProxy {
  runtimeId: string
  connection: RuntimeConnection | null
  /** The proxy id from the URL, percent-decoded (ids can contain
   *  ``@``, ``.``, ``-``). */
  decodedId: string
  proxy: ServiceProxy | null
  error: string | null
  setProxy: Dispatch<SetStateAction<ServiceProxy | null>>
  setError: Dispatch<SetStateAction<string | null>>
}


export function usePoppedOutProxy(): PoppedOutProxy {
  const { proxyId = '' } = useParams<{ proxyId: string }>()
  const { runtimeId, connection } = useActiveRuntime()
  const decodedId = decodeProxyId(proxyId)

  const [proxy, setProxy] = useState<ServiceProxy | null>(null)
  const [error, setError] = useState<string | null>(null)

  // RuntimeConnection mutates ``state`` in place + notifies listeners;
  // it does NOT swap the object reference, so nothing else would
  // re-render to notice a connecting→connected transition. Subscribe
  // directly and force one on every notify. Also kick the socket open
  // explicitly — idempotent; covers the case where nothing else has
  // subscribed yet to trigger the lazy connect.
  const [, forceRender] = useReducer((n: number) => n + 1, 0)
  useEffect(() => {
    if (!connection) return
    const off = connection.subscribe(forceRender)
    connection.ws.connect()
    return off
  }, [connection])

  // Fetch the proxy record once the connection is live. Re-runs if the
  // connection flips to connected after an initial disconnected render.
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
      .catch((err: Error) => {
        if (!cancelled) setError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [connection, connection?.state, decodedId, runtimeId])

  return { runtimeId, connection, decodedId, proxy, error, setProxy, setError }
}


function decodeProxyId(raw: string): string {
  try {
    return decodeURIComponent(raw)
  } catch {
    return raw
  }
}
