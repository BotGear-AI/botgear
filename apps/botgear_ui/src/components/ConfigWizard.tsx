// Config wizard — per-instance settings (M4). Shown on first Start of an
// instance whose type declares configurable settings and that hasn't been
// configured yet, and reopenable any time via the inspector's "Configure"
// button so a user can configure before starting.
//
// Two renderers, in preference order:
//   1. RJSF (SchemaForm) against the type's config_schema (Pydantic-derived
//      JSON Schema — see schema_introspect.py) + optional ui_schema for
//      widget/order hints. This is the general path: any config_class gets
//      a working form for free, no per-type authoring.
//   2. The legacy hand-rolled config_steps renderer (FieldInput), for types
//      that predate config_schema or only ever authored config_steps.
// A type with both renders RJSF only — config_steps is left to die off
// type-by-type rather than migrating everything at once.
//
// Seeds from GET /v1/service-proxy/{id}/config (yml-resolved — see
// service_proxy_config_api.py), NOT proxy.service_config. That prop is the
// service_proxy row's service_config, a stale/unused mirror for subprocess
// types (config actually lives in the active config-set's yml). RJSF always
// submits a value for EVERY schema property whether the user touched it or
// not, so seeding from the stale mirror would silently revert any yml-only
// field (e.g. oakd's filters/mono_stream_camera) back to whatever the
// mirror last happened to hold — confirmed live: this is exactly what made
// oakd's depth_align_to_rgb look like it saved but never reached the
// subprocess.
import { useEffect, useMemo, useRef, useState } from 'react'
import type { ServiceMeta } from '../models/ServiceMeta'
import type { ServiceProxy } from '../models/ServiceProxy'
import { useApiFetch } from '../contexts/ActiveRuntimeContext'
import { FieldInput, defaultsFor, type WizardStep } from './InstallWizard'
import { SchemaForm, schemaFormValidator, type SchemaFormRef } from './SchemaForm'
import type { RJSFSchema, UiSchema } from '@rjsf/utils'

interface Props {
  meta: ServiceMeta
  proxy: ServiceProxy
  busy?: boolean
  onCancel: () => void
  onSave: (config: Record<string, unknown>) => void
  saveLabel?: string
}

/** True when a type's config_schema is a non-empty JSON Schema object —
 * i.e. there's actually something to render. Shared with Composer.tsx's
 * "does this type need/have config" gates so schema-only types (like
 * oakd, which has no config_steps at all) get treated as configurable. */
export function hasConfigSchema(schema: unknown): schema is RJSFSchema {
  return (
    !!schema &&
    typeof schema === 'object' &&
    Object.keys((schema as RJSFSchema).properties ?? {}).length > 0
  )
}

export default function ConfigWizard({ meta, proxy, busy, onCancel, onSave, saveLabel = 'Save & start' }: Props) {
  const schema = hasConfigSchema(meta.config_schema) ? (meta.config_schema as RJSFSchema) : null
  const uiSchema = (meta.ui_schema as UiSchema | undefined) ?? undefined
  const proxyId = proxy.id ?? proxy.name ?? ''

  const steps = useMemo<WizardStep[]>(
    () => (Array.isArray(meta.config_steps) ? (meta.config_steps as WizardStep[]) : []),
    [meta.config_steps],
  )

  const apiFetch = useApiFetch()
  const [freshConfig, setFreshConfig] = useState<Record<string, unknown> | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    setLoadError(null)
    apiFetch<{ config: Record<string, unknown> }>(`/v1/service-proxy/${encodeURIComponent(proxyId)}/config`)
      .then((r) => { if (!cancelled) setFreshConfig(r.config ?? {}) })
      .catch((e) => { if (!cancelled) setLoadError(e instanceof Error ? e.message : String(e)) })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [proxyId])
  const loading = freshConfig === null && !loadError

  const [values, setValues] = useState<Record<string, unknown>>(() => defaultsFor(steps))
  const setField = (id: string, v: unknown) => setValues((prev) => ({ ...prev, [id]: v }))
  // Hydrate the legacy renderer's local state once the fresh config lands
  // (see module docstring — proxy.service_config alone isn't trustworthy).
  useEffect(() => {
    if (freshConfig !== null) setValues({ ...defaultsFor(steps), ...freshConfig })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [freshConfig])

  const formRef = useRef<SchemaFormRef>(null)
  // RJSF's own default for a failed submit is a bare `console.error('Form
  // validation failed', errors)` — no visible UI feedback at all, which
  // reads as the Save button silently doing nothing (confirmed live: this
  // is what looked like a "freeze" when slam-1's yml had three fields
  // stored as strings against a number schema). Surface it plainly,
  // outside the scrollable body so it can't be missed by not scrolling.
  const [validationErrors, setValidationErrors] = useState<string[]>([])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="flex max-h-[85vh] w-full max-w-lg flex-col rounded-lg border border-slate-700 bg-slate-900 shadow-xl">
        <header className="border-b border-slate-800 px-5 py-3">
          <h2 className="text-base font-semibold text-slate-100">
            Configure {proxy.name ?? proxy.id}
            <span className="ml-2 text-xs font-normal text-slate-500">{meta.name}</span>
          </h2>
        </header>

        <div className="flex-1 space-y-4 overflow-auto px-5 py-4 text-sm text-slate-300">
          {loadError ? (
            <p className="text-xs text-rose-400">Couldn't load current config: {loadError}</p>
          ) : loading ? (
            <p className="text-xs text-slate-500">Loading current configuration…</p>
          ) : schema ? (
            <SchemaForm
              ref={formRef}
              schema={schema}
              uiSchema={uiSchema}
              formData={freshConfig ?? {}}
              validator={schemaFormValidator}
              onSubmit={({ formData }) => {
                setValidationErrors([])
                onSave((formData as Record<string, unknown>) ?? {})
              }}
              onError={(errors) => setValidationErrors(errors.map((e) => e.stack ?? e.message ?? 'invalid value'))}
            >
              {null}
            </SchemaForm>
          ) : (
            <>
              {steps.length === 0 && (
                <p className="text-xs text-slate-500">This service type has no configurable settings.</p>
              )}
              {steps.map((step) => (
                <fieldset key={step.id} className="space-y-2">
                  {step.title && <legend className="text-xs font-semibold text-slate-200">{step.title}</legend>}
                  {step.description && <p className="text-xs text-slate-500">{step.description}</p>}
                  {(step.fields ?? []).map((f) => (
                    <FieldInput key={f.id} field={f} value={values[f.id]} onChange={(v) => setField(f.id, v)} />
                  ))}
                </fieldset>
              ))}
            </>
          )}
        </div>

        {validationErrors.length > 0 && (
          <div className="mx-5 mb-3 rounded border border-rose-500/40 bg-rose-500/10 p-2 text-xs text-rose-200">
            <p className="mb-1 font-semibold">Couldn't save — fix these first:</p>
            <ul className="list-inside list-disc space-y-0.5">
              {validationErrors.map((msg, i) => (
                // eslint-disable-next-line react/no-array-index-key
                <li key={i}>{msg}</li>
              ))}
            </ul>
          </div>
        )}

        <footer className="flex items-center justify-between border-t border-slate-800 px-5 py-3">
          <button
            type="button"
            onClick={onCancel}
            className="rounded px-3 py-1.5 text-sm text-slate-400 hover:text-slate-200"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={busy || loading || !!loadError}
            onClick={() => (schema ? formRef.current?.submit() : onSave(values))}
            className="rounded bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-40"
          >
            {saveLabel}
          </button>
        </footer>
      </div>
    </div>
  )
}
