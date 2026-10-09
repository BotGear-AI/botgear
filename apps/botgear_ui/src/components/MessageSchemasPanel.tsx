// MessageSchemasPanel — every message a service type sends or receives,
// organized by direction, rendered from its type descriptor.
//
// Source of truth is the backend, not this file: each service declares
// Pydantic models (``state_schema`` / ``topic_schemas`` /
// ``subscribes_schemas`` / ``config_class`` / @service_method signatures)
// and the runtime publishes the JSON Schemas retained on
// ``/runtime/runtime/types/<type>``. The live instance's
// ``/<type>/<id>/meta`` supplies the concrete topic root + pid.
//
// Rendered at the foot of every view_full (see composerViews/view_full.tsx)
// so any service's messages are one click away, running or not.
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { ArrowDownLeft, ArrowUpRight, ChevronDown, ChevronRight, Settings2, Terminal } from 'lucide-react'

import { useWsClient } from '../contexts/ActiveRuntimeContext'
import type { InboundFrame } from '../runtime/wsClient'
import { CopyButton } from './CopyButton'


type JsonSchema = Record<string, unknown>

interface TypeMethod {
  name: string
  doc?: string | null
  args_schema?: JsonSchema
  publishes?: string[]
  publish_return?: string | null
}

interface TypeDescriptor {
  type: string
  version?: string
  transport?: string
  schemas_complete?: boolean
  notes?: string
  publishes?: string[]
  config_schema?: JsonSchema | null
  state_schema?: JsonSchema | null
  topic_schemas?: Record<string, JsonSchema>
  subscribes_schemas?: Record<string, JsonSchema>
  methods?: TypeMethod[]
}

interface ServiceMeta {
  runtime_id?: string
  pid?: number
  topics_root?: string
  // Names/docs only (no arg schemas) — but every runtime version
  // publishes these, so they're the fallback when the owning runtime's
  // type descriptor has no methods (older runtime, missing venv).
  methods?: TypeMethod[]
}


function useRetained<T>(topic: string): T | null {
  const wsClient = useWsClient()
  const [value, setValue] = useState<T | null>(null)
  useEffect(() => {
    setValue(null)
    return wsClient.subscribe(topic, (f: InboundFrame) => {
      if (f.method !== 'message') return
      setValue(f.payload && typeof f.payload === 'object' ? (f.payload as T) : null)
    })
  }, [topic, wsClient])
  return value
}


/** Full bus path for a descriptor key: absolute keys pass through,
 *  relative ones (incl. templated ``pin/{label}``) hang off the root. */
function topicPath(root: string, key: string): string {
  return key.startsWith('/') ? key : `${root}/${key}`
}


export function MessageSchemasPanel({ typeName, proxyId }: { typeName: string; proxyId: string }) {
  const [open, setOpen] = useState(false)
  const descriptorTopic = `/runtime/runtime/types/${typeName}`
  const type = useRetained<TypeDescriptor>(descriptorTopic)
  const meta = useRetained<ServiceMeta>(`/${typeName}/${proxyId}/meta`)
  const root = meta?.topics_root ?? `/${typeName}/${proxyId}`

  // Outbound: declared ``publishes`` order first (state leads), then any
  // schema'd topic not in that list — templated per-instance topics
  // (``pin/{label}``) and absolute ones (``/filter/index/{id}``).
  const outbound = useMemo(() => {
    if (!type) return []
    const schemas = type.topic_schemas ?? {}
    const declared = type.publishes ?? []
    const keys = [...declared, ...Object.keys(schemas).filter((k) => !declared.includes(k))]
    return keys.map((key) => ({
      key,
      schema: key === 'state' ? type.state_schema ?? null : schemas[key] ?? null,
      declared: declared.includes(key),
    }))
  }, [type])
  const inbound = Object.entries(type?.subscribes_schemas ?? {})
  const descriptorMethods = type?.methods ?? []
  const methodsFromMeta = descriptorMethods.length === 0 && (meta?.methods?.length ?? 0) > 0
  const methods = methodsFromMeta ? meta!.methods! : descriptorMethods
  // An incomplete descriptor means "unknown", not "none" — say so
  // instead of claiming the service publishes/accepts nothing.
  const incomplete = type?.schemas_complete === false
  const none = (what: string) => (incomplete ? 'unknown — descriptor incomplete' : what)

  return (
    <section className="nodrag nopan nowheel border-t border-slate-800 bg-slate-950/60 text-slate-300">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        onPointerDown={(e) => e.stopPropagation()}
        className="flex w-full items-center gap-2 px-2 py-1 text-left hover:bg-slate-900"
      >
        {open ? <ChevronDown size={12} className="text-slate-500" /> : <ChevronRight size={12} className="text-slate-500" />}
        <span className="text-[10px] uppercase tracking-wider text-slate-400">messages</span>
        {type ? (
          <span className="font-mono text-[9px] text-slate-500">
            {outbound.length} out · {inbound.length} in · {methods.length} actions
          </span>
        ) : (
          <span className="text-[9px] text-slate-600">no type descriptor yet</span>
        )}
      </button>

      {open && type && (
        <div className="max-h-[50vh] space-y-2 overflow-auto border-t border-slate-800 px-2 py-2">
          {type.schemas_complete === false && (
            <div className="font-mono text-[9px] text-amber-400">
              schemas incomplete — {type.notes ?? 'introspection failed for this type'}.
              {' '}The runtime that owns this service may be on older botgear code, or
              this type's venv isn't installed there.
            </div>
          )}

          <Group icon={<ArrowUpRight size={11} />} title="publishes" hint="this service → bus" tone="text-sky-400">
            {outbound.length === 0 && <Empty>{none('publishes nothing')}</Empty>}
            {outbound.map(({ key, schema, declared }) => (
              <MessageRow
                key={key}
                name={key}
                path={topicPath(root, key)}
                schema={schema}
                tag={key.includes('{') ? 'per-instance' : !declared ? 'extra' : undefined}
              />
            ))}
          </Group>

          <Group icon={<ArrowDownLeft size={11} />} title="subscribes" hint="bus → this service" tone="text-emerald-400">
            {inbound.length === 0 && <Empty>{none('no declared inbound topics (besides control)')}</Empty>}
            {inbound.map(([key, schema]) => (
              <MessageRow key={key} name={key} path={topicPath(root, key)} schema={schema} />
            ))}
          </Group>

          <Group
            icon={<Terminal size={11} />}
            title="control actions"
            hint={`publish {action, …args} to ${root}/control`}
            tone="text-violet-400"
          >
            {methodsFromMeta && (
              <Empty>from the live instance's /meta — argument schemas unavailable</Empty>
            )}
            {methods.length === 0 && <Empty>{none('no actions')}</Empty>}
            {methods.map((m) => <ActionRow key={m.name} method={m} />)}
          </Group>

          {type.config_schema && (
            <Group icon={<Settings2 size={11} />} title="config" hint="persisted to the config set" tone="text-amber-400">
              <MessageRow name="config" schema={type.config_schema} />
            </Group>
          )}

          <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 border-t border-slate-800 pt-1.5 font-mono text-[9px] text-slate-500">
            <span>{type.type}@{type.version ?? '?'} · {type.transport ?? '—'}</span>
            {meta?.pid != null && <span>pid {meta.pid}</span>}
            {meta?.runtime_id && <span>runtime {meta.runtime_id}</span>}
            <span className="flex items-center gap-1">
              descriptor {descriptorTopic}
              <CopyButton value={descriptorTopic} size={10} />
            </span>
          </div>
        </div>
      )}
    </section>
  )
}


function Group({ icon, title, hint, tone, children }: {
  icon: ReactNode; title: string; hint: string; tone: string; children: ReactNode
}) {
  return (
    <div>
      <div className="mb-0.5 flex items-baseline gap-1.5">
        <span className={`flex items-center gap-1 text-[9px] uppercase tracking-wider ${tone}`}>{icon}{title}</span>
        <span className="truncate text-[9px] text-slate-600">{hint}</span>
      </div>
      <div className="space-y-0.5">{children}</div>
    </div>
  )
}


function Empty({ children }: { children: ReactNode }) {
  return <div className="pl-4 text-[9px] italic text-slate-600">{children}</div>
}


/** One message: topic name + model title + path, expanding to its
 *  field tree (or raw JSON Schema). */
function MessageRow({ name, path, schema, tag }: {
  name: string; path?: string; schema: JsonSchema | null; tag?: string
}) {
  const [open, setOpen] = useState(false)
  const [raw, setRaw] = useState(false)
  const title = typeof schema?.title === 'string' ? schema.title : null
  const doc = typeof schema?.description === 'string' ? schema.description : null
  return (
    <div className="rounded border border-slate-800/80 bg-slate-900/40">
      <div className="flex items-center gap-1.5 px-1.5 py-0.5 font-mono text-[10px]">
        <button
          type="button"
          disabled={!schema}
          onClick={() => setOpen((v) => !v)}
          className="flex min-w-0 flex-1 items-center gap-1.5 text-left disabled:cursor-default"
        >
          {schema
            ? (open ? <ChevronDown size={10} className="shrink-0 text-slate-500" /> : <ChevronRight size={10} className="shrink-0 text-slate-500" />)
            : <span className="w-[10px] shrink-0" />}
          <span className="shrink-0 text-slate-200">{name}</span>
          {title && <span className="shrink-0 text-amber-300">{title}</span>}
          {tag && <span className="shrink-0 rounded bg-slate-800 px-1 text-[8px] text-slate-400">{tag}</span>}
          {!schema && <span className="shrink-0 text-[9px] text-rose-400">no schema declared</span>}
          {path && <span className="truncate text-sky-500/80" title={path}>{path}</span>}
        </button>
        {path && <CopyButton value={path} size={10} />}
      </div>
      {open && schema && (
        <div className="border-t border-slate-800 px-1.5 py-1">
          {doc && <p className="mb-1 whitespace-pre-line text-[9px] text-slate-500">{doc}</p>}
          <div className="mb-1 flex gap-2 text-[9px]">
            <button type="button" onClick={() => setRaw((v) => !v)} className="text-slate-500 hover:text-slate-200">
              {raw ? 'fields' : 'raw JSON Schema'}
            </button>
            <CopyButton value={JSON.stringify(schema, null, 2)} title="Copy JSON Schema" size={10} />
          </div>
          {raw
            ? <pre className="max-h-64 overflow-auto rounded bg-black p-1.5 text-[9px] text-slate-300">{JSON.stringify(schema, null, 2)}</pre>
            : <SchemaTree schema={schema} root={schema} depth={0} />}
        </div>
      )}
    </div>
  )
}


function ActionRow({ method }: { method: TypeMethod }) {
  const [open, setOpen] = useState(false)
  const args = method.args_schema ?? {}
  const props = (args.properties ?? {}) as Record<string, JsonSchema>
  const required = (args.required ?? []) as string[]
  const signature = Object.entries(props)
    .map(([k, s]) => `${k}${required.includes(k) ? '' : '?'}: ${typeLabel(s, args)}`)
    .join(', ')
  const hasDetail = Object.keys(props).length > 0 || !!method.doc || (method.publishes?.length ?? 0) > 0
  return (
    <div className="rounded border border-slate-800/80 bg-slate-900/40">
      <button
        type="button"
        disabled={!hasDetail}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 px-1.5 py-0.5 text-left font-mono text-[10px] disabled:cursor-default"
      >
        {hasDetail
          ? (open ? <ChevronDown size={10} className="shrink-0 text-slate-500" /> : <ChevronRight size={10} className="shrink-0 text-slate-500" />)
          : <span className="w-[10px] shrink-0" />}
        <span className="shrink-0 text-violet-300">{method.name}</span>
        <span className="truncate text-slate-500">({signature})</span>
      </button>
      {open && (
        <div className="space-y-1 border-t border-slate-800 px-1.5 py-1">
          {method.doc && <p className="whitespace-pre-line text-[9px] text-slate-500">{method.doc}</p>}
          {(method.publishes?.length ?? 0) > 0 && (
            <div className="font-mono text-[9px] text-slate-500">
              publishes → <span className="text-sky-400">{method.publishes!.join(', ')}</span>
            </div>
          )}
          {Object.keys(props).length > 0 && <SchemaTree schema={args} root={args} depth={0} />}
        </div>
      )}
    </div>
  )
}


// ─── JSON Schema rendering (Pydantic v2 flavour) ──────────────────────

const MAX_DEPTH = 6

/** Follow a local ``#/$defs/X`` reference against the root schema. */
function deref(schema: JsonSchema, root: JsonSchema): JsonSchema {
  const ref = schema.$ref
  if (typeof ref !== 'string' || !ref.startsWith('#/')) return schema
  let node: unknown = root
  for (const part of ref.slice(2).split('/')) {
    node = node && typeof node === 'object' ? (node as Record<string, unknown>)[part] : undefined
  }
  return node && typeof node === 'object' ? { ...(node as JsonSchema), ...omit(schema, '$ref') } : schema
}

function omit(o: JsonSchema, key: string): JsonSchema {
  const { [key]: _drop, ...rest } = o
  return rest
}

/** anyOf with a null branch (Pydantic's Optional[X]) → [X, nullable]. */
function unwrapOptional(schema: JsonSchema): [JsonSchema, boolean] {
  const any = schema.anyOf as JsonSchema[] | undefined
  if (!Array.isArray(any)) return [schema, false]
  const nonNull = any.filter((s) => s.type !== 'null')
  const nullable = nonNull.length < any.length
  if (nonNull.length === 1) return [{ ...omit(schema, 'anyOf'), ...nonNull[0] }, nullable]
  return [schema, nullable]
}

/** Compact one-line type: ``ServoState``, ``list[int]``,
 *  ``dict[str, bool]``, ``"up" | "down"``, ``float?``. */
function typeLabel(input: JsonSchema, root: JsonSchema, depth = 0): string {
  if (depth > MAX_DEPTH) return '…'
  const [unwrapped, nullable] = unwrapOptional(input)
  const suffix = nullable ? '?' : ''
  if (typeof unwrapped.$ref === 'string') {
    return (unwrapped.$ref.split('/').pop() ?? 'object') + suffix
  }
  const s = unwrapped
  if (Array.isArray(s.anyOf)) {
    return (s.anyOf as JsonSchema[]).map((b) => typeLabel(b, root, depth + 1)).join(' | ') + suffix
  }
  if (s.const !== undefined) return JSON.stringify(s.const) + suffix
  if (Array.isArray(s.enum)) return (s.enum as unknown[]).map((v) => JSON.stringify(v)).join(' | ') + suffix
  if (s.type === 'array') {
    if (Array.isArray(s.prefixItems)) {
      return `[${(s.prefixItems as JsonSchema[]).map((b) => typeLabel(b, root, depth + 1)).join(', ')}]` + suffix
    }
    return `list[${s.items ? typeLabel(s.items as JsonSchema, root, depth + 1) : 'any'}]` + suffix
  }
  if (s.type === 'object' || (!s.type && s.additionalProperties)) {
    if (s.properties) return (typeof s.title === 'string' ? s.title : 'object') + suffix
    const ap = s.additionalProperties
    if (ap && typeof ap === 'object') return `dict[str, ${typeLabel(ap as JsonSchema, root, depth + 1)}]` + suffix
    return 'object' + suffix
  }
  if (typeof s.type === 'string') return s.type + suffix
  return 'any' + suffix
}

/** The nested object model a field leads to, if any — through
 *  Optional, list items, and dict values — so it can expand in place. */
function nestedModel(input: JsonSchema, root: JsonSchema): JsonSchema | null {
  let [s] = unwrapOptional(input)
  s = deref(s, root)
  if (s.type === 'array' && s.items) return nestedModel(s.items as JsonSchema, root)
  if (s.properties) return s
  const ap = s.additionalProperties
  if (ap && typeof ap === 'object') return nestedModel(ap as JsonSchema, root)
  return null
}

function SchemaTree({ schema, root, depth }: { schema: JsonSchema; root: JsonSchema; depth: number }) {
  const resolved = deref(schema, root)
  const props = (resolved.properties ?? {}) as Record<string, JsonSchema>
  const required = (resolved.required ?? []) as string[]
  const entries = Object.entries(props)
  if (entries.length === 0) {
    const extra = resolved.additionalProperties !== false
    return <div className="text-[9px] italic text-slate-600">{extra ? 'free-form object (any fields)' : 'no fields'}</div>
  }
  return (
    <ul className="space-y-px font-mono text-[10px]">
      {entries.map(([name, field]) => (
        <FieldRow key={name} name={name} field={field} required={required.includes(name)} root={root} depth={depth} />
      ))}
    </ul>
  )
}

function FieldRow({ name, field, required, root, depth }: {
  name: string; field: JsonSchema; required: boolean; root: JsonSchema; depth: number
}) {
  const [open, setOpen] = useState(false)
  const nested = depth < MAX_DEPTH ? nestedModel(field, root) : null
  const description = typeof field.description === 'string' ? field.description : null
  const hasDefault = field.default !== undefined
  return (
    <li>
      <div className="flex items-baseline gap-1.5" style={{ paddingLeft: depth * 12 }}>
        {nested ? (
          <button type="button" onClick={() => setOpen((v) => !v)} className="shrink-0 text-slate-500 hover:text-slate-200">
            {open ? '▾' : '▸'}
          </button>
        ) : (
          <span className="w-[7px] shrink-0" />
        )}
        <span className="shrink-0 text-slate-200">
          {name}{required && <span className="text-rose-400">*</span>}
        </span>
        <span className="shrink-0 text-amber-300">{typeLabel(field, root)}</span>
        {hasDefault && <span className="shrink-0 text-slate-600">= {JSON.stringify(field.default)}</span>}
        {description && <span className="truncate text-slate-500" title={description}>{description}</span>}
      </div>
      {open && nested && <SchemaTree schema={nested} root={root} depth={depth + 1} />}
    </li>
  )
}
