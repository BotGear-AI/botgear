// Themed react-jsonschema-form (RJSF) wrapper — dark-slate palette matching
// the rest of the app (see FieldInput in InstallWizard.tsx for the style
// this mirrors). Renders any Pydantic-derived JSON Schema (config_schema)
// with an optional RJSF ui:schema (ui_schema) for widget/order hints.
//
// Consumers render <SchemaForm> uncontrolled — pass an initial `formData`
// and `onSubmit`, no `onChange` needed — then trigger validation + submit
// externally via a ref: `formRef.current?.submit()`. The default submit
// button is suppressed (both via `children={null}` and a norender
// SubmitButton template) since callers supply their own Save button in
// their own modal chrome.
import { forwardRef, useMemo, useState } from 'react'
import { withTheme, type ThemeProps } from '@rjsf/core'
import type Form from '@rjsf/core'
import validator from '@rjsf/validator-ajv8'
import {
  ADDITIONAL_PROPERTY_FLAG,
  ariaDescribedByIds,
  buttonId,
  descriptionId,
  getInputProps,
  getTemplate,
  getUiOptions,
  schemaRequiresTrueValue,
  titleId,
  type BaseInputTemplateProps,
  type DescriptionFieldProps,
  type FieldErrorProps,
  type FieldTemplateProps,
  type IconButtonProps,
  type ObjectFieldTemplateProps,
  type SubmitButtonProps,
  type TitleFieldProps,
  type WidgetProps,
  type WrapIfAdditionalTemplateProps,
} from '@rjsf/utils'
import { ArrowDown, ArrowUp, Eye, EyeOff, Plus, Trash2 } from 'lucide-react'

const inputCls =
  'w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100 ' +
  'placeholder:text-slate-500 focus:border-sky-500 focus:outline-none disabled:opacity-50'

// ─── field chrome ──────────────────────────────────────────────────────

function FieldTemplate(props: FieldTemplateProps) {
  const { id, label, children, errors, help, description, hidden, required, displayLabel, uiSchema } = props
  if (hidden) return <div className="hidden">{children}</div>
  const isCheckbox = getUiOptions(uiSchema).widget === 'checkbox'
  return (
    <div className="mb-3 last:mb-0">
      {displayLabel && !isCheckbox && label && (
        <label htmlFor={id} className="mb-1 block text-xs text-slate-300">
          {label}
          {required && <span className="ml-0.5 text-rose-400">*</span>}
        </label>
      )}
      {displayLabel && description}
      {children}
      {errors}
      {help}
    </div>
  )
}

function DescriptionField({ id, description }: DescriptionFieldProps) {
  if (!description) return null
  return (
    <p id={id} className="mb-1 whitespace-pre-wrap text-[10px] text-slate-500">
      {description}
    </p>
  )
}

function TitleField({ id, title, required }: TitleFieldProps) {
  if (!title) return null
  return (
    <legend id={id} className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-400">
      {title}
      {required && <span className="ml-0.5 text-rose-400">*</span>}
    </legend>
  )
}

function FieldErrorTemplate({ errors = [], fieldPathId }: FieldErrorProps) {
  if (errors.length === 0) return null
  return (
    <ul id={`${fieldPathId.$id}__error`} className="mt-1 space-y-0.5">
      {errors.filter(Boolean).map((error, i) => (
        // eslint-disable-next-line react/no-array-index-key
        <li key={i} className="text-[11px] text-rose-400">
          {error}
        </li>
      ))}
    </ul>
  )
}

function ErrorListTemplate({ errors }: { errors: Array<{ stack: string }> }) {
  if (errors.length === 0) return null
  return (
    <div className="mb-3 rounded border border-rose-500/40 bg-rose-500/10 p-2 text-xs text-rose-200">
      <p className="mb-1 font-semibold">Please fix the following:</p>
      <ul className="list-inside list-disc space-y-0.5">
        {errors.map((err, i) => (
          // eslint-disable-next-line react/no-array-index-key
          <li key={i}>{err.stack}</li>
        ))}
      </ul>
    </div>
  )
}

// Root object gets no border (the modal chrome already frames it) and its
// (often long — Pydantic class docstrings) description renders like the
// install wizard's license block: a bounded, scrollable mono panel rather
// than an unbounded wall of text. Nested objects get a light box + legend.
function ObjectFieldTemplate(props: ObjectFieldTemplateProps) {
  const { description, properties, title, uiSchema, registry, schema, required, fieldPathId } = props
  const isRoot = fieldPathId.path.length === 0
  const options = getUiOptions(uiSchema)
  const TitleFieldTemplate = getTemplate('TitleFieldTemplate', registry, options)
  const DescriptionFieldTemplate = getTemplate('DescriptionFieldTemplate', registry, options)
  return (
    <fieldset
      id={fieldPathId.$id}
      className={isRoot ? 'space-y-3' : 'space-y-2 rounded border border-slate-800 bg-slate-950/40 p-3'}
    >
      {!isRoot && title && (
        <TitleFieldTemplate
          id={titleId(fieldPathId)}
          title={title}
          required={required}
          schema={schema}
          uiSchema={uiSchema}
          registry={registry}
        />
      )}
      {description ? (
        isRoot ? (
          <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded bg-slate-950 p-3 font-mono text-[11px] leading-snug text-slate-400">
            {description}
          </pre>
        ) : (
          <DescriptionFieldTemplate
            id={descriptionId(fieldPathId)}
            description={description}
            schema={schema}
            uiSchema={uiSchema}
            registry={registry}
          />
        )
      ) : null}
      {properties.map((p) => p.content)}
    </fieldset>
  )
}

// Themed key-rename row for genuinely free-form dict fields (e.g. every
// subprocess service's topic_remap: Dict[str, str]) — the one place RJSF's
// default templates render a raw, unstyled <input> outside BaseInputTemplate.
function WrapIfAdditionalTemplate(props: WrapIfAdditionalTemplateProps) {
  const { id, label, onKeyRenameBlur, onRemoveProperty, required, schema, children, uiSchema, registry, disabled, readonly } = props
  const additional = ADDITIONAL_PROPERTY_FLAG in schema
  if (!additional) return <div>{children}</div>
  const { RemoveButton } = registry.templates.ButtonTemplates
  return (
    <div className="mb-2 flex items-end gap-2">
      <div className="flex-1">
        <label htmlFor={`${id}-key`} className="mb-1 block text-xs text-slate-300">
          key{required && <span className="ml-0.5 text-rose-400">*</span>}
        </label>
        <input key={label} id={`${id}-key`} defaultValue={label} onBlur={onKeyRenameBlur} className={inputCls} />
      </div>
      <div className="flex-1">{children}</div>
      <RemoveButton
        id={buttonId(id, 'remove')}
        disabled={disabled || readonly}
        onClick={onRemoveProperty}
        uiSchema={uiSchema}
        registry={registry}
      />
    </div>
  )
}

// ─── inputs ──────────────────────────────────────────────────────────

function BaseInputTemplate(props: BaseInputTemplateProps) {
  const {
    id, name: _name, htmlName, value, readonly, disabled, autofocus, onBlur, onFocus, onChange, onChangeOverride,
    options, schema, type, hideLabel: _hideLabel, hideError: _hideError,
    uiSchema: _uiSchema, registry: _registry, rawErrors: _rawErrors, ...rest
  } = props
  const [visible, setVisible] = useState(false)
  const inputProps = { ...rest, ...getInputProps(schema, type, options) }
  const isPassword = inputProps.type === 'password'
  const inputValue = inputProps.type === 'number' ? (value || value === 0 ? value : '') : (value ?? '')
  const input = (
    <input
      id={id}
      name={htmlName || id}
      className={`${inputCls} ${isPassword ? 'pr-8' : ''}`}
      readOnly={readonly}
      disabled={disabled}
      autoFocus={autofocus}
      value={inputValue}
      {...inputProps}
      {...(isPassword ? { type: visible ? 'text' : 'password' } : {})}
      onChange={onChangeOverride ?? ((e) => onChange(e.target.value === '' ? options.emptyValue : e.target.value))}
      onBlur={(e) => onBlur(id, e.target.value)}
      onFocus={(e) => onFocus(id, e.target.value)}
      aria-describedby={ariaDescribedByIds(id)}
    />
  )
  if (!isPassword) return input
  return (
    <span className="relative block">
      {input}
      <button
        type="button"
        tabIndex={-1}
        onClick={() => setVisible((v) => !v)}
        title={visible ? 'Hide value' : 'Show value'}
        className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-slate-400 hover:text-slate-200"
      >
        {visible ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
      </button>
    </span>
  )
}

function CheckboxWidget({
  schema, id, value, disabled, readonly, label, hideLabel, autofocus, onBlur, onFocus, onChange, htmlName,
}: WidgetProps) {
  const required = schemaRequiresTrueValue(schema)
  return (
    <label className="flex items-start gap-2 text-xs text-slate-300">
      <input
        type="checkbox"
        id={id}
        name={htmlName || id}
        checked={typeof value === 'undefined' ? false : value}
        required={required}
        disabled={disabled || readonly}
        autoFocus={autofocus}
        onChange={(e) => onChange(e.target.checked)}
        onBlur={(e) => onBlur(id, e.target.checked)}
        onFocus={(e) => onFocus(id, e.target.checked)}
        className="mt-0.5"
      />
      {!hideLabel && <span>{label}</span>}
    </label>
  )
}

function SelectWidget({ id, options, value, disabled, readonly, multiple, autofocus, onChange, placeholder, htmlName }: WidgetProps) {
  const { enumOptions, emptyValue } = options as { enumOptions?: Array<{ value: unknown; label: string }>; emptyValue?: unknown }
  return (
    <select
      id={id}
      name={htmlName || id}
      multiple={!!multiple}
      value={value ?? (multiple ? [] : (emptyValue as string) ?? '')}
      disabled={disabled || readonly}
      autoFocus={autofocus}
      onChange={(e) =>
        onChange(multiple ? Array.from(e.target.selectedOptions).map((o) => o.value) : e.target.value)
      }
      className={inputCls}
    >
      {!multiple && <option value="">{placeholder}</option>}
      {(enumOptions ?? []).map(({ value: v, label: l }) => (
        <option key={String(v)} value={v as string}>
          {l}
        </option>
      ))}
    </select>
  )
}

// ─── buttons ─────────────────────────────────────────────────────────

function SubmitButton(_props: SubmitButtonProps) {
  // Callers drive submission via a ref (formRef.current.submit()) from
  // their own footer button — see the module docstring.
  return null
}

function AddButton({ onClick, disabled, className }: IconButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`mt-1 flex items-center gap-1 rounded border border-slate-700 px-2 py-1 text-[11px] text-slate-300 hover:border-sky-500 hover:text-sky-300 disabled:opacity-40 ${className ?? ''}`}
    >
      <Plus className="h-3 w-3" /> Add
    </button>
  )
}

function RemoveButton({ onClick, disabled }: IconButtonProps) {
  return (
    <button
      type="button"
      title="Remove"
      onClick={onClick}
      disabled={disabled}
      className="shrink-0 rounded border border-slate-700 p-1.5 text-slate-400 hover:border-rose-500 hover:text-rose-300 disabled:opacity-40"
    >
      <Trash2 className="h-3.5 w-3.5" />
    </button>
  )
}

function MoveUpButton({ onClick, disabled }: IconButtonProps) {
  return (
    <button
      type="button"
      title="Move up"
      onClick={onClick}
      disabled={disabled}
      className="shrink-0 rounded border border-slate-700 p-1 text-slate-400 hover:border-slate-500 hover:text-slate-200 disabled:opacity-30"
    >
      <ArrowUp className="h-3.5 w-3.5" />
    </button>
  )
}

function MoveDownButton({ onClick, disabled }: IconButtonProps) {
  return (
    <button
      type="button"
      title="Move down"
      onClick={onClick}
      disabled={disabled}
      className="shrink-0 rounded border border-slate-700 p-1 text-slate-400 hover:border-slate-500 hover:text-slate-200 disabled:opacity-30"
    >
      <ArrowDown className="h-3.5 w-3.5" />
    </button>
  )
}

// ─── theme ───────────────────────────────────────────────────────────

const theme: ThemeProps = {
  templates: {
    FieldTemplate,
    DescriptionFieldTemplate: DescriptionField,
    TitleFieldTemplate: TitleField,
    FieldErrorTemplate,
    ErrorListTemplate,
    ObjectFieldTemplate,
    WrapIfAdditionalTemplate,
    BaseInputTemplate,
    ButtonTemplates: { SubmitButton, AddButton, RemoveButton, MoveUpButton, MoveDownButton },
  },
  widgets: { CheckboxWidget, SelectWidget },
}

const ThemedForm = withTheme(theme)

// ─── schema normalization ───────────────────────────────────────────

function isNullSchema(s: unknown): boolean {
  return (
    !!s &&
    typeof s === 'object' &&
    !Array.isArray(s) &&
    Object.keys(s as object).length === 1 &&
    (s as Record<string, unknown>).type === 'null'
  )
}

/** Collapse Pydantic's ``Optional[X]`` JSON-Schema shape —
 *  ``{"anyOf": [X, {"type": "null"}], ...}`` (either branch order) —
 *  down to plain ``X`` with the outer title/description/default kept.
 *
 *  RJSF's default anyOf widget renders EVERY anyOf as a "pick a
 *  branch" dropdown. For a plain optional field that's pure noise —
 *  "<title> option 1" / "<title> option 2" with nothing meaningful to
 *  choose between, since one "option" is just "unset". A field's
 *  optionality is already carried by Pydantic's ``required`` list;
 *  the value can still be empty/null through the normal widget, no
 *  picker needed.
 *
 *  A genuine multi-branch union (anyOf.length !== 2, or neither
 *  branch is exactly {type:"null"}) is left untouched — that's a real
 *  choice, not an Optional[]. Recurses into properties / items /
 *  $defs / definitions / additionalProperties so nested objects and
 *  array-of-object fields (and Pydantic's $ref'd sub-models) get the
 *  same treatment. */
function stripNullableAnyOf(schema: unknown): unknown {
  if (!schema || typeof schema !== 'object') return schema
  if (Array.isArray(schema)) return schema.map(stripNullableAnyOf)

  let result = schema as Record<string, unknown>
  const anyOf = result.anyOf
  if (Array.isArray(anyOf) && anyOf.length === 2) {
    const [a, b] = anyOf
    const real = isNullSchema(a) ? b : isNullSchema(b) ? a : null
    if (real && typeof real === 'object') {
      const { anyOf: _drop, ...rest } = result
      const realRec = real as Record<string, unknown>
      // Keep "null" a valid value, not just a valid *default*: RJSF
      // submits every property on every save (see ConfigWizard's
      // docstring), so an untouched Optional field round-trips as
      // literal `null`. Dropping the null branch down to a bare
      // ``type: "string"`` made ajv reject that as "must be string" on
      // every save — confirmed live against network_scanner's
      // alert_target. ``type: [X, "null"]`` is RJSF/ajv8's own
      // documented nullable idiom: it still resolves to the plain X
      // widget (no anyOf picker) while accepting null.
      const realType = realRec.type
      const mergedType = typeof realType === 'string' ? [realType, 'null'] : realType
      result = {
        ...realRec,
        ...rest,
        ...(mergedType ? { type: mergedType } : {}),
      }
    }
  }

  const next: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(result)) {
    if (key === 'properties' || key === '$defs' || key === 'definitions') {
      const obj = (value ?? {}) as Record<string, unknown>
      next[key] = Object.fromEntries(Object.entries(obj).map(([k, v]) => [k, stripNullableAnyOf(v)]))
    } else if (key === 'items' || (key === 'additionalProperties' && value && typeof value === 'object')) {
      next[key] = stripNullableAnyOf(value)
    } else {
      next[key] = value
    }
  }
  return next
}

// forwardRef pass-through so callers can do `formRef.current?.submit()`.
export const SchemaForm = forwardRef<Form<any>, React.ComponentProps<typeof ThemedForm>>(function SchemaForm(
  { schema, ...props },
  ref,
) {
  const cleanedSchema = useMemo(() => stripNullableAnyOf(schema), [schema])
  return <ThemedForm ref={ref} noHtml5Validate schema={cleanedSchema as typeof schema} {...props} />
})

export const schemaFormValidator = validator
export type SchemaFormRef = Form<any>
