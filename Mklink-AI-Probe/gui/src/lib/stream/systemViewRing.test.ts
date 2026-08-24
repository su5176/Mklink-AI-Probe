import { describe, expect, it } from 'vitest'
import { SystemViewIntervalRing } from './systemViewRing'

describe('SystemViewIntervalRing visible-range lookup', () => {
  it('starts at the first interval whose end overlaps the requested range', () => {
    const ring = new SystemViewIntervalRing(8)
    ring.append(1, 0n, 10n)
    ring.append(2, 20n, 30n)
    ring.append(3, 40n, 50n)

    expect(ring.firstOverlappingIndex(0n)).toBe(0)
    expect(ring.firstOverlappingIndex(15n)).toBe(1)
    expect(ring.firstOverlappingIndex(35n)).toBe(2)
    expect(ring.firstOverlappingIndex(60n)).toBe(3)
  })

  it('keeps binary lookup correct after the ring wraps', () => {
    const ring = new SystemViewIntervalRing(3)
    ring.append(1, 0n, 10n)
    ring.append(2, 20n, 30n)
    ring.append(3, 40n, 50n)
    ring.append(4, 60n, 70n)

    expect(ring.length).toBe(3)
    expect(ring.startTickAt(0)).toBe(20n)
    expect(ring.firstOverlappingIndex(15n)).toBe(0)
    expect(ring.firstOverlappingIndex(55n)).toBe(2)
  })
})
