import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { useApiFetch } from '../contexts/ActiveRuntimeContext'
import { useAuth } from '../contexts/AuthContext'
import type { ApiClient } from '../models/ApiClient'
import Banner from '../components/Banner'

// API Clients admin page. An api_client is a named credential whose `secret`
// authenticates HTTP requests sent with the `X-API-Key` header — the matching
// row's `roles` then drive the same role gates as a logged-in user. This is
// the place where the runtime's Admin mints / re-roles / revokes those keys
// for external integrations and service-to-service callers.
//
// Mirrors the shape of Users.tsx (single "Admin" role universe, ~250 lines).
// The one extra concern vs Users is the secret: it is minted server-side on
// create and shown back once in a copy banner, and is also revealable per-row
// from the table (it is stored as-is, not hashed — see the backend service).

interface ClientDraft {
  id: string                  // empty when creating
  name: string
  roles: string               // comma-separated
}

const EMPTY_DRAFT: ClientDraft = { id: '', name: '', roles: 'Admin' }

function rolesFromString(s: string): string[] {
  return s.split(',').map((r) => r.trim()).filter(Boolean)
}

// Best-effort clipboard copy. crypto/clipboard APIs are only exposed in a
// secure context (HTTPS or localhost); a bundle reached over plain HTTP on a
// LAN IP has no navigator.clipboard, so fall back to a hidden textarea +
// execCommand so "Copy" works regardless of how the UI was reached.
async function copyText(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    /* fall through to the legacy path */
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}

export default function ApiClients() {
  const apiFetch = useApiFetch()
  const { user } = useAuth()

  const [rows, setRows] = useState<ApiClient[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<ClientDraft | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // The freshly-minted key, surfaced once after create so the operator can
  // copy it. (It's also revealable per-row, since the backend stores it
  // as-is — but a prominent banner is the expected "here is your new key" UX.)
  const [newKey, setNewKey] = useState<{ name: string; secret: string } | null>(null)
  const [revealed, setRevealed] = useState<Set<string>>(new Set())
  const [copied, setCopied] = useState<string | null>(null)

  // RBAC gate: only Admin can manage keys. Without it /v1/api-client-list
  // would 403 and the page would just look broken.
  const isAdmin = useMemo(() => (user?.roles ?? []).includes('Admin'), [user])

  const reload = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await apiFetch<ApiClient[]>('/v1/api-client-list')
      setRows(Array.isArray(data) ? data : [])
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [apiFetch])

  useEffect(() => {
    if (isAdmin) reload()
  }, [isAdmin, reload])

  const toggleReveal = useCallback((id: string) => {
    setRevealed((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  const doCopy = useCallback(async (key: string, secret: string) => {
    const ok = await copyText(secret)
    if (ok) {
      setCopied(key)
      setTimeout(() => setCopied((c) => (c === key ? null : c)), 1500)
    } else {
      setError('Could not copy to clipboard — reveal the secret and copy it manually.')
    }
  }, [])

  const onSave = useCallback(async (e?: FormEvent) => {
    e?.preventDefault()
    if (!editing) return
    setBusy(true)
    setError(null)
    try {
      const name = editing.name.trim()
      const roles = rolesFromString(editing.roles)
      if (!name) throw new Error('Name is required')

      if (editing.id) {
        // PUT — start from the existing row so the secret + created stamp
        // survive the round-trip (the backend also does a credential-safe
        // partial update, but sending the full row is unambiguous).
        const existing = rows.find((r) => r.id === editing.id)
        if (!existing) throw new Error(`client ${editing.id} no longer exists`)
        const next: ApiClient = { ...existing, name, roles }
        await apiFetch(`/v1/api-client/${encodeURIComponent(editing.id)}`, {
          method: 'PUT',
          body: JSON.stringify(next),
        })
        setEditing(null)
      } else {
        // POST — leave `secret` unset so the server mints a strong one and
        // returns it. That returned secret is the one time we surface it in
        // full (the banner); afterwards it's reveal-on-demand in the table.
        const created = await apiFetch<ApiClient>('/v1/api-client', {
          method: 'POST',
          body: JSON.stringify({ name, roles }),
        })
        setEditing(null)
        if (created?.secret) setNewKey({ name: created.name ?? name, secret: created.secret })
      }
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }, [editing, rows, apiFetch, reload])

  const onDelete = useCallback(async (id: string) => {
    setBusy(true)
    setError(null)
    try {
      await apiFetch(`/v1/api-client/${encodeURIComponent(id)}`, { method: 'DELETE' })
      setConfirmDelete(null)
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }, [apiFetch, reload])

  if (!isAdmin) {
    return (
      <div className="mx-auto max-w-2xl space-y-4 p-6 text-sm text-slate-300">
        <h1 className="text-lg font-semibold">API Clients</h1>
        <Banner tone="error">
          Admin role required to manage API clients on this runtime.
        </Banner>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-6">
      <header className="space-y-1">
        <h1 className="text-lg font-semibold">API Clients</h1>
        <p className="text-xs text-slate-400">
          Each client has a secret that authenticates HTTP requests sent with the{' '}
          <code className="text-slate-300">X-API-Key</code> header. The client's roles then
          apply just like a logged-in user — an <code className="text-slate-300">Admin</code>{' '}
          key is a full admin credential, so treat it like a password.
        </p>
      </header>

      {error && <Banner tone="error">{error}</Banner>}
      {loading && <Banner tone="info">Loading API clients…</Banner>}

      {newKey && (
        <Banner tone="success">
          <div className="space-y-1">
            <div className="font-medium">
              New key for <span className="font-mono">{newKey.name}</span> — copy it now.
            </div>
            <div className="flex items-center gap-2">
              <code className="break-all rounded bg-slate-950 px-2 py-1 font-mono text-xs text-emerald-200">
                {newKey.secret}
              </code>
              <button
                type="button"
                onClick={() => doCopy('new', newKey.secret)}
                className="shrink-0 rounded border border-slate-600 px-2 py-0.5 text-xs hover:border-slate-400"
              >
                {copied === 'new' ? 'Copied ✓' : 'Copy'}
              </button>
              <button
                type="button"
                onClick={() => setNewKey(null)}
                className="shrink-0 rounded border border-slate-700 px-2 py-0.5 text-xs hover:border-slate-500"
              >
                Dismiss
              </button>
            </div>
          </div>
        </Banner>
      )}

      <div className="flex items-center justify-between">
        <span className="text-xs text-slate-400">{rows.length} client{rows.length === 1 ? '' : 's'}</span>
        <button
          type="button"
          onClick={() => setEditing({ ...EMPTY_DRAFT })}
          className="rounded bg-sky-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-500"
        >
          + Add client
        </button>
      </div>

      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase tracking-wider text-slate-500">
          <tr>
            <th className="py-2">Name</th>
            <th className="py-2">Roles</th>
            <th className="py-2">Secret</th>
            <th className="py-2">Created</th>
            <th className="py-2 text-right">Actions</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const id = r.id ?? ''
            const isDeleting = confirmDelete === id
            const isRevealed = revealed.has(id)
            return (
              <tr key={id} className="border-t border-slate-800">
                <td className="py-2 text-xs text-slate-200">{r.name || '—'}</td>
                <td className="py-2 text-xs text-slate-300">{(r.roles ?? []).join(', ') || '—'}</td>
                <td className="py-2">
                  <div className="flex items-center gap-1.5">
                    <code className="rounded bg-slate-950 px-1.5 py-0.5 font-mono text-[11px] text-slate-300">
                      {isRevealed ? (r.secret || '—') : '••••••••'}
                    </code>
                    <button
                      type="button"
                      onClick={() => toggleReveal(id)}
                      className="rounded border border-slate-700 px-1.5 py-0.5 text-[10px] hover:border-slate-500"
                    >
                      {isRevealed ? 'Hide' : 'Reveal'}
                    </button>
                    {r.secret && (
                      <button
                        type="button"
                        onClick={() => doCopy(id, r.secret!)}
                        className="rounded border border-slate-700 px-1.5 py-0.5 text-[10px] hover:border-slate-500"
                      >
                        {copied === id ? 'Copied ✓' : 'Copy'}
                      </button>
                    )}
                  </div>
                </td>
                <td className="py-2 text-xs text-slate-400">
                  {r.created ? new Date(r.created).toLocaleString() : '—'}
                </td>
                <td className="py-2 text-right">
                  <div className="flex items-center justify-end gap-2">
                    <button
                      type="button"
                      onClick={() => setEditing({
                        id,
                        name: r.name ?? '',
                        roles: (r.roles ?? []).join(', '),
                      })}
                      className="rounded border border-slate-700 px-2 py-0.5 text-xs hover:border-slate-500"
                    >
                      Edit
                    </button>
                    {isDeleting ? (
                      <>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => onDelete(id)}
                          className="rounded border border-rose-700 bg-rose-900/40 px-2 py-0.5 text-xs text-rose-200 hover:border-rose-500 disabled:opacity-40"
                        >
                          Confirm revoke?
                        </button>
                        <button
                          type="button"
                          onClick={() => setConfirmDelete(null)}
                          className="rounded border border-slate-700 px-2 py-0.5 text-xs hover:border-slate-500"
                        >
                          Cancel
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setConfirmDelete(id)}
                        className="rounded border border-rose-700 px-2 py-0.5 text-xs text-rose-300 hover:border-rose-500"
                      >
                        Revoke
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            )
          })}
          {!loading && rows.length === 0 && (
            <tr className="border-t border-slate-800">
              <td colSpan={5} className="py-6 text-center text-xs text-slate-500">
                No API clients yet. Add one to mint a key for an external integration.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {editing && (
        <div
          role="dialog"
          aria-modal="true"
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/70 p-4"
          onClick={(e) => { if (e.target === e.currentTarget) setEditing(null) }}
        >
          <form
            onSubmit={onSave}
            className="w-full max-w-md space-y-3 rounded-lg border border-slate-700 bg-slate-900 p-5 text-sm"
          >
            <h2 className="text-base font-semibold">{editing.id ? 'Edit client' : 'New API client'}</h2>

            <label className="block space-y-1">
              <span className="text-xs text-slate-400">Name</span>
              <input
                type="text"
                required
                autoFocus
                value={editing.name}
                onChange={(e) => setEditing({ ...editing, name: e.target.value })}
                placeholder="Reporting Pipeline"
                className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-1.5"
              />
            </label>

            <label className="block space-y-1">
              <span className="text-xs text-slate-400">Roles (comma-separated)</span>
              <input
                type="text"
                value={editing.roles}
                onChange={(e) => setEditing({ ...editing, roles: e.target.value })}
                placeholder="Admin"
                className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-1.5 font-mono text-xs"
              />
              <span className="block text-[10px] text-slate-500">
                Botgear uses <code className="text-slate-400">Admin</code> as its only meaningful
                role today. A key with no roles authenticates but passes no role gates.
              </span>
            </label>

            {!editing.id && (
              <p className="text-[10px] text-slate-500">
                The secret is generated for you on create and shown once so you can copy it.
              </p>
            )}

            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={() => setEditing(null)}
                className="rounded border border-slate-700 px-3 py-1.5 text-xs hover:border-slate-500"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={busy}
                className="rounded bg-sky-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-500 disabled:opacity-40"
              >
                {busy ? 'Saving…' : editing.id ? 'Save' : 'Create client'}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  )
}
