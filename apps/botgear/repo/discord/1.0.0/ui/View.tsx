// Discord — view_full panel for the discord service.
//
// Same transcript + composer shape as chat's view, plus a connection
// strip (this service actually has a platform to be connected to) and
// TWO separate send affordances, since "operator talking as if they
// were a Discord user" and "bot posting a message" are genuinely
// different actions here (unlike chat, where inbox IS the only input):
//   - "Simulate inbound" publishes straight to /inbox, exactly like
//     chat's composer — useful for testing a workflow without a real
//     Discord message.
//   - "Post as bot" calls the speak control action, which actually
//     hits the Discord API via this proxy's bot connection.
//
// "Post as bot" is a request/response call (publish with reply_to),
// not fire-and-forget — the control response's `sent` flag drives an
// inline error banner when delivery didn't actually happen (no
// channel configured, platform rejected it, etc). /spoken also now
// carries `sent`, so historical transcript entries show a per-message
// "not delivered" mark, not just the most recent attempt.
//
// Replying to whoever last DMed/posted needs the channel that message
// arrived on — there's no channel-picker UI for it (list_channels() is
// request/response, not a retained topic — same caveat as below), so
// each inbound bubble gets a "Reply" affordance that copies its
// channel into the composer's channel field instead of requiring the
// operator to already know/paste a raw Discord channel id.
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
interface SpokenPayload { text: string; channel?: string | null; ts: number; sent?: boolean }
interface StatePayload {
  connected: boolean
  listening: boolean
  queued: number
  last_inbox?: string
  last_spoken?: string
  default_channel?: string | null
  capabilities?: { listen: boolean; speak: boolean; channels: boolean }
}
interface SpeakResponse { spoken: string; ts: number; channel: string | null; sent: boolean }

interface Turn {
  from: 'inbound' | 'bot'
  text: string
  sender?: string | null
  channel?: string | null
  sent?: boolean
  ts: number
  key: string
}

const MAX_TURNS = 200
const REPLY_TIMEOUT_MS = 8000

/** One-shot request/response over a control topic: subscribes a private
 * reply topic, waits for the server to ack that subscription (closes the
 * race where a fast responder's reply arrives before the bus pump is
 * registered — see wsClient.awaitSubscribed's docstring), then publishes
 * with reply_to set. Rejects on timeout so callers can surface a real
 * error instead of hanging forever. */
async function requestOnce<T>(
  wsClient: ReturnType<typeof useWsClient>,
  topic: string,
  payload: Record<string, unknown>,
  timeoutMs = REPLY_TIMEOUT_MS,
): Promise<T> {
  const replyTopic = `/reply/discord-ui/${Math.random().toString(36).slice(2)}`
  return new Promise<T>((resolve, reject) => {
    let settled = false
    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      off()
      reject(new Error('timed out waiting for a response'))
    }, timeoutMs)
    const off = wsClient.subscribe(replyTopic, (f: InboundFrame) => {
      if (f.method !== 'message' || settled) return
      settled = true
      clearTimeout(timer)
      off()
      resolve(f.payload as T)
    })
    wsClient.awaitSubscribed(replyTopic)
      .then(() => wsClient.publish(topic, { ...payload, reply_to: replyTopic }))
      .catch((err: Error) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        off()
        reject(err)
      })
  })
}

export default function DiscordFullView({ proxy }: { proxy: ServiceProxy }) {
  const wsClient = useWsClient()
  const proxyId = proxy.id ?? proxy.name ?? ''
  const inboxTopic = `/discord/${proxyId}/inbox`
  const spokenTopic = `/discord/${proxyId}/spoken`
  const stateTopic = `/discord/${proxyId}/state`
  const controlTopic = `/discord/${proxyId}/control`

  const [transcript, setTranscript] = useState<Turn[]>([])
  const [state, setState] = useState<StatePayload | null>(null)
  const [draft, setDraft] = useState<string>('')
  const [channelOverride, setChannelOverride] = useState<string>('')
  const [replyingToSender, setReplyingToSender] = useState<string | null>(null)
  const [sendError, setSendError] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const keyCounterRef = useRef(0)
  const draftRef = useRef<HTMLTextAreaElement | null>(null)

  const pushTurn = useCallback(
    (from: 'inbound' | 'bot', text: string, ts: number, sender?: string | null, channel?: string | null, sent?: boolean) => {
      const trimmed = text.trim()
      if (!trimmed) return
      keyCounterRef.current += 1
      const key = `${ts}-${keyCounterRef.current}`
      setTranscript((prev) => {
        const next = [...prev, { from, text: trimmed, ts, sender, channel, sent, key }]
        return next.length > MAX_TURNS ? next.slice(next.length - MAX_TURNS) : next
      })
    },
    [],
  )

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
      pushTurn('bot', p.text, p.ts ?? Date.now() / 1000, null, p.channel, p.sent)
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
  const canDeliver = Boolean(effectiveChannel)

  const startReply = useCallback((turn: Turn) => {
    if (!turn.channel) return
    setChannelOverride(turn.channel)
    setReplyingToSender(turn.sender ?? null)
    setSendError(null)
    draftRef.current?.focus()
  }, [])

  const clearChannel = useCallback(() => {
    setChannelOverride('')
    setReplyingToSender(null)
  }, [])

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
    if (!text || !serviceRunning || !canDeliver || sending) return
    setDraft('')
    setSendError(null)
    setSending(true)
    requestOnce<SpeakResponse>(wsClient, controlTopic, { action: 'speak', text, channel: effectiveChannel })
      .then((res) => {
        if (!res.sent) {
          setSendError(`Discord rejected the message (channel ${res.channel ?? effectiveChannel} — check the bot has access there).`)
        }
      })
      .catch((err: Error) => {
        setSendError(`Send failed: ${err.message}`)
      })
      .finally(() => setSending(false))
  }, [draft, controlTopic, serviceRunning, canDeliver, sending, wsClient, effectiveChannel])

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
        <span>discord-{proxyId}</span>
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
                ? 'no messages yet — post below, or wait for a Discord message to arrive'
                : 'start the discord service to connect'}
            </div>
          ) : (
            transcript.map((t) => {
              const isInbound = t.from === 'inbound'
              const failed = t.from === 'bot' && t.sent === false
              return (
                <div key={t.key} className={`group flex flex-col ${isInbound ? 'items-end' : 'items-start'}`}>
                  <span className="flex items-center gap-1 text-[9px] text-slate-600">
                    {isInbound ? (t.sender ?? 'inbound') : 'bot'}
                    {t.channel ? ` · #${t.channel}` : ''} · {fmtTime(t.ts)}
                    {isInbound && t.channel && (
                      <button
                        type="button"
                        onClick={(e) => { e.stopPropagation(); startReply(t) }}
                        onPointerDown={(e) => e.stopPropagation()}
                        title={`Reply to ${t.sender ?? 'this sender'} — sets the channel field to this message's channel`}
                        className="nodrag nopan ml-1 rounded px-1 text-sky-500 opacity-0 hover:bg-sky-950 group-hover:opacity-100"
                      >
                        ↩ reply
                      </button>
                    )}
                  </span>
                  <div
                    className={`max-w-[85%] whitespace-pre-wrap break-words rounded px-2 py-1 text-[11px] ${
                      isInbound
                        ? 'bg-sky-950/60 text-sky-100'
                        : failed
                          ? 'border border-rose-800 bg-rose-950/40 text-rose-100'
                          : 'bg-indigo-950/60 text-indigo-100'
                    }`}
                  >
                    {t.text}
                  </div>
                  {failed && (
                    <span className="text-[9px] text-rose-400" title="speak() returned sent: false — this never reached Discord">
                      ✕ not delivered
                    </span>
                  )}
                </div>
              )
            })
          )}
        </div>
      </div>

      {sendError && (
        <div className="flex items-center justify-between rounded border border-rose-800 bg-rose-950/50 px-2 py-1 text-[10px] text-rose-200">
          <span>{sendError}</span>
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); setSendError(null) }}
            onPointerDown={(e) => e.stopPropagation()}
            className="nodrag nopan ml-2 text-rose-400 hover:text-rose-200"
          >
            ✕
          </button>
        </div>
      )}

      <div className="flex items-center gap-2 text-[10px] text-slate-500">
        <span>channel</span>
        <input
          value={channelOverride}
          onChange={(e) => { setChannelOverride(e.target.value); setReplyingToSender(null) }}
          onClick={(e) => e.stopPropagation()}
          placeholder={state?.default_channel ?? 'default_channel not set'}
          className="nodrag nopan w-40 rounded border border-slate-700 bg-slate-950 px-2 py-0.5 font-mono text-[10px] text-slate-200 placeholder:text-slate-600 focus:border-slate-500 focus:outline-none"
        />
        {channelOverride && (
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); clearChannel() }}
            onPointerDown={(e) => e.stopPropagation()}
            className="nodrag nopan text-slate-500 hover:text-slate-300"
            title="Clear the channel override"
          >
            ✕
          </button>
        )}
        {canDeliver ? (
          <span className="text-emerald-500">
            → {replyingToSender ? `replying to ${replyingToSender}` : `#${effectiveChannel}`}
          </span>
        ) : (
          <span className="text-amber-500" title="Post as bot is disabled until a channel is set — otherwise the message silently goes nowhere">
            ⚠ no channel set — Post as bot disabled
          </span>
        )}
      </div>

      <form onSubmit={simulateInbound} className="flex items-end gap-2">
        <textarea
          ref={draftRef}
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
            title="Publish to /inbox — simulates a Discord message arriving, doesn't touch the real platform"
            className="nodrag nopan rounded bg-sky-700 px-3 py-1 text-[10px] font-medium text-white hover:bg-sky-600 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Simulate inbound
          </button>
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); postAsBot() }}
            onPointerDown={(e) => e.stopPropagation()}
            disabled={!serviceRunning || draft.trim().length === 0 || !canDeliver || sending}
            title={canDeliver ? 'Calls speak() — actually posts to Discord via this bot' : 'Set a channel (or click Reply on an inbound message) first'}
            className="nodrag nopan rounded bg-indigo-600 px-3 py-1 text-[10px] font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {sending ? 'Sending…' : 'Post as bot'}
          </button>
        </div>
      </form>
    </div>
  )
}
