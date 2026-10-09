import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  loadActiveId,
  loadConnections,
  originSeed,
  saveActiveId,
  saveConnections,
  type PersistedConnection,
} from './connectionsStorage'

beforeEach(() => {
  localStorage.clear()
})

describe('loadConnections', () => {
  it('returns [] when nothing is stored', () => {
    expect(loadConnections()).toEqual([])
  })

  it('round-trips a list written by saveConnections', () => {
    const list: PersistedConnection[] = [{ id: 'a', url: 'http://a', refreshToken: 'rt-a' }]
    saveConnections(list)
    expect(loadConnections()).toEqual(list)
  })

  it('preserves forward-compat extra fields on round-trip', () => {
    const list: PersistedConnection[] = [{ id: 'a', url: 'http://a', refreshToken: null, color: 'blue' }]
    saveConnections(list)
    expect(loadConnections()[0].color).toBe('blue')
  })

  it('returns [] for malformed JSON rather than throwing', () => {
    localStorage.setItem('rlx.connections', '{not valid json')
    expect(loadConnections()).toEqual([])
  })

  it('returns [] when the stored value is valid JSON but not an array', () => {
    localStorage.setItem('rlx.connections', JSON.stringify({ id: 'a' }))
    expect(loadConnections()).toEqual([])
  })

  it('drops entries that do not smell like a connection, keeping the rest', () => {
    localStorage.setItem(
      'rlx.connections',
      JSON.stringify([
        { id: 'a', url: 'http://a', refreshToken: null }, // valid
        { id: 'b' }, // missing url -> dropped
        { id: 'c', url: 'http://c', refreshToken: 123 }, // wrong type -> dropped
        'not even an object', // dropped
        null, // dropped
      ]),
    )
    const out = loadConnections()
    expect(out).toHaveLength(1)
    expect(out[0].id).toBe('a')
  })

  it('accepts a null refreshToken and a string refreshToken, rejects other types', () => {
    localStorage.setItem(
      'rlx.connections',
      JSON.stringify([
        { id: 'a', url: 'http://a', refreshToken: null },
        { id: 'b', url: 'http://b', refreshToken: 'rt' },
      ]),
    )
    expect(loadConnections()).toHaveLength(2)
  })
})

describe('saveConnections', () => {
  it('does not throw when localStorage.setItem throws (quota/private mode)', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })
    expect(() => saveConnections([{ id: 'a', url: 'http://a', refreshToken: null }])).not.toThrow()
    spy.mockRestore()
  })
})

describe('active id', () => {
  it('is null when nothing is stored', () => {
    expect(loadActiveId()).toBeNull()
  })

  it('round-trips through saveActiveId', () => {
    saveActiveId('conn-1')
    expect(loadActiveId()).toBe('conn-1')
  })

  it('removes the key when saved with null', () => {
    saveActiveId('conn-1')
    saveActiveId(null)
    expect(loadActiveId()).toBeNull()
  })

  it('does not throw when localStorage throws', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('nope')
    })
    expect(() => saveActiveId('conn-1')).not.toThrow()
    spy.mockRestore()
  })
})

describe('originSeed', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('returns a connection entry for the current page origin', () => {
    const seed = originSeed()
    expect(seed).not.toBeNull()
    expect(seed?.id).toBe(location.origin)
    expect(seed?.url).toBe(location.origin)
    expect(seed?.refreshToken).toBeNull()
  })
})
