import { describe, expect, it } from 'vitest'
import { metaNameFromId, metaVersionFromId, serviceTitle } from './_shared'

describe('serviceTitle', () => {
  it('prefers a non-empty title over name', () => {
    expect(serviceTitle({ title: 'USB Camera', name: 'camera' })).toBe('USB Camera')
  })

  it('falls back to name when title is absent', () => {
    expect(serviceTitle({ name: 'camera' })).toBe('camera')
  })

  it('falls back to name when title is whitespace-only', () => {
    expect(serviceTitle({ title: '   ', name: 'camera' })).toBe('camera')
  })

  it('trims a title with surrounding whitespace', () => {
    expect(serviceTitle({ title: '  USB Camera  ', name: 'camera' })).toBe('USB Camera')
  })

  it('returns empty string when both are absent', () => {
    expect(serviceTitle({})).toBe('')
    expect(serviceTitle(null)).toBe('')
    expect(serviceTitle(undefined)).toBe('')
  })
})

describe('metaNameFromId', () => {
  it('splits name off a name@version id', () => {
    expect(metaNameFromId('opencv@4.8.0')).toBe('opencv')
  })

  it('returns the whole string when there is no @', () => {
    expect(metaNameFromId('opencv')).toBe('opencv')
  })

  it('returns undefined for nullish input', () => {
    expect(metaNameFromId(null)).toBeUndefined()
    expect(metaNameFromId(undefined)).toBeUndefined()
    expect(metaNameFromId('')).toBeUndefined()
  })

  it('splits on the first @ only', () => {
    expect(metaNameFromId('scoped@name@1.0.0')).toBe('scoped')
  })
})

describe('metaVersionFromId', () => {
  it('pulls the version off a name@version id', () => {
    expect(metaVersionFromId('opencv@4.8.0')).toBe('4.8.0')
  })

  it('returns undefined when there is no @ (legacy unversioned id)', () => {
    expect(metaVersionFromId('opencv')).toBeUndefined()
  })

  it('returns undefined for nullish input', () => {
    expect(metaVersionFromId(null)).toBeUndefined()
    expect(metaVersionFromId(undefined)).toBeUndefined()
  })

  it('keeps everything after the first @ (including further @s)', () => {
    expect(metaVersionFromId('scoped@name@1.0.0')).toBe('name@1.0.0')
  })
})
