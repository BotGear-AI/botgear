import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useApiFetch, useWsClient } from '../contexts/ActiveRuntimeContext'
import { useAuth } from '../contexts/AuthContext'
import type { InboundFrame } from '../runtime/wsClient'
import Banner from '../components/Banner'

// Updates page — the self-update feature (docs/TODO_UPGRADE_FEATURE.md).
//  • Check for updates  → GET /v1/update/check (framework current-vs-latest).
//  • Update now         → POST /v1/update/framework, stream
//    /system/update/progress, then poll /v1/version across the restart until
//    it flips to the new version.

interface UpdateCheck {
  current: string
  latest: string | null
  update_available: boolean
  reason: string
  platform: string
  repo: string
  asset_url: string | null
  sha256_url: string | null
  published_at: string | null
  notes: string | null
  checked_at: string
  error: string | null
}

interface Progress {
  phase: string
  pct: number | null
  message: string
}

const bare = (v: string | null | undefined) => (v ?? '').replace(/^v/, '')
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

export default function Updates() {
  const apiFetch = useApiFetch()
  const wsClient = useWsClient()
  const { user } = useAuth()
  const isAdmin = useMemo(() => (user?.roles ?? []).includes('Admin'), [user])

  const [result, setResult] = useState<UpdateCheck | null>(null)
  const [checking, setChecking] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [updating, setUpdating] = useState(false)
  const [progress, setProgress] = useState<Progress | null>(null)
  const [updatedTo, setUpdatedTo] = useState<string | null>(null)
  const offRef = useRef<(() => void) | null>(null)

  useEffect(() => () => { offRef.current?.() }, [])

  const check = useCallback(async () => {
    setChecking(true); setError(null)
    try {
      setResult(await apiFetch<UpdateCheck>('/v1/update/check'))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setChecking(false)
    }
  }, [apiFetch])

  const doUpdate = useCallback(async () => {
    if (!result?.update_available) return
    setUpdating(true); setError(null); setProgress(null); setUpdatedTo(null)

    // Live phase stream (download% / verify / drain / carry / restarting).
    offRef.current?.()
    offRef.current = wsClient.subscribe('/system/update/progress', (f: InboundFrame) => {
      if (f.method === 'message') setProgress(f.payload as Progress)
    })

    try {
      const resp = await apiFetch<{ accepted: boolean; target: string | null; message: string }>(
        '/v1/update/framework', { method: 'POST', body: JSON.stringify({}) },
      )
      if (!resp.accepted) {
        setError(resp.message); setUpdating(false); offRef.current?.(); offRef.current = null
        return
      }
      const target = bare(resp.target)

      // Poll /v1/version across the re-exec. apiFetch throws during the
      // down window (connection drops + auto-reconnects) — keep polling.
      const deadline = Date.now() + 10 * 60 * 1000
      let landed = false
      while (Date.now() < deadline) {
        await sleep(2500)
        try {
          const v = await apiFetch<{ version: string }>('/v1/version')
          if (bare(v.version) === target) { landed = true; break }
        } catch { /* runtime is mid-restart — retry */ }
      }
      if (landed) setUpdatedTo(resp.target)
      else setError('timed out waiting for the new version to start')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setUpdating(false)
      offRef.current?.(); offRef.current = null
      void check()  // refresh current-vs-latest
    }
  }, [result, apiFetch, wsClient, check])

  if (!isAdmin) {
    return (
      <div className="mx-auto max-w-2xl space-y-4 p-6 text-sm text-slate-300">
        <h1 className="text-lg font-semibold">Updates</h1>
        <Banner tone="error">Admin role required to manage updates on this runtime.</Banner>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-6">
      <header className="space-y-1">
        <h1 className="text-lg font-semibold">Updates</h1>
        <p className="text-xs text-slate-400">
          Check for a newer botgear framework release for this runtime's platform,
          and update in place — your data and configuration are backed up and carried
          across the restart.
        </p>
      </header>

      {error && <Banner tone="error">{error}</Banner>}
      {updatedTo && <Banner tone="success">Updated to {updatedTo} — the runtime restarted on the new version.</Banner>}

      <div>
        <button
          type="button"
          onClick={check}
          disabled={checking || updating}
          className="rounded bg-sky-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-500 disabled:opacity-50"
        >
          {checking ? 'Checking…' : 'Check for updates'}
        </button>
      </div>

      {result && (
        <div className="space-y-3 rounded-lg border border-slate-700 bg-slate-900 p-4 text-sm">
          <div className="flex items-center gap-3">
            <span className="font-mono text-slate-200">{result.current}</span>
            <span className="text-slate-500">→</span>
            <span className="font-mono text-slate-200">{result.latest ?? '—'}</span>
            {result.error ? (
              <span className="rounded bg-amber-900/60 px-2 py-0.5 text-[11px] font-medium text-amber-200">couldn't check</span>
            ) : result.update_available ? (
              <span className="rounded bg-emerald-900 px-2 py-0.5 text-[11px] font-medium text-emerald-200">update available</span>
            ) : (
              <span className="rounded bg-slate-700 px-2 py-0.5 text-[11px] font-medium text-slate-300">up to date</span>
            )}
          </div>

          <div className="text-xs text-slate-400">{result.reason}</div>
          {result.error && <div className="font-mono text-[11px] text-amber-300/80">{result.error}</div>}

          <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-xs text-slate-400">
            <dt>Platform</dt><dd className="font-mono text-slate-300">{result.platform}</dd>
            <dt>Source</dt><dd className="font-mono text-slate-300">{result.repo}</dd>
            {result.published_at && (<><dt>Published</dt><dd className="text-slate-300">{new Date(result.published_at).toLocaleString()}</dd></>)}
          </dl>

          {result.notes && (
            <details className="text-xs">
              <summary className="cursor-pointer text-slate-400 hover:text-slate-200">Release notes</summary>
              <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded bg-slate-950 p-2 text-[11px] text-slate-300">{result.notes}</pre>
            </details>
          )}

          {updating && progress && (
            <div className="space-y-1 rounded border border-slate-700 bg-slate-950 p-2">
              <div className="flex items-center justify-between text-xs">
                <span className="font-medium text-sky-300">{progress.phase}</span>
                {progress.pct != null && <span className="font-mono text-slate-400">{progress.pct}%</span>}
              </div>
              <div className="text-[11px] text-slate-400">{progress.message}</div>
              {progress.pct != null && (
                <div className="h-1 overflow-hidden rounded bg-slate-800">
                  <div className="h-full bg-sky-500 transition-all" style={{ width: `${progress.pct}%` }} />
                </div>
              )}
            </div>
          )}

          <div className="pt-1">
            <button
              type="button"
              onClick={doUpdate}
              disabled={!result.update_available || updating}
              className="rounded bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {updating ? 'Updating…' : `Update now${result.latest ? ` → ${result.latest}` : ''}`}
            </button>
            {updating && (
              <span className="ml-3 text-[11px] text-slate-500">
                The runtime will restart; this page reconnects automatically.
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
