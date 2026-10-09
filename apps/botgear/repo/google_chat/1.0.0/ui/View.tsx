// Google Chat — view_full panel for the google_chat service.
//
// Send-only in v1 (see this service's package.yml description for why)
// — there's no inbound to render, so unlike chat/discord/slack's view
// this is a one-way outbound log + composer, not a two-column
// transcript. state.capabilities.listen is always false here; shown
// plainly rather than hidden, so an operator looking at this panel
// understands why nothing arrives even if they post in the space.
import {
  useCallback, useEffect, useLayoutEffect, useRef, useState, type FormEvent,
} from 'react'

import type { ServiceProxy } from '@rlx/ui'
import { type InboundFrame } from '@rlx/ui'
import { useWsClient } from '@rlx/ui'


interface SpokenPayload { text: string; channel?: string | null; ts: number }
interface StatePayload {
  connected: boolean
  last_spoken?: string
  capabilities?: { listen: boolean; speak: boolean; channels: boolean }
}

interface Turn { text: string; ts: number; key: string }

const MAX_TURNS = 200

export default function GoogleChatFullView({ proxy }: { proxy: ServiceProxy }) {
  const wsClient = useWsClient()
  const proxyId = proxy.id ?? proxy.name ?? ''
  const spokenTopic = `/google_chat/${proxyId}/spoken`
  const stateTopic = `/google_chat/${proxyId}/state`
  const controlTopic = `/google_chat/${proxyId}/control`

  const [log, setLog] = useState<Turn[]>([])
  const [state, setState] = useState<StatePayload | null>(null)
  const [draft, setDraft] = useState<string>('')
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const keyCounterRef = useRef(0)

  useEffect(() => {
    if (!proxyId) return
    const offSpoken = wsClient.subscribe(spokenTopic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      const p = (f.payload ?? {}) as SpokenPayload
      if (typeof p.text !== 'string') return
      keyCounterRef.current += 1
      const key = `${p.ts ?? Date.now() / 1000}-${keyCounterRef.current}`
      setLog((prev) => {
        const next = [...prev, { text: p.text, ts: p.ts ?? Date.now() / 1000, key }]
        return next.length > MAX_TURNS ? next.slice(next.length - MAX_TURNS) : next
      })
    })
    const offState = wsClient.subscribe(stateTopic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      setState(f.payload as StatePayload)
    })
    return () => { offSpoken(); offState() }
  }, [proxyId, spokenTopic, stateTopic, wsClient])

  useLayoutEffect(() => {
    const el = scrollRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [log.length])

  const serviceRunning = proxy.status === 'running' || proxy.status === 'starting'

  const post = useCallback(
    (e?: FormEvent) => {
      e?.preventDefault()
      const text = draft.trim()
      if (!text || !serviceRunning) return
      wsClient.publish(controlTopic, { action: 'speak', text })
      setDraft('')
    },
    [draft, controlTopic, serviceRunning, wsClient],
  )

  return (
    <div
      className="flex h-full min-h-[260px] min-w-[300px] flex-col gap-2 p-3 text-xs"
      onPointerDown={(e) => e.stopPropagation()}
    >
      <div className="flex items-baseline justify-between text-[10px] text-slate-500">
        <span>google_chat-{proxyId}</span>
        <span className="flex items-baseline gap-2">
          {state ? (
            <span className={state.connected ? 'text-emerald-400' : 'text-rose-400'}>
              {state.connected ? '● webhook configured' : '○ not configured'}
            </span>
          ) : (
            <span>{serviceRunning ? 'loading…' : 'service not running'}</span>
          )}
        </span>
      </div>

      <div className="rounded border border-amber-800 bg-amber-950/30 px-2 py-1 text-[10px] text-amber-300">
        send-only — this integration can't receive messages from the space (webhook limitation, not a bug)
      </div>

      <div className="relative min-h-0 flex-1 rounded border border-slate-800 bg-slate-950/60">
        <div
          ref={scrollRef}
          className="nodrag nopan absolute inset-0 flex flex-col gap-1 overflow-y-auto p-2"
          onWheel={(e) => e.stopPropagation()}
        >
          {log.length === 0 ? (
            <div className="text-[11px] text-slate-600">
              {serviceRunning ? 'nothing posted yet — type below' : 'start the google_chat service to connect'}
            </div>
          ) : (
            log.map((t) => (
              <div key={t.key} className="max-w-[90%] whitespace-pre-wrap break-words rounded bg-teal-950/60 px-2 py-1 text-[11px] text-teal-100">
                {t.text}
              </div>
            ))
          )}
        </div>
      </div>

      <form onSubmit={post} className="flex items-end gap-2">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onClick={(e) => e.stopPropagation()}
          disabled={!serviceRunning}
          rows={2}
          placeholder={serviceRunning ? 'type here, Enter to post to the space' : 'service not running'}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); post() } }}
          className="nodrag nopan min-h-[44px] flex-1 resize-none rounded border border-slate-700 bg-slate-950 px-2 py-1 font-sans text-[12px] text-slate-100 placeholder:text-slate-600 focus:border-slate-500 focus:outline-none disabled:opacity-50"
        />
        <button
          type="submit"
          onClick={(e) => e.stopPropagation()}
          onPointerDown={(e) => e.stopPropagation()}
          disabled={!serviceRunning || draft.trim().length === 0}
          className="nodrag nopan rounded bg-teal-600 px-3 py-1.5 text-[11px] font-medium text-white hover:bg-teal-500 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Post
        </button>
      </form>
    </div>
  )
}
