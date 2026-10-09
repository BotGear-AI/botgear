import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  ApiError,
  clearAuthToken,
  createApiFetch,
  decodeJwtPayload,
  defaultOnAuthExpired,
  getAuthToken,
  scheduleProactiveRefresh,
  setAuthToken,
  setRefreshToken,
} from './api'

/** Base64url-encodes a JWT-shaped token carrying `payload` (header and
 *  signature are dummy — nothing here verifies them). */
function makeJwt(payload: Record<string, unknown>): string {
  const toB64Url = (obj: unknown) =>
    btoa(JSON.stringify(obj)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  return `${toB64Url({ alg: 'HS256' })}.${toB64Url(payload)}.sig`
}

/** A Response-like object with just what doRequest() reads. */
function fakeResponse(status: number, body: unknown, { json = true } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => (name === 'content-type' && json ? 'application/json' : 'text/plain') },
    json: async () => body,
    text: async () => String(body),
  } as unknown as Response
}

beforeEach(() => {
  localStorage.clear()
})

describe('decodeJwtPayload', () => {
  it('decodes a well-formed token', () => {
    expect(decodeJwtPayload(makeJwt({ exp: 123, user: { id: 'u1' } }))).toEqual({ exp: 123, user: { id: 'u1' } })
  })

  it('returns null for a token with no payload segment', () => {
    expect(decodeJwtPayload('onlyonesegment')).toBeNull()
  })

  it('returns null for malformed base64/JSON rather than throwing', () => {
    expect(decodeJwtPayload('a.not-valid-base64!!!.c')).toBeNull()
  })

  it('handles base64url characters (-/_) and missing padding', () => {
    // A payload whose base64 happens to need '-'/'_' substitution and
    // isn't a multiple of 4 chars — the exact edge decodeJwtPayload's
    // repadding exists for.
    const token = makeJwt({ exp: 1, note: '>>??>>' })
    expect(decodeJwtPayload(token)?.exp).toBe(1)
  })
})

describe('token storage', () => {
  it('round-trips the access token through localStorage', () => {
    expect(getAuthToken()).toBeNull()
    setAuthToken('tok-1')
    expect(getAuthToken()).toBe('tok-1')
    clearAuthToken()
    expect(getAuthToken()).toBeNull()
  })
})

describe('defaultOnAuthExpired', () => {
  it('clears both tokens and fires a window auth:expired event', () => {
    setAuthToken('tok-1')
    setRefreshToken('rt-1')
    const onExpired = vi.fn()
    window.addEventListener('auth:expired', onExpired)
    defaultOnAuthExpired()
    window.removeEventListener('auth:expired', onExpired)

    expect(getAuthToken()).toBeNull()
    expect(localStorage.getItem('botgear.refresh_token')).toBeNull()
    expect(onExpired).toHaveBeenCalledTimes(1)
  })
})

describe('createApiFetch', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('parses a successful JSON response and returns its body', async () => {
    const fetchMock = vi.fn().mockResolvedValue(fakeResponse(200, { ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    const apiFetch = createApiFetch({ baseUrl: '', getToken: () => null })

    const out = await apiFetch('/v1/thing')
    expect(out).toEqual({ ok: true })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/v1/thing')
    expect((init.headers as Headers).has('Authorization')).toBe(false)
  })

  it('joins baseUrl + path and strips a trailing slash from baseUrl', async () => {
    const fetchMock = vi.fn().mockResolvedValue(fakeResponse(200, {}))
    vi.stubGlobal('fetch', fetchMock)
    const apiFetch = createApiFetch({ baseUrl: 'http://10.0.0.5:8998/', getToken: () => null })
    await apiFetch('/v1/thing')
    expect(fetchMock.mock.calls[0][0]).toBe('http://10.0.0.5:8998/v1/thing')
  })

  it('attaches a Bearer Authorization header when a token is present', async () => {
    const fetchMock = vi.fn().mockResolvedValue(fakeResponse(200, {}))
    vi.stubGlobal('fetch', fetchMock)
    const apiFetch = createApiFetch({ baseUrl: '', getToken: () => 'tok-1' })
    await apiFetch('/v1/thing')
    const init = fetchMock.mock.calls[0][1]
    expect((init.headers as Headers).get('Authorization')).toBe('Bearer tok-1')
  })

  it('does not overwrite an Authorization header the caller already set', async () => {
    const fetchMock = vi.fn().mockResolvedValue(fakeResponse(200, {}))
    vi.stubGlobal('fetch', fetchMock)
    const apiFetch = createApiFetch({ baseUrl: '', getToken: () => 'tok-1' })
    await apiFetch('/v1/thing', { headers: { Authorization: 'Bearer caller-set' } })
    const init = fetchMock.mock.calls[0][1]
    expect((init.headers as Headers).get('Authorization')).toBe('Bearer caller-set')
  })

  it('defaults Content-Type to application/json when a body is given', async () => {
    const fetchMock = vi.fn().mockResolvedValue(fakeResponse(200, {}))
    vi.stubGlobal('fetch', fetchMock)
    const apiFetch = createApiFetch({ baseUrl: '', getToken: () => null })
    await apiFetch('/v1/thing', { method: 'POST', body: JSON.stringify({ a: 1 }) })
    const init = fetchMock.mock.calls[0][1]
    expect((init.headers as Headers).get('Content-Type')).toBe('application/json')
  })

  it('throws ApiError with the body\'s detail on a non-ok response', async () => {
    const fetchMock = vi.fn().mockResolvedValue(fakeResponse(404, { detail: 'not found' }))
    vi.stubGlobal('fetch', fetchMock)
    const apiFetch = createApiFetch({ baseUrl: '', getToken: () => null })

    await expect(apiFetch('/v1/thing')).rejects.toMatchObject(
      new ApiError(404, 'not found', { detail: 'not found' }),
    )
  })

  it('falls back to "HTTP <status>" when the error body has no detail', async () => {
    const fetchMock = vi.fn().mockResolvedValue(fakeResponse(500, { oops: true }))
    vi.stubGlobal('fetch', fetchMock)
    const apiFetch = createApiFetch({ baseUrl: '', getToken: () => null })

    await expect(apiFetch('/v1/thing')).rejects.toThrow('HTTP 500')
  })

  it('on 401 with a token: retries once via tryRefresh, and succeeds if the retry succeeds', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(fakeResponse(401, { detail: 'expired' }))
      .mockResolvedValueOnce(fakeResponse(200, { ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    const tryRefresh = vi.fn().mockResolvedValue('fresh-token')
    const onAuthExpired = vi.fn()
    const apiFetch = createApiFetch({ baseUrl: '', getToken: () => 'stale-token', tryRefresh, onAuthExpired })

    const out = await apiFetch('/v1/thing')

    expect(out).toEqual({ ok: true })
    expect(tryRefresh).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect((fetchMock.mock.calls[1][1].headers as Headers).get('Authorization')).toBe('Bearer fresh-token')
    expect(onAuthExpired).not.toHaveBeenCalled()
  })

  it('on 401 with a token: calls onAuthExpired and throws when tryRefresh yields nothing', async () => {
    const fetchMock = vi.fn().mockResolvedValue(fakeResponse(401, { detail: 'expired' }))
    vi.stubGlobal('fetch', fetchMock)
    const tryRefresh = vi.fn().mockResolvedValue(null)
    const onAuthExpired = vi.fn()
    const apiFetch = createApiFetch({ baseUrl: '', getToken: () => 'stale-token', tryRefresh, onAuthExpired })

    await expect(apiFetch('/v1/thing')).rejects.toThrow('expired')
    expect(fetchMock).toHaveBeenCalledTimes(1) // no retry — tryRefresh gave nothing to retry with
    expect(onAuthExpired).toHaveBeenCalledTimes(1)
  })

  it('on 401 with NO token: skips tryRefresh and onAuthExpired entirely', async () => {
    const fetchMock = vi.fn().mockResolvedValue(fakeResponse(401, { detail: 'nope' }))
    vi.stubGlobal('fetch', fetchMock)
    const tryRefresh = vi.fn()
    const onAuthExpired = vi.fn()
    const apiFetch = createApiFetch({ baseUrl: '', getToken: () => null, tryRefresh, onAuthExpired })

    await expect(apiFetch('/v1/thing')).rejects.toThrow('nope')
    expect(tryRefresh).not.toHaveBeenCalled()
    expect(onAuthExpired).not.toHaveBeenCalled()
  })

  it('a tryRefresh that throws is treated the same as one that returns null', async () => {
    const fetchMock = vi.fn().mockResolvedValue(fakeResponse(401, {}))
    vi.stubGlobal('fetch', fetchMock)
    const tryRefresh = vi.fn().mockRejectedValue(new Error('network down'))
    const onAuthExpired = vi.fn()
    const apiFetch = createApiFetch({ baseUrl: '', getToken: () => 'stale', tryRefresh, onAuthExpired })

    await expect(apiFetch('/v1/thing')).rejects.toThrow()
    expect(onAuthExpired).toHaveBeenCalledTimes(1)
  })
})

describe('scheduleProactiveRefresh', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('fires doRefresh at ~80% of the token\'s remaining lifetime and reschedules on success', async () => {
    const nowS = Date.now() / 1000
    const token = makeJwt({ exp: nowS + 100 }) // 100s remaining -> fires at ~80s
    const nextToken = makeJwt({ exp: nowS + 200 })
    const doRefresh = vi.fn().mockResolvedValue(nextToken)
    const onRefreshed = vi.fn()

    scheduleProactiveRefresh(token, doRefresh, onRefreshed)

    await vi.advanceTimersByTimeAsync(79_000)
    expect(doRefresh).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(2_000)
    expect(doRefresh).toHaveBeenCalledTimes(1)
    expect(onRefreshed).toHaveBeenCalledWith(nextToken)
  })

  it('does not reschedule when doRefresh resolves to null', async () => {
    const nowS = Date.now() / 1000
    const token = makeJwt({ exp: nowS + 10 })
    const doRefresh = vi.fn().mockResolvedValue(null)

    scheduleProactiveRefresh(token, doRefresh)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(doRefresh).toHaveBeenCalledTimes(1)

    // If it had rescheduled, this would eventually call doRefresh again —
    // advancing well past any plausible second interval proves it didn't.
    await vi.advanceTimersByTimeAsync(1_000_000)
    expect(doRefresh).toHaveBeenCalledTimes(1)
  })

  it('cancel() stops a pending refresh from firing', async () => {
    const nowS = Date.now() / 1000
    const token = makeJwt({ exp: nowS + 10 })
    const doRefresh = vi.fn().mockResolvedValue(null)

    const cancel = scheduleProactiveRefresh(token, doRefresh)
    cancel()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(doRefresh).not.toHaveBeenCalled()
  })

  it('does nothing for a token with no exp claim', async () => {
    const token = makeJwt({ user: { id: 'u1' } })
    const doRefresh = vi.fn()
    scheduleProactiveRefresh(token, doRefresh)
    await vi.advanceTimersByTimeAsync(1_000_000)
    expect(doRefresh).not.toHaveBeenCalled()
  })
})
