import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import {
  apiFetch,
  clearAuthToken,
  clearRefreshToken,
  decodeJwtPayload,
  defaultTryRefresh,
  getAuthToken,
  scheduleProactiveRefresh,
  setAuthToken,
  setRefreshToken,
} from '../lib/api'
import { wsClient } from '../runtime/wsClient'
import { useRuntimeConnections } from './RuntimeConnectionsContext'

export interface AppUser {
  id?: string
  email?: string
  fullname?: string | null
  roles?: string[]
}

interface LoginResponse {
  access_token?: string | null
  token_type?: string | null
  mfa_required?: boolean | null
  message?: string | null
  refresh_token?: string | null
}

interface AuthContextValue {
  user: AppUser | null
  loading: boolean
  signIn: (username: string, password: string) => Promise<{ error: Error | null }>
  // First-user enrollment — only succeeds on a runtime whose user table
  // is empty. The backend (POST /v1/auth/claim-first-user, see
  // botgear/api/first_user_routes.py) atomically refuses on a
  // populated runtime; the UI normally only shows the claim form when
  // GET /v1/auth/has-users said exists=false. Returns the same
  // session-established shape as signIn.
  claimFirstUser: (email: string, password: string) => Promise<{ error: Error | null }>
  signOut: () => void
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined)

function userFromToken(token: string): AppUser | null {
  const payload = decodeJwtPayload(token)
  if (!payload || !payload.user) return null
  return payload.user as AppUser
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AppUser | null>(null)
  const [loading, setLoading] = useState(true)
  const { connections } = useRuntimeConnections()

  // Each RuntimeConnection (RuntimeConnectionsContext) owns its OWN
  // WsClient, separate from the legacy same-origin `wsClient` singleton
  // above. Its constructor tries connect() once, at construction time —
  // if no token exists yet (e.g. the seed connection was created while
  // the user was still on /login), that attempt no-ops and nothing
  // retries it. Without this, a fresh sign-in leaves every
  // RuntimeConnection's socket permanently disconnected: meta.runtime_id
  // never arrives via /runtime/info, so the RuntimeSwitcher chip is
  // stuck showing the URL-derived id instead of the learned federation
  // handle until a full reload reconstructs the connection with the
  // token already in localStorage.
  const connectAllRuntimes = useCallback(() => {
    for (const c of connections) c.ws.connect()
  }, [connections])

  // Holds the cancel function for the current session's proactive-refresh
  // timer (see lib/api.ts's scheduleProactiveRefresh) so it can be torn
  // down on sign-out/expiry and not fire against a dead session.
  const cancelRefreshRef = useRef<(() => void) | null>(null)

  const stopProactiveRefresh = useCallback(() => {
    cancelRefreshRef.current?.()
    cancelRefreshRef.current = null
  }, [])

  // Arms the background refresh loop for a freshly-issued token. Runs the
  // WHOLE session's refresh cycle from one call — scheduleProactiveRefresh
  // reschedules itself on every successful refresh, so this only needs to
  // be called once per login (or once on mount, for a token found already
  // in storage), not per-refresh.
  const startProactiveRefresh = useCallback(
    (token: string) => {
      stopProactiveRefresh()
      cancelRefreshRef.current = scheduleProactiveRefresh(
        token,
        defaultTryRefresh,
        (newToken) => {
          // Extend the live WS session under the new token instead of
          // dropping the socket — the whole point for a persistent
          // control connection (e.g. teleop) is that time passing alone
          // never interrupts it. See wsClient.reauth() +
          // ws_endpoint.py's `reauth` handler / expiry watchdog.
          wsClient.reauth(newToken)
        },
      )
    },
    [stopProactiveRefresh],
  )

  useEffect(() => {
    const token = getAuthToken()
    if (token) {
      const u = userFromToken(token)
      if (u) {
        setUser(u)
        // Kick the WebSocket open eagerly — without this it'd only
        // connect lazily on the first subscribe, leaving the header
        // indicator red even though we're authenticated.
        wsClient.connect()
        connectAllRuntimes()
        // Covers a token that already expired while the tab sat idle —
        // scheduleProactiveRefresh refreshes immediately in that case
        // rather than waiting for the (already-passed) scheduled time.
        startProactiveRefresh(token)
      } else {
        clearAuthToken()
        clearRefreshToken()
      }
    }
    setLoading(false)
    return () => stopProactiveRefresh()
  }, [startProactiveRefresh, stopProactiveRefresh, connectAllRuntimes])

  // Listen for `auth:expired` events dispatched by apiFetch/wsClient once
  // a refresh attempt has already failed (see defaultOnAuthExpired).
  // Dropping `user` makes ProtectedRoute redirect to /login on the next
  // render — no manual navigate() call needed.
  useEffect(() => {
    const onExpired = () => {
      stopProactiveRefresh()
      wsClient.disconnect()
      setUser(null)
    }
    window.addEventListener('auth:expired', onExpired)
    return () => window.removeEventListener('auth:expired', onExpired)
  }, [stopProactiveRefresh])

  const signIn = useCallback(
    async (username: string, password: string) => {
      try {
        const res = await apiFetch<LoginResponse>('/v1/login', {
          method: 'POST',
          body: JSON.stringify({ username, password }),
        })
        if (!res.access_token) {
          return { error: new Error(res.message || 'Login failed') }
        }
        setAuthToken(res.access_token)
        if (res.refresh_token) setRefreshToken(res.refresh_token)
        const u = userFromToken(res.access_token)
        setUser(u)
        // Open the bus immediately on sign-in so the indicator turns
        // green before the user navigates to any subscribing page.
        wsClient.connect()
        connectAllRuntimes()
        startProactiveRefresh(res.access_token)
        return { error: null }
      } catch (err) {
        return { error: err instanceof Error ? err : new Error(String(err)) }
      }
    },
    [startProactiveRefresh, connectAllRuntimes],
  )

  const claimFirstUser = useCallback(
    async (email: string, password: string) => {
      try {
        const res = await apiFetch<LoginResponse>('/v1/auth/claim-first-user', {
          method: 'POST',
          body: JSON.stringify({ email, password }),
        })
        if (!res.access_token) {
          return { error: new Error(res.message || 'Could not create the first account') }
        }
        setAuthToken(res.access_token)
        if (res.refresh_token) setRefreshToken(res.refresh_token)
        const u = userFromToken(res.access_token)
        setUser(u)
        wsClient.connect()
        connectAllRuntimes()
        startProactiveRefresh(res.access_token)
        return { error: null }
      } catch (err) {
        return { error: err instanceof Error ? err : new Error(String(err)) }
      }
    },
    [startProactiveRefresh, connectAllRuntimes],
  )

  const signOut = useCallback(() => {
    stopProactiveRefresh()
    // Tear the WS down BEFORE clearing the token so the (now-orphaned)
    // socket doesn't try to reconnect with a stale Authorization.
    wsClient.disconnect()
    clearAuthToken()
    clearRefreshToken()
    setUser(null)
  }, [stopProactiveRefresh])

  const value = useMemo<AuthContextValue>(
    () => ({ user, loading, signIn, claimFirstUser, signOut }),
    [user, loading, signIn, claimFirstUser, signOut],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider')
  return ctx
}
