const TOKEN_STORAGE_KEY = 'botgear.access_token'
const REFRESH_TOKEN_STORAGE_KEY = 'botgear.refresh_token'

export function getAuthToken(): string | null {
  return localStorage.getItem(TOKEN_STORAGE_KEY)
}

export function setAuthToken(token: string): void {
  localStorage.setItem(TOKEN_STORAGE_KEY, token)
}

export function clearAuthToken(): void {
  localStorage.removeItem(TOKEN_STORAGE_KEY)
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_TOKEN_STORAGE_KEY)
}

export function setRefreshToken(token: string): void {
  localStorage.setItem(REFRESH_TOKEN_STORAGE_KEY, token)
}

export function clearRefreshToken(): void {
  localStorage.removeItem(REFRESH_TOKEN_STORAGE_KEY)
}

export interface JwtPayload {
  exp?: number
  user?: { id?: string; email?: string; fullname?: string; roles?: string[] }
  [key: string]: unknown
}

export function decodeJwtPayload(token: string): JwtPayload | null {
  try {
    const [, payload] = token.split('.')
    if (!payload) return null
    const padded = payload + '='.repeat((4 - (payload.length % 4)) % 4)
    const json = atob(padded.replace(/-/g, '+').replace(/_/g, '/'))
    return JSON.parse(json) as JwtPayload
  } catch {
    return null
  }
}

export class ApiError extends Error {
  status: number
  body: unknown
  constructor(status: number, message: string, body: unknown) {
    super(message)
    this.status = status
    this.body = body
  }
}

// ─── refresh-token plumbing ─────────────────────────────────────────────
//
// The backend has had a working POST /v1/refresh-token (rotating refresh
// tokens, an auth_session table, idle-timeout enforcement — see
// botgear/server.py) since before any of this existed on the frontend;
// nothing here called it. That's the root of "daily I get logged out with
// no warning" — every session was riding a bare 60-minute access token
// with no renewal path, so expiry always meant a hard stop. Everything
// below exists to use the refresh token that was already being issued.

export interface RefreshResult {
  accessToken: string
  refreshToken: string
}

/**
 * Call POST /v1/refresh-token directly via `fetch` — NOT through
 * `apiFetch`, which would recurse (apiFetch's own 401 handling is what
 * calls this). Throws on any failure (network, non-2xx, malformed body)
 * so every caller can treat "couldn't refresh" uniformly as "this refresh
 * token is no good, fall back to interactive login."
 */
export async function performTokenRefresh(baseUrl: string, refreshToken: string): Promise<RefreshResult> {
  const url = (baseUrl || '').replace(/\/+$/, '') + '/v1/refresh-token'
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: refreshToken }),
  })
  if (!response.ok) {
    throw new Error(`refresh failed: HTTP ${response.status}`)
  }
  const body = (await response.json().catch(() => null)) as
    | { access_token?: string; refresh_token?: string }
    | null
  if (!body?.access_token || !body?.refresh_token) {
    throw new Error('refresh response missing tokens')
  }
  return { accessToken: body.access_token, refreshToken: body.refresh_token }
}

/**
 * Shared refresh closure for anything using the legacy global token pair
 * (the default same-origin apiFetch/wsClient, and any RuntimeConnection
 * still on the shared-token fallback — see RuntimeConnection.ts). Reads
 * + rewrites the global localStorage pair; returns the new access token
 * on success, null on failure (and clears the dead refresh token so we
 * don't keep retrying it).
 */
export async function defaultTryRefresh(): Promise<string | null> {
  const rt = getRefreshToken()
  if (!rt) return null
  try {
    const { accessToken, refreshToken } = await performTokenRefresh('', rt)
    setAuthToken(accessToken)
    setRefreshToken(refreshToken)
    return accessToken
  } catch {
    clearRefreshToken()
    return null
  }
}

/**
 * Shared "give up, sign out" closure — clears the global token pair and
 * fires the window-level event AuthContext listens for (drops `user`,
 * which makes ProtectedRoute redirect to /login). Both the default
 * apiFetch and wsClient singletons use this as their last resort, after a
 * refresh attempt has already failed.
 */
export function defaultOnAuthExpired(): void {
  clearAuthToken()
  clearRefreshToken()
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('auth:expired'))
  }
}

/**
 * Schedule a proactive access-token refresh at ~80% of its remaining
 * lifetime (immediately, if that point has already passed — e.g. a tab
 * left open past expiry). The point is that an active session — a robot
 * being teleoperated is the motivating case — never hits a live 401 in
 * the first place; the token gets silently swapped out in the
 * background. Self-reschedules on every successful refresh, so one call
 * covers the whole session lifetime, not just the next renewal.
 *
 * ``doRefresh`` performs the actual refresh AND persists the new tokens
 * wherever this caller's tokens live (localStorage for the default
 * singleton, in-memory auth state for a RuntimeConnection) — this
 * function only owns the timing. Returns a cancel function; call it on
 * sign-out or unmount so a stale timer doesn't fire against a dead
 * session.
 *
 * On failure, this stops rescheduling and returns — it does NOT sign
 * anyone out itself (that's a policy decision belonging to the caller,
 * e.g. AuthContext dispatching auth:expired from its own doRefresh).
 */
export function scheduleProactiveRefresh(
  token: string,
  doRefresh: () => Promise<string | null>,
  onRefreshed?: (newToken: string) => void,
): () => void {
  let timer: ReturnType<typeof setTimeout> | null = null
  let cancelled = false

  const arm = (tok: string): void => {
    const payload = decodeJwtPayload(tok)
    if (!payload?.exp) return // no exp claim — nothing to schedule against
    const nowS = Date.now() / 1000
    const remainingS = payload.exp - nowS
    const delayMs = Math.max(0, remainingS * 0.8 * 1000)
    timer = setTimeout(() => {
      void (async () => {
        if (cancelled) return
        const fresh = await doRefresh().catch(() => null)
        if (cancelled) return
        if (fresh) {
          onRefreshed?.(fresh)
          arm(fresh)
        }
        // Failure: intentionally not rescheduling. Caller's doRefresh owns
        // deciding what "give up" means (see module doc above).
      })()
    }, delayMs)
  }

  arm(token)
  return () => {
    cancelled = true
    if (timer) clearTimeout(timer)
  }
}

/**
 * Per-runtime apiFetch configuration.
 *
 * ``baseUrl`` is the origin of the target runtime, e.g.
 * ``http://10.0.0.5:8998``. Empty string means "same origin" — the
 * default ``apiFetch`` factory below uses that so existing callers
 * keep hitting ``/v1/...`` via Vite's dev proxy or the same backend
 * that served the SPA.
 *
 * ``getToken`` returns the current access token (or null) for THIS
 * runtime. It's a callback rather than a value so the apiFetch always
 * sees the live token — important because tokens rotate on refresh
 * and because the multi-runtime layer keeps tokens in-memory per
 * connection.
 *
 * ``tryRefresh`` is called on a 401 with a token attached, BEFORE
 * ``onAuthExpired``. If it resolves to a new token, the ORIGINAL request
 * is retried once with it; only if that retry also fails (or tryRefresh
 * itself returns null) does ``onAuthExpired`` fire. Omitting it preserves
 * the old "401 -> onAuthExpired immediately" behavior.
 *
 * ``onAuthExpired`` is the last resort — the default clears localStorage
 * + fires a window-level ``auth:expired`` event for the AuthContext /
 * ProtectedRoute machinery. The multi-runtime layer passes a
 * per-connection handler so an expiry on one runtime doesn't sign the
 * user out of every other one (see RuntimeConnection.ts).
 */
export interface ApiFetchConfig {
  baseUrl: string
  getToken: () => string | null
  onAuthExpired?: () => void
  tryRefresh?: () => Promise<string | null>
}

interface RawResult<T> {
  ok: boolean
  status: number
  detail: string
  body: unknown
  parsed?: T
}

/**
 * Build an apiFetch bound to a specific runtime.
 *
 * Each call returns an independent function with its own
 * baseUrl/getToken closure. The multi-runtime layer holds one of
 * these per ``RuntimeConnection``.
 */
export function createApiFetch(cfg: ApiFetchConfig) {
  const baseUrl = (cfg.baseUrl || '').replace(/\/+$/, '')

  async function doRequest<T>(path: string, init: RequestInit, token: string | null): Promise<RawResult<T>> {
    const headers = new Headers(init.headers)
    if (!headers.has('Content-Type') && init.body) {
      headers.set('Content-Type', 'application/json')
    }
    if (token && !headers.has('Authorization')) {
      headers.set('Authorization', `Bearer ${token}`)
    }
    // ``baseUrl + path`` works for both same-origin ("" + "/v1/foo")
    // and cross-origin ("http://10.0.0.5:8998" + "/v1/foo"). Path is
    // expected to start with '/'.
    const url = baseUrl + path
    const response = await fetch(url, { ...init, headers })
    const contentType = response.headers.get('content-type') ?? ''
    const body = contentType.includes('application/json')
      ? await response.json().catch(() => null)
      : await response.text().catch(() => '')
    if (!response.ok) {
      const detail =
        (body && typeof body === 'object' && 'detail' in body
          ? String((body as { detail: unknown }).detail)
          : null) ?? `HTTP ${response.status}`
      return { ok: false, status: response.status, detail, body }
    }
    return { ok: true, status: response.status, detail: '', body, parsed: body as T }
  }

  return async function apiFetch<T = unknown>(
    path: string,
    init: RequestInit = {},
  ): Promise<T> {
    const token = cfg.getToken()
    let result = await doRequest<T>(path, init, token)

    // A 401 on a request that HAD a token means the server explicitly
    // rejected it (as opposed to "no session at all", which isn't an
    // expiry). Try one silent refresh-and-retry before giving up.
    if (!result.ok && result.status === 401 && token && cfg.tryRefresh) {
      const fresh = await cfg.tryRefresh().catch(() => null)
      if (fresh) {
        result = await doRequest<T>(path, init, fresh)
      }
    }

    if (!result.ok) {
      if (result.status === 401 && token) {
        cfg.onAuthExpired?.()
      }
      throw new ApiError(result.status, result.detail, result.body)
    }
    return result.parsed as T
  }
}

/**
 * Default same-origin apiFetch — preserves today's behaviour
 * (relative paths hit the SPA's origin, token from localStorage,
 * window-level ``auth:expired`` event), now with a silent
 * refresh-before-giving-up step. Existing imports
 * (``import { apiFetch } from '../lib/api'``) keep working unchanged.
 */
export const apiFetch = createApiFetch({
  baseUrl: '',
  getToken: getAuthToken,
  tryRefresh: defaultTryRefresh,
  onAuthExpired: defaultOnAuthExpired,
})
