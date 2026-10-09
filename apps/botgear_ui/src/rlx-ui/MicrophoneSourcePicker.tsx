// MicrophoneSourcePicker — the "attach a mic" gesture, shared across every
// audio-consumer service view (stt_local, stt_whisper, speaker_local,
// speaker_browser, and any future one) instead of each re-implementing its
// own free-text "type the topic by hand" field.
//
// Lists the runtime's currently known microphone-kind proxies (any
// service_meta_id whose catalog entry is tagged "microphone" — mic_local,
// mic_browser, …) as a dropdown of ``/microphone/<id>/audio`` topics. Falls
// back to a raw text field ("Custom topic…") for anything the option list
// doesn't cover: a value set before the picker existed, a hand-typed topic,
// or a federated peer's microphone reached via a bridged topic string.
import { useEffect, useState } from 'react'
import { useApiFetch } from '../contexts/ActiveRuntimeContext'
import type { ServiceProxy } from '../models/ServiceProxy'
import type { ServiceMeta } from '../models/ServiceMeta'

export interface MicrophoneOption {
  proxyId: string
  label: string
  topic: string
  status?: string
}

/** Live list of microphone-capable proxies on the active runtime, as
 * {proxyId, label, topic, status} options. Polls every 5s — cheap REST
 * reads already used elsewhere in the canvas, no bus subscription needed
 * just to populate a picker. */
export function useMicrophoneOptions(): { options: MicrophoneOption[]; loading: boolean } {
  const apiFetch = useApiFetch()
  const [options, setOptions] = useState<MicrophoneOption[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const [proxies, catalog] = await Promise.all([
          apiFetch<ServiceProxy[]>('/v1/service-proxy-list'),
          apiFetch<ServiceMeta[]>('/v1/service-meta-list'),
        ])
        if (cancelled) return
        const micTypes = new Set(
          (catalog ?? [])
            .filter((m) => Array.isArray(m.tags) && (m.tags as string[]).includes('microphone'))
            .map((m) => `${m.name}@${m.version}`),
        )
        const opts: MicrophoneOption[] = (proxies ?? [])
          .filter((p) => !!p.id && micTypes.has(p.service_meta_id))
          .map((p) => ({
            proxyId: p.id as string,
            label: p.name || (p.id as string),
            topic: `/microphone/${p.id}/audio`,
            status: p.status,
          }))
        setOptions(opts)
      } catch {
        // Soft-fail — the picker degrades to its "Custom topic" field.
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    const t = setInterval(load, 5000)
    return () => { cancelled = true; clearInterval(t) }
  }, [apiFetch])

  return { options, loading }
}

export function MicrophoneSourcePicker({
  value,
  onChange,
  className,
}: {
  /** Current source topic (SttConfig/SpeakerConfig's ``input_ref``), or
   * null/empty when nothing is selected yet. */
  value: string | null | undefined
  onChange: (topic: string) => void
  className?: string
}) {
  const { options } = useMicrophoneOptions()
  const known = value ? options.some((o) => o.topic === value) : true
  // Only the picker's OWN toggle can leave "custom" mode — a value that
  // stops matching an option (e.g. its proxy got released) shouldn't snap
  // the field shut and hide what's actually configured.
  const [customOpen, setCustomOpen] = useState(!known)
  useEffect(() => {
    if (!known) setCustomOpen(true)
  }, [known])

  if (!customOpen) {
    return (
      <select
        value={value ?? ''}
        onChange={(e) => {
          if (e.target.value === '__custom__') { setCustomOpen(true); return }
          onChange(e.target.value)
        }}
        className={className ?? 'nodrag nopan w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-xs text-slate-200'}
        onPointerDown={(e) => e.stopPropagation()}
      >
        <option value="">(no microphone selected)</option>
        {options.map((o) => (
          <option key={o.proxyId} value={o.topic}>
            {o.label}{o.status && o.status !== 'running' ? ` (${o.status})` : ''}
          </option>
        ))}
        <option value="__custom__">Custom topic…</option>
      </select>
    )
  }

  return (
    <div className="flex min-w-0 flex-1 gap-2">
      <input
        type="text"
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value)}
        placeholder="/microphone/mic_local-1/audio"
        className="nodrag nopan min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-200"
        onPointerDown={(e) => e.stopPropagation()}
      />
      {options.length > 0 && (
        <button
          type="button"
          onClick={() => setCustomOpen(false)}
          className="nodrag nopan rounded border border-slate-700 px-2 py-1 text-[11px] text-slate-400 hover:border-slate-500"
          onPointerDownCapture={(e) => e.stopPropagation()}
        >
          Pick…
        </button>
      )}
    </div>
  )
}
