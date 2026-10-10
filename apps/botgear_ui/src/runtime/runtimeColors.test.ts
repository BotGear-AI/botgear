import { describe, expect, it } from 'vitest'
import { runtimeFaint, runtimeMuted, runtimePrimary } from './runtimeColors'

// All three are pure functions of `id` (same djb2-ish hash → hue), so the
// contract to prove is: deterministic, a real hsl(...) string, distinct
// ids usually land on distinct hues, and each function's own fixed
// saturation/lightness band.

describe('runtimePrimary / runtimeMuted / runtimeFaint', () => {
  it('are deterministic — same id always produces the same color', () => {
    expect(runtimePrimary('worke')).toBe(runtimePrimary('worke'))
    expect(runtimeMuted('worke')).toBe(runtimeMuted('worke'))
    expect(runtimeFaint('worke')).toBe(runtimeFaint('worke'))
  })

  it('return a well-formed hsl() string with the documented bands', () => {
    expect(runtimePrimary('worke')).toMatch(/^hsl\(\d+, 65%, 55%\)$/)
    expect(runtimeMuted('worke')).toMatch(/^hsl\(\d+, 35%, 28%\)$/)
    expect(runtimeFaint('worke')).toMatch(/^hsl\(\d+, 30%, 20%\)$/)
  })

  it('use the same hue across all three variants for one id', () => {
    const hueOf = (css: string) => css.match(/^hsl\((\d+),/)?.[1]
    expect(hueOf(runtimeMuted('cerebellum'))).toBe(hueOf(runtimePrimary('cerebellum')))
    expect(hueOf(runtimeFaint('cerebellum'))).toBe(hueOf(runtimePrimary('cerebellum')))
  })

  it('produce a hue within [0, 360)', () => {
    for (const id of ['', 'a', 'worke', 'cerebellum', 'axon-botgear-k-1']) {
      const hue = Number(runtimePrimary(id).match(/^hsl\((\d+),/)?.[1])
      expect(hue).toBeGreaterThanOrEqual(0)
      expect(hue).toBeLessThan(360)
    }
  })

  it('usually assigns different runtimes different hues', () => {
    const ids = ['worke', 'cerebellum', 'axon', 'funny-droid', 'witty-gizmo']
    const hues = new Set(ids.map((id) => runtimePrimary(id)))
    // Not a uniqueness guarantee (it's a hash into 360 buckets) — just
    // confirms the function isn't secretly constant.
    expect(hues.size).toBeGreaterThan(1)
  })
})
