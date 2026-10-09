// Slack — view_full panel for the slack service.
//
// Same transcript + composer shape as chat's view, plus a connection
// strip (this service actually has a platform to be connected to) and
// TWO separate send affordances, since "operator talking as if they
// were a Slack user" and "bot posting a message" are genuinely
// different actions here (unlike chat, where inbox IS the only input):
//   - "Simulate inbound" publishes straight to /inbox, exactly like
//     chat's composer — useful for testing a workflow without a real
//     Slack message.
//   - "Post as bot" calls the speak control action, which actually
//     hits the Slack API via this proxy's bot connection.
//
// No live channel-picker dropdown in v1 — list_channels() is a
// request/response action, not a retained topic, so wiring a dropdown
// needs reply_to plumbing this view doesn't have yet. default_channel
// (set at proxy config time) plus an optional per-message channel
// override text field covers the common case.
import {
  useCallback, useEffect, useLayoutEffect, useRef, useState,
  type FormEvent, type KeyboardEvent,
} from 'react'

import type { ServiceProxy } from '@rlx/ui'
import { type InboundFrame } from '@rlx/ui'
import { useWsClient } from '@rlx/ui'


interface InboxPayload { text: string; sender?: string | null; channel?: string | null; ts?: number }
interface SpokenPayload { text: string; channel?: string | null; ts: number }
interface StatePayload {
  connected: boolean
  listening: boolean
  queued: number
  last_inbox?: string
  last_spoken?: string
  default_channel?: string | null
  capabilities?: { listen: boolean; speak: boolean; channels: boolean }
}

interface Turn {
  from: 'inbound' | 'bot'
  text: string
  sender?: string | null
  channel?: string | null
  ts: number
  key: string
}

const MAX_TURNS = 200

export default function SlackFullView({ proxy }: { proxy: ServiceProxy }) {
  const wsClient = useWsClient()
  const proxyId = proxy.id ?? proxy.name ?? ''
  const inboxTopic = `/slack/${proxyId}/inbox`
  const spokenTopic = `/slack/${proxyId}/spoken`
  const stateTopic = `/slack/${proxyId}/state`
  const controlTopic = `/slack/${proxyId}/control`

  const [transcript, setTranscript] = useState<Turn[]>([])
  const [state, setState] = useState<StatePayload | null>(null)
  const [draft, setDraft] = useState<string>('')
  const [channelOverride, setChannelOverride] = useState<string>('')
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const keyCounterRef = useRef(0)

  const pushTurn = useCallback((from: 'inbound' | 'bot', text: string, ts: number, sender?: string | null, channel?: string | null) => {
    const trimmed = text.trim()
    if (!trimmed) return
    keyCounterRef.current += 1
    const key = `${ts}-${keyCounterRef.current}`
    setTranscript((prev) => {
      const next = [...prev, { from, text: trimmed, ts, sender, channel, key }]
      return next.length > MAX_TURNS ? next.slice(next.length - MAX_TURNS) : next
    })
  }, [])

  useEffect(() => {
    if (!proxyId) return
    const offInbox = wsClient.subscribe(inboxTopic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      const p = (f.payload ?? {}) as InboxPayload
      if (typeof p.text !== 'string') return
      pushTurn('inbound', p.text, p.ts ?? Date.now() / 1000, p.sender, p.channel)
    })
    const offSpoken = wsClient.subscribe(spokenTopic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      const p = (f.payload ?? {}) as SpokenPayload
      if (typeof p.text !== 'string') return
      pushTurn('bot', p.text, p.ts ?? Date.now() / 1000, null, p.channel)
    })
    const offState = wsClient.subscribe(stateTopic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      setState(f.payload as StatePayload)
    })
    return () => { offInbox(); offSpoken(); offState() }
  }, [proxyId, inboxTopic, spokenTopic, stateTopic, wsClient, pushTurn])

  useLayoutEffect(() => {
    const el = scrollRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
    const id = requestAnimationFrame(() => { el.scrollTop = el.scrollHeight })
    return () => cancelAnimationFrame(id)
  }, [transcript.length])

  const serviceRunning = proxy.status === 'running' || proxy.status === 'starting'
  const effectiveChannel = channelOverride.trim() || state?.default_channel || undefined

  const simulateInbound = useCallback(
    (e?: FormEvent) => {
      e?.preventDefault()
      const text = draft.trim()
      if (!text || !serviceRunning) return
      wsClient.publish(inboxTopic, { text, channel: effectiveChannel ?? null, ts: Date.now() / 1000 })
      setDraft('')
    },
    [draft, inboxTopic, serviceRunning, wsClient, effectiveChannel],
  )

  const postAsBot = useCallback(() => {
    const text = draft.trim()
    if (!text || !serviceRunning) return
    wsClient.publish(controlTopic, { action: 'speak', text, channel: effectiveChannel })
    setDraft('')
  }, [draft, controlTopic, serviceRunning, wsClient, effectiveChannel])

  const onKeyDown = useCallback((e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      postAsBot()
    }
  }, [postAsBot])

  const fmtTime = (ts: number): string => {
    const d = new Date(ts * 1000)
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  }

  return (
    <div
      className="flex h-full min-h-[300px] min-w-[320px] flex-col gap-2 p-3 text-xs"
      onPointerDown={(e) => e.stopPropagation()}
    >
      <div className="flex items-baseline justify-between text-[10px] text-slate-500">
        <span>slack-{proxyId}</span>
        <span className="flex items-baseline gap-2">
          {state ? (
            <>
              <span className={state.connected ? 'text-emerald-400' : 'text-rose-400'} title="Gateway connection status">
                {state.connected ? '● connected' : '○ disconnected'}
              </span>
              <span className={state.listening ? 'text-emerald-400' : 'text-slate-500'}>
                {state.listening ? 'listening' : 'idle'}
              </span>
              {state.queued > 0 && (
                <span className="text-amber-400" title="Messages queued for the next listen() call">
                  queued {state.queued}
                </span>
              )}
            </>
          ) : (
            <span>{serviceRunning ? 'loading…' : 'service not running'}</span>
          )}
        </span>
      </div>

      <div className="relative min-h-0 flex-1 rounded border border-slate-800 bg-slate-950/60">
        <div
          ref={scrollRef}
          className="nodrag nopan absolute inset-0 flex flex-col gap-1 overflow-y-auto p-2"
          onWheel={(e) => e.stopPropagation()}
        >
          {transcript.length === 0 ? (
            <div className="text-[11px] text-slate-600">
              {serviceRunning
                ? 'no messages yet — post below, or wait for a Slack message to arrive'
                : 'start the slack service to connect'}
            </div>
          ) : (
            transcript.map((t) => {
              const isInbound = t.from === 'inbound'
              return (
                <div key={t.key} className={`flex flex-col ${isInbound ? 'items-end' : 'items-start'}`}>
                  <span className="text-[9px] text-slate-600">
                    {isInbound ? (t.sender ?? 'inbound') : 'bot'}
                    {t.channel ? ` · #${t.channel}` : ''} · {fmtTime(t.ts)}
                  </span>
                  <div
                    className={`max-w-[85%] whitespace-pre-wrap break-words rounded px-2 py-1 text-[11px] ${
                      isInbound ? 'bg-sky-950/60 text-sky-100' : 'bg-purple-950/60 text-purple-100'
                    }`}
                  >
                    {t.text}
                  </div>
                </div>
              )
            })
          )}
        </div>
      </div>

      <div className="flex items-center gap-2 text-[10px] text-slate-500">
        <span>channel</span>
        <input
          value={channelOverride}
          onChange={(e) => setChannelOverride(e.target.value)}
          onClick={(e) => e.stopPropagation()}
          placeholder={state?.default_channel ?? 'default_channel not set'}
          className="nodrag nopan w-40 rounded border border-slate-700 bg-slate-950 px-2 py-0.5 font-mono text-[10px] text-slate-200 placeholder:text-slate-600 focus:border-slate-500 focus:outline-none"
        />
      </div>

      <form onSubmit={simulateInbound} className="flex items-end gap-2">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          onClick={(e) => e.stopPropagation()}
          disabled={!serviceRunning}
          rows={2}
          placeholder={serviceRunning ? 'type here — Enter posts as bot, buttons below for either' : 'service not running'}
          className="nodrag nopan min-h-[44px] flex-1 resize-none rounded border border-slate-700 bg-slate-950 px-2 py-1 font-sans text-[12px] text-slate-100 placeholder:text-slate-600 focus:border-slate-500 focus:outline-none disabled:opacity-50"
        />
        <div className="flex flex-col gap-1">
          <button
            type="submit"
            onClick={(e) => e.stopPropagation()}
            onPointerDown={(e) => e.stopPropagation()}
            disabled={!serviceRunning || draft.trim().length === 0}
            title="Publish to /inbox — simulates a Slack message arriving, doesn't touch the real platform"
            className="nodrag nopan rounded bg-sky-700 px-3 py-1 text-[10px] font-medium text-white hover:bg-sky-600 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Simulate inbound
          </button>
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); postAsBot() }}
            onPointerDown={(e) => e.stopPropagation()}
            disabled={!serviceRunning || draft.trim().length === 0}
            title="Calls speak() — actually posts to Slack via this bot"
            className="nodrag nopan rounded bg-purple-600 px-3 py-1 text-[10px] font-medium text-white hover:bg-purple-500 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Post as bot
          </button>
        </div>
      </form>
    </div>
  )
}
