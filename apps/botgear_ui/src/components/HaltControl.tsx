// Runtime-wide HALT — the one Stop control, always on screen.
//
// Pressing Stop (or Ctrl+Alt+H anywhere in the UI) POSTs /v1/system/halt,
// which broadcasts /runtime/halt: every service stops what it's doing at
// once — speech is cut mid-word, playback flushed, queued utterances
// dropped, every brain run cancelled (and no new run starts until Resume).
// The same broadcast also comes from a spoken stop word (the STT's
// spotter) or the keyboard service's halt hotkey; the retained
// /runtime/halt_state topic tells every open UI which, so the banner shows
// up no matter where the stop came from.
import { useCallback, useEffect, useState } from 'react'
import { OctagonX, Play } from 'lucide-react'
import { useApiFetch, useWsClient } from '../contexts/ActiveRuntimeContext'
import type { InboundFrame } from '../runtime/wsClient'

export interface HaltState {
  halted: boolean
  level: 'stop' | 'emergency' | null
  source: string | null
  reason: string | null
  since: string | null
}

const HALT_STATE_TOPIC = '/runtime/halt_state'

export function useHaltState() {
  const apiFetch = useApiFetch()
  const ws = useWsClient()
  const [state, setState] = useState<HaltState | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    apiFetch<HaltState>('/v1/system/halt')
      .then((s) => { if (alive) setState(s) })
      .catch(() => { /* older backend without /system/halt — control stays usable */ })
    const off = ws.subscribe(HALT_STATE_TOPIC, (frame: InboundFrame) => {
      if (frame.method === 'message' && frame.payload) setState(frame.payload as HaltState)
    })
    return () => { alive = false; off() }
  }, [apiFetch, ws])

  const post = useCallback(async (path: string, body: Record<string, unknown>) => {
    setBusy(true)
    setError(null)
    try {
      setState(await apiFetch<HaltState>(path, { method: 'POST', body: JSON.stringify(body) }))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }, [apiFetch])

  const halt = useCallback((reason = 'Stop button') => post('/v1/system/halt', { level: 'stop', reason }), [post])
  const resume = useCallback(() => post('/v1/system/resume', {}), [post])
  return { state, busy, error, halt, resume }
}

/** Nav-bar control: a red Stop button, or Resume while halted. Owns the
 *  Ctrl+Alt+H shortcut (same combo as the keyboard service's default
 *  halt_combo, so muscle memory works whether or not one is running). */
export default function HaltControl() {
  const { state, busy, error, halt, resume } = useHaltState()

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.altKey && !e.shiftKey && !e.metaKey && e.code === 'KeyH' && !e.repeat) {
        e.preventDefault()
        void halt('Ctrl+Alt+H in the UI')
      }
    }
    window.addEventListener('keydown', onKey, { capture: true })
    return () => window.removeEventListener('keydown', onKey, { capture: true })
  }, [halt])

  if (state?.halted) {
    return (
      <button
        type="button"
        onClick={() => void resume()}
        disabled={busy}
        title={`Halted by ${state.source ?? 'unknown'}${state.reason ? ` — ${state.reason}` : ''}. Click to resume.`}
        className="mr-1 flex items-center gap-1.5 rounded border border-amber-500 bg-amber-500/15 px-2.5 py-1 text-xs font-semibold uppercase tracking-wide text-amber-200 hover:bg-amber-500/25 disabled:opacity-50"
      >
        <Play className="h-3.5 w-3.5" />
        Resume
      </button>
    )
  }
  return (
    <button
      type="button"
      onClick={() => void halt()}
      disabled={busy}
      title={error ? `Stop failed: ${error}` : 'Stop everything now — speech, playback, brain runs (Ctrl+Alt+H)'}
      className={`mr-1 flex items-center gap-1.5 rounded border px-2.5 py-1 text-xs font-semibold uppercase tracking-wide disabled:opacity-50 ${
        error
          ? 'border-rose-400 bg-rose-600 text-white'
          : 'border-rose-600 bg-rose-600/90 text-white hover:bg-rose-500'
      }`}
    >
      <OctagonX className="h-3.5 w-3.5" />
      Stop
    </button>
  )
}

/** Full-width strip under the nav while halted: who stopped it, why, and
 *  a Resume button — visible on every page. */
export function HaltBanner() {
  const { state, busy, resume } = useHaltState()
  if (!state?.halted) return null
  const emergency = state.level === 'emergency'
  return (
    <div
      role="alert"
      className={`flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-3 py-1.5 text-sm ${
        emergency ? 'border-rose-700 bg-rose-950 text-rose-100' : 'border-amber-700 bg-amber-950 text-amber-100'
      }`}
    >
      <OctagonX className="h-4 w-4 shrink-0" />
      <span className="font-semibold">{emergency ? 'EMERGENCY STOP' : 'Halted'}</span>
      <span className="min-w-0 flex-1 truncate text-xs opacity-80">
        by {state.source ?? 'unknown'}{state.reason ? ` — ${state.reason}` : ''}. Speech, playback and
        brain runs were stopped; new workflow runs are refused until you resume.
      </span>
      <button
        type="button"
        onClick={() => void resume()}
        disabled={busy}
        className="rounded border border-current px-2 py-0.5 text-xs font-semibold uppercase tracking-wide hover:bg-white/10 disabled:opacity-50"
      >
        Resume
      </button>
    </div>
  )
}
