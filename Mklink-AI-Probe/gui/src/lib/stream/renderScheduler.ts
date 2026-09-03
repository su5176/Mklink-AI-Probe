export type RenderInvalidation = 'data' | 'hover' | 'zoom' | 'resize'

export interface RenderSchedulerDependencies {
  readonly now: () => number
  readonly requestAnimationFrame: (callback: FrameRequestCallback) => number
  readonly cancelAnimationFrame: (id: number) => void
  readonly isDocumentHidden: () => boolean
  readonly addVisibilityListener: (listener: () => void) => void
  readonly removeVisibilityListener: (listener: () => void) => void
}

export interface RenderSchedulerOptions {
  readonly frameRate?: number
  /** Keep an animation frame alive even when no invalidation is pending. */
  readonly continuous?: boolean
}

export interface AdaptiveFrameRateSample {
  readonly now: number
  readonly renderCostMs: number
  readonly visibleItems: number
  readonly pixelWidth: number
}

// Browser rAF timestamps can land a few tenths of a millisecond before the
// nominal 30 FPS boundary (for example 33.2 ms instead of 33.333 ms). Without
// a small tolerance the scheduler waits for a third 60 Hz frame and produces
// a visible 33/50 ms cadence.
const FRAME_BOUNDARY_TOLERANCE_MS = 0.5
const FRAME_RATE_UPSHIFT_DELAY_MS = 2_000

/**
 * Chooses a live plot rate from visible data density and measured paint cost.
 * Dense subpixel data gains no detail at 60 FPS, while sparse/zoomed views do.
 * Downshifts are immediate; upshifts wait so the rate does not oscillate.
 */
export class AdaptiveFrameRateController {
  private currentRate = 60
  private renderCostEwma: number | null = null
  private pendingUpshift: number | null = null
  private pendingUpshiftAt = 0

  reset(initialRate = 60): void {
    if (!Number.isFinite(initialRate) || initialRate <= 0) {
      throw new RangeError('initialRate must be a positive finite number')
    }
    this.currentRate = initialRate
    this.renderCostEwma = null
    this.pendingUpshift = null
    this.pendingUpshiftAt = 0
  }

  observe(sample: AdaptiveFrameRateSample): number {
    const cost = Number.isFinite(sample.renderCostMs) ? Math.max(0, sample.renderCostMs) : 0
    this.renderCostEwma = this.renderCostEwma === null
      ? cost
      : this.renderCostEwma * 0.85 + cost * 0.15
    const pixelWidth = Number.isFinite(sample.pixelWidth) && sample.pixelWidth > 0
      ? sample.pixelWidth
      : 1
    const visibleItems = Number.isFinite(sample.visibleItems)
      ? Math.max(0, sample.visibleItems)
      : 0
    const density = visibleItems / pixelWidth
    const desired = this.desiredRate(density, this.renderCostEwma)

    if (desired < this.currentRate) {
      this.currentRate = desired
      this.pendingUpshift = null
      return this.currentRate
    }
    if (desired === this.currentRate) {
      this.pendingUpshift = null
      return this.currentRate
    }
    if (this.pendingUpshift !== desired) {
      this.pendingUpshift = desired
      this.pendingUpshiftAt = sample.now
      return this.currentRate
    }
    if (sample.now - this.pendingUpshiftAt >= FRAME_RATE_UPSHIFT_DELAY_MS) {
      this.currentRate = desired
      this.pendingUpshift = null
    }
    return this.currentRate
  }

  private desiredRate(density: number, renderCostMs: number): number {
    if (this.currentRate === 60) {
      if (renderCostMs >= 10) return 20
      if (density >= 1 || renderCostMs >= 5) return 30
      return 60
    }
    if (this.currentRate === 30) {
      if (renderCostMs >= 10) return 20
      if (density <= 0.65 && renderCostMs <= 4) return 60
      return 30
    }
    if (renderCostMs <= 7) return 30
    return 20
  }
}

function browserDependencies(): RenderSchedulerDependencies {
  return {
    now: () => performance.now(),
    requestAnimationFrame: callback => requestAnimationFrame(callback),
    cancelAnimationFrame: id => cancelAnimationFrame(id),
    isDocumentHidden: () => document.hidden,
    addVisibilityListener: listener => document.addEventListener('visibilitychange', listener),
    removeVisibilityListener: listener => document.removeEventListener('visibilitychange', listener),
  }
}

/** Coalesces plot invalidations into a configurable render loop. */
export class RenderScheduler {
  private readonly render: (reasons: ReadonlySet<RenderInvalidation>) => void
  private readonly dependencies: RenderSchedulerDependencies
  private readonly collectionTelemetry: (collectedItems: number) => void
  private frameIntervalMs: number
  private readonly continuous: boolean
  private readonly dirty = new Set<RenderInvalidation>()
  private readonly visibilityListener = () => this.visibilityChanged()
  private frameId: number | null = null
  private lastRender = Number.NEGATIVE_INFINITY
  private generation = 0
  private running = false
  private disposed = false

  constructor(
    render: (reasons: ReadonlySet<RenderInvalidation>) => void,
    dependencies: RenderSchedulerDependencies = browserDependencies(),
    collectionTelemetry: (collectedItems: number) => void = () => {},
    options: RenderSchedulerOptions = {},
  ) {
    this.render = render
    this.dependencies = dependencies
    this.collectionTelemetry = collectionTelemetry
    const frameRate = options.frameRate ?? 30
    if (!Number.isFinite(frameRate) || frameRate <= 0) {
      throw new RangeError('frameRate must be a positive finite number')
    }
    this.frameIntervalMs = 1000 / frameRate
    this.continuous = options.continuous === true
    dependencies.addVisibilityListener(this.visibilityListener)
  }

  start(): void {
    if (this.running || this.disposed) return
    this.running = true
    this.generation += 1
    this.scheduleIfNeeded()
  }

  stop(): void {
    if (!this.running) return
    this.running = false
    this.generation += 1
    if (this.frameId !== null) {
      this.dependencies.cancelAnimationFrame(this.frameId)
      this.frameId = null
    }
  }

  dispose(): void {
    if (this.disposed) return
    this.stop()
    this.disposed = true
    this.dirty.clear()
    this.dependencies.removeVisibilityListener(this.visibilityListener)
  }

  invalidate(reason: RenderInvalidation): void {
    if (this.disposed) return
    this.dirty.add(reason)
    this.scheduleIfNeeded()
  }

  setFrameRate(frameRate: number): void {
    if (!Number.isFinite(frameRate) || frameRate <= 0) {
      throw new RangeError('frameRate must be a positive finite number')
    }
    this.frameIntervalMs = 1000 / frameRate
  }

  /** Acquisition accounting is immediate and remains active while hidden. */
  recordCollection(collectedItems: number): void {
    if (this.disposed) return
    this.collectionTelemetry(collectedItems)
  }

  private scheduleIfNeeded(): void {
    if (
      !this.running
      || this.disposed
      || this.frameId !== null
      || (!this.continuous && this.dirty.size === 0)
      || this.dependencies.isDocumentHidden()
    ) return
    const generation = this.generation
    this.frameId = this.dependencies.requestAnimationFrame(() => this.onFrame(generation))
  }

  private onFrame(generation: number): void {
    if (generation !== this.generation || !this.running || this.disposed) return
    this.frameId = null
    if (this.dependencies.isDocumentHidden()) return
    const now = this.dependencies.now()
    if (now - this.lastRender >= this.frameIntervalMs - FRAME_BOUNDARY_TOLERANCE_MS) {
      const reasons = new Set(this.dirty)
      this.dirty.clear()
      this.lastRender = now
      this.render(reasons)
    }
    this.scheduleIfNeeded()
  }

  private visibilityChanged(): void {
    if (!this.dependencies.isDocumentHidden()) this.scheduleIfNeeded()
  }
}
