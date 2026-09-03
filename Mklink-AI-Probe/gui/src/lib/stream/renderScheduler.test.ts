import { describe, expect, it, vi } from 'vitest'
import {
  AdaptiveFrameRateController,
  RenderScheduler,
  type RenderInvalidation,
} from './renderScheduler'

class FakeAnimationClock {
  now = 0
  hidden = false
  private nextId = 1
  private frames = new Map<number, FrameRequestCallback>()
  private visibilityListeners = new Set<() => void>()

  readonly requestAnimationFrame = (callback: FrameRequestCallback): number => {
    const id = this.nextId++
    this.frames.set(id, callback)
    return id
  }

  readonly cancelAnimationFrame = (id: number): void => {
    this.frames.delete(id)
  }

  readonly addVisibilityListener = (listener: () => void): void => {
    this.visibilityListeners.add(listener)
  }

  readonly removeVisibilityListener = (listener: () => void): void => {
    this.visibilityListeners.delete(listener)
  }

  step(milliseconds: number): void {
    this.now += milliseconds
    const pending = [...this.frames.values()]
    this.frames.clear()
    for (const callback of pending) callback(this.now)
  }

  setHidden(hidden: boolean): void {
    this.hidden = hidden
    for (const listener of this.visibilityListeners) listener()
  }

  dependencies() {
    return {
      now: () => this.now,
      requestAnimationFrame: this.requestAnimationFrame,
      cancelAnimationFrame: this.cancelAnimationFrame,
      isDocumentHidden: () => this.hidden,
      addVisibilityListener: this.addVisibilityListener,
      removeVisibilityListener: this.removeVisibilityListener,
    }
  }

  get pendingFrames(): number {
    return this.frames.size
  }
}

describe('RenderScheduler', () => {
  it('coalesces 100 invalidations inside one 30 FPS frame', () => {
    const clock = new FakeAnimationClock()
    const renders: ReadonlySet<RenderInvalidation>[] = []
    const scheduler = new RenderScheduler(reasons => renders.push(reasons), clock.dependencies())
    scheduler.start()

    const reasons: RenderInvalidation[] = ['data', 'hover', 'zoom', 'resize']
    for (let index = 0; index < 100; index += 1) {
      scheduler.invalidate(reasons[index % reasons.length])
    }

    expect(clock.pendingFrames).toBe(1)
    clock.step(32)
    expect(renders).toHaveLength(1)
    expect([...renders[0]].sort()).toEqual(['data', 'hover', 'resize', 'zoom'])
  })

  it('limits later renders to 30 FPS', () => {
    const clock = new FakeAnimationClock()
    const render = vi.fn()
    const scheduler = new RenderScheduler(render, clock.dependencies())
    scheduler.start()
    scheduler.invalidate('data')
    clock.step(1)
    scheduler.invalidate('data')
    clock.step(32)
    expect(render).toHaveBeenCalledTimes(1)
    clock.step(2)
    expect(render).toHaveBeenCalledTimes(2)
  })

  it('accepts a faster frame rate for dense live timelines', () => {
    const clock = new FakeAnimationClock()
    const render = vi.fn()
    const scheduler = new RenderScheduler(render, clock.dependencies(), () => {}, { frameRate: 60 })
    scheduler.start()
    scheduler.invalidate('data')
    clock.step(1)
    scheduler.invalidate('data')
    clock.step(16)
    expect(render).toHaveBeenCalledTimes(1)
    clock.step(1)
    expect(render).toHaveBeenCalledTimes(2)
  })

  it('renders on the second 60 Hz frame when its timestamp is just before 33.333 ms', () => {
    const clock = new FakeAnimationClock()
    const render = vi.fn()
    const scheduler = new RenderScheduler(
      render,
      clock.dependencies(),
      () => {},
      { frameRate: 30, continuous: true },
    )
    scheduler.start()

    clock.step(16.6)
    clock.step(16.6)
    clock.step(16.6)

    expect(render).toHaveBeenCalledTimes(2)
  })

  it('rejects invalid frame rates', () => {
    const clock = new FakeAnimationClock()
    expect(() => new RenderScheduler(() => {}, clock.dependencies(), () => {}, { frameRate: 0 }))
      .toThrow('frameRate must be a positive finite number')
  })

  it('changes the active frame rate without rebuilding the scheduler', () => {
    const clock = new FakeAnimationClock()
    const render = vi.fn()
    const scheduler = new RenderScheduler(
      render,
      clock.dependencies(),
      () => {},
      { frameRate: 60, continuous: true },
    )
    scheduler.start()
    clock.step(1)
    scheduler.setFrameRate(30)
    clock.step(17)
    expect(render).toHaveBeenCalledTimes(1)
    clock.step(16)
    expect(render).toHaveBeenCalledTimes(2)
    expect(() => scheduler.setFrameRate(0)).toThrow('frameRate must be a positive finite number')
  })

  it('keeps a continuous scheduler alive and emits empty frames', () => {
    const clock = new FakeAnimationClock()
    const renders: ReadonlySet<RenderInvalidation>[] = []
    const scheduler = new RenderScheduler(
      reasons => renders.push(reasons),
      clock.dependencies(),
      () => {},
      { frameRate: 60, continuous: true },
    )
    scheduler.start()
    expect(clock.pendingFrames).toBe(1)
    clock.step(17)
    expect(renders).toHaveLength(1)
    expect(renders[0].size).toBe(0)
    expect(clock.pendingFrames).toBe(1)
    scheduler.stop()
    expect(clock.pendingFrames).toBe(0)
  })

  it('pauses rendering while hidden and renders once after visibility returns', () => {
    const clock = new FakeAnimationClock()
    const render = vi.fn()
    const collect = vi.fn()
    const scheduler = new RenderScheduler(render, clock.dependencies(), collect)
    scheduler.start()
    clock.setHidden(true)
    scheduler.invalidate('data')
    scheduler.invalidate('zoom')
    scheduler.recordCollection(25)
    clock.step(100)

    expect(render).not.toHaveBeenCalled()
    expect(collect).toHaveBeenCalledWith(25)
    expect(clock.pendingFrames).toBe(0)

    clock.setHidden(false)
    expect(clock.pendingFrames).toBe(1)
    clock.step(1)
    expect(render).toHaveBeenCalledTimes(1)
  })

  it('keeps start, stop, and dispose idempotent without stale callbacks', () => {
    const clock = new FakeAnimationClock()
    const render = vi.fn()
    const scheduler = new RenderScheduler(render, clock.dependencies())

    scheduler.start()
    scheduler.start()
    scheduler.invalidate('data')
    expect(clock.pendingFrames).toBe(1)
    scheduler.stop()
    scheduler.stop()
    expect(clock.pendingFrames).toBe(0)
    clock.step(100)
    expect(render).not.toHaveBeenCalled()

    scheduler.start()
    expect(clock.pendingFrames).toBe(1)
    scheduler.dispose()
    scheduler.dispose()
    expect(clock.pendingFrames).toBe(0)
    scheduler.invalidate('zoom')
    scheduler.start()
    clock.step(100)
    expect(render).not.toHaveBeenCalled()
  })
})

describe('AdaptiveFrameRateController', () => {
  it('keeps 60 FPS for sparse visible data', () => {
    const controller = new AdaptiveFrameRateController()
    expect(controller.observe({ now: 0, renderCostMs: 1, visibleItems: 200, pixelWidth: 1_000 }))
      .toBe(60)
  })

  it('drops dense subpixel data to 30 FPS and restores 60 FPS after a stable sparse view', () => {
    const controller = new AdaptiveFrameRateController()
    expect(controller.observe({ now: 0, renderCostMs: 1, visibleItems: 1_200, pixelWidth: 1_000 }))
      .toBe(30)
    expect(controller.observe({ now: 1_000, renderCostMs: 1, visibleItems: 300, pixelWidth: 1_000 }))
      .toBe(30)
    expect(controller.observe({ now: 3_001, renderCostMs: 1, visibleItems: 300, pixelWidth: 1_000 }))
      .toBe(60)
  })

  it('drops to 20 FPS when painting remains too expensive', () => {
    const controller = new AdaptiveFrameRateController()
    expect(controller.observe({ now: 0, renderCostMs: 6, visibleItems: 200, pixelWidth: 1_000 }))
      .toBe(30)
    expect(controller.observe({ now: 20, renderCostMs: 40, visibleItems: 200, pixelWidth: 1_000 }))
      .toBe(20)
  })

  it('resets adaptive history between trace sessions', () => {
    const controller = new AdaptiveFrameRateController()
    expect(controller.observe({ now: 0, renderCostMs: 6, visibleItems: 200, pixelWidth: 1_000 }))
      .toBe(30)
    controller.reset()
    expect(controller.observe({ now: 1, renderCostMs: 1, visibleItems: 200, pixelWidth: 1_000 }))
      .toBe(60)
    expect(() => controller.reset(0)).toThrow('initialRate must be a positive finite number')
  })
})
