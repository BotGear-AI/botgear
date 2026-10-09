// Modular UI for the local (offline) speech-to-text service. Drives the
// shared `transcription` control interface: pick which microphone topic to
// transcribe, start/stop continuous recognition, mute, choose a model — and
// watch live partials + finalized utterances stream in on /stt/{id}/text.
import { useCallback, useEffect, useRef, useState } from 'react'
import { useWsClient, MicrophoneSourcePicker, type InboundFrame, type ServiceProxy } from '@rlx/ui'

interface Model { id: string; label?: string; downloaded?: boolean }
interface SttState {
  model?: string | null; models?: Model[]
  input_kind?: string | null; input_ref?: string | null
  sample_rate?: number; muted?: boolean; listening?: boolean; continuous?: boolean
  downloading?: boolean; ready?: boolean; queued?: number
  level_rms?: number; last_partial?: string; last_final?: string; last_error?: string | null; source?: string
  recording_transcript?: boolean; transcript_path?: string | null
  transcript_suggested_path?: string | null; transcript_lines?: number
}
interface TextFrame { text?: string; final?: boolean; seq?: number; ts?: number }

export default function SttLocalView({ proxy }: { proxy: ServiceProxy }) {
  const wsClient = useWsClient()
  const proxyId = proxy.id ?? proxy.name ?? ''
  const T = (s: string) => `/stt/${proxyId}/${s}`

  const [state, setState] = useState<SttState>({})
  const [ref, setRef] = useState('')
  const [partial, setPartial] = useState('')
  const [finals, setFinals] = useState<string[]>([])
  const [filename, setFilename] = useState('')
  const refDirty = useRef(false)
  const filenameEdited = useRef(false)

  const sendAction = useCallback(
    (action: string, args: Record<string, unknown> = {}) => wsClient.publish(T('control'), { action, ...args }),
    [wsClient, proxyId],
  )

  useEffect(() => {
    if (!proxyId) return
    const offState = wsClient.subscribe(T('state'), (f: InboundFrame) => {
      if (f.method !== 'message') return
      const s = (f.payload as SttState) ?? {}; setState(s)
      if (!refDirty.current) setRef(s.input_ref ?? '')
      if (!filenameEdited.current && s.transcript_suggested_path) setFilename(s.transcript_suggested_path)
    })
    const offText = wsClient.subscribe(T('text'), (f: InboundFrame) => {
      if (f.method !== 'message') return
      const t = f.payload as TextFrame
      const txt = (t?.text ?? '').trim()
      if (t?.final) {
        setPartial('')
        if (txt) setFinals((prev) => [...prev.slice(-49), txt])
      } else {
        setPartial(txt)
      }
    })
    sendAction('list_models')
    return () => { offState(); offText() }
  }, [proxyId, wsClient, sendAction])

  // Keep the transcript scrolled to the newest line.
  const logRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => { const el = logRef.current; if (el) el.scrollTop = el.scrollHeight }, [finals, partial])

  const applyInput = () => {
    sendAction('select_input', { kind: ref.trim() ? 'topic' : null, ref: ref.trim() || null })
    refDirty.current = false
  }
  const muted = !!state.muted
  const listening = !!state.listening
  const recordingTranscript = !!state.recording_transcript
  const models = state.models ?? []
  const level = muted ? 0 : (state.level_rms ?? 0)
  const status = state.downloading ? 'downloading model…'
    : !state.ready && listening ? 'starting…'
    : listening ? 'listening' : 'idle'

  return (
    <div className="rlx-drag-handle space-y-3 p-3 text-sm text-slate-200">
      <div className="flex items-center justify-between">
        <span className="font-semibold">Speech-to-text</span>
        <span className="text-[10px] uppercase tracking-wide text-cyan-400">{state.source ?? 'local'}</span>
      </div>

      {/* Source microphone */}
      <label className="block space-y-1">
        <span className="text-xs text-slate-400">Microphone</span>
        <div className="flex gap-2">
          <MicrophoneSourcePicker
            value={ref}
            onChange={(topic) => { setRef(topic); refDirty.current = true }}
            className="nodrag nopan min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-2 py-1 text-xs text-slate-200"
          />
          <button type="button" onClick={applyInput}
            className="nodrag nopan rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:border-slate-500"
            onPointerDownCapture={(e) => e.stopPropagation()}>Set</button>
        </div>
      </label>

      {/* Model */}
      <label className="block space-y-1">
        <span className="text-xs text-slate-400">Model</span>
        <select value={state.model ?? ''} onChange={(e) => sendAction('set_model', { model: e.target.value || null })}
          className="nodrag nopan w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-xs"
          onPointerDown={(e) => e.stopPropagation()}>
          <option value="">(default model)</option>
          {models.map((m) => <option key={m.id} value={m.id}>{m.label ?? m.id}{m.downloaded ? '' : ' ⤓'}</option>)}
        </select>
      </label>

      {/* Transport */}
      <div className="flex gap-2">
        <button type="button" onClick={() => sendAction(listening ? 'stop' : 'start')}
          className={`nodrag nopan flex-1 rounded px-2 py-1 text-xs font-medium ${listening ? 'border border-rose-600 text-rose-300 hover:bg-rose-950/40' : 'bg-cyan-600 text-white hover:bg-cyan-500'}`}
          onPointerDownCapture={(e) => e.stopPropagation()}>{listening ? 'Stop' : 'Start'}</button>
        <button type="button" onClick={() => sendAction(muted ? 'unmute' : 'mute')}
          className={`nodrag nopan rounded border px-2 py-1 text-xs ${muted ? 'border-amber-500 text-amber-300' : 'border-slate-700 text-slate-300 hover:border-slate-500'}`}
          onPointerDownCapture={(e) => e.stopPropagation()}>{muted ? 'Unmute' : 'Mute'}</button>
      </div>

      {/* Level meter */}
      <div className="h-2 w-full overflow-hidden rounded bg-slate-800">
        <div className="h-full bg-cyan-400 transition-[width] duration-75" style={{ width: `${Math.round(level * 100)}%` }} />
      </div>

      {/* Transcript */}
      <div className="flex items-center justify-between">
        <span className="text-xs text-slate-400">Transcript</span>
        <button type="button" onClick={() => { setFinals([]); setPartial('') }}
          disabled={finals.length === 0 && !partial}
          className="nodrag nopan rounded border border-slate-700 px-2 py-0.5 text-[11px] text-slate-400 hover:border-slate-500 disabled:cursor-not-allowed disabled:opacity-40"
          onPointerDownCapture={(e) => e.stopPropagation()}>Clear</button>
      </div>
      <div ref={logRef} className="max-h-40 min-h-[3rem] space-y-1 overflow-y-auto rounded border border-slate-800 bg-slate-950/60 p-2 text-[12px] leading-snug">
        {finals.length === 0 && !partial && <div className="text-slate-600">…transcript appears here…</div>}
        {finals.map((t, i) => <div key={i} className="text-slate-200">{t}</div>)}
        {partial && <div className="text-cyan-300/80 italic">{partial}</div>}
      </div>

      <div className="flex items-center justify-between text-[10px] text-slate-500">
        <span>queued: {state.queued ?? 0}</span>
        <span className={muted ? 'text-amber-400' : (state.downloading ? 'text-sky-400' : (listening ? 'text-emerald-400' : 'text-slate-500'))}>{status}</span>
      </div>
      {state.last_error && <div className="text-[10px] text-rose-400">{state.last_error}</div>}

      {/* Save transcript to file */}
      <div className="space-y-1 border-t border-slate-800 pt-2">
        <span className="text-xs text-slate-400">Save transcript to file</span>
        <input type="text" value={filename} onChange={(e) => { setFilename(e.target.value); filenameEdited.current = true }}
          placeholder="auto filename" disabled={recordingTranscript}
          className="nodrag nopan w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-200 disabled:opacity-60"
          onPointerDown={(e) => e.stopPropagation()} />
        <div className="flex items-center gap-2">
          {recordingTranscript ? (
            <button type="button" onClick={() => sendAction('stop_recording')}
              className="nodrag nopan rounded bg-rose-600 px-3 py-1 text-xs font-medium text-white hover:bg-rose-500"
              onPointerDownCapture={(e) => e.stopPropagation()}>Stop saving</button>
          ) : (
            <button type="button" onClick={() => { sendAction('start_recording', { path: filename || undefined }); filenameEdited.current = false }}
              className="nodrag nopan rounded border border-emerald-700 px-3 py-1 text-xs font-medium text-emerald-300 hover:border-emerald-500"
              onPointerDownCapture={(e) => e.stopPropagation()}>Record to file</button>
          )}
          {recordingTranscript && <span className="text-[10px] text-emerald-400">● {state.transcript_lines ?? 0} lines</span>}
        </div>
      </div>
    </div>
  )
}
