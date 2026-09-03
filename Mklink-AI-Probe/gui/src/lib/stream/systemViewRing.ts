export interface IntervalEnvelopeSelection {
  readonly logicalIndices: Int32Array
  readonly count: number
  readonly candidateCount: number
}

abstract class TickRingBase {
  readonly capacity: number
  protected head = 0
  protected count = 0

  constructor(capacity: number) {
    if (!Number.isSafeInteger(capacity) || capacity <= 0) {
      throw new RangeError('capacity must be a positive safe integer')
    }
    this.capacity = capacity
  }

  get length(): number { return this.count }

  protected physical(logicalIndex: number): number {
    if (!Number.isInteger(logicalIndex) || logicalIndex < 0 || logicalIndex >= this.count) {
      throw new RangeError('logical ring index is out of range')
    }
    return (this.head + logicalIndex) % this.capacity
  }

  protected appendIndex(): number {
    if (this.count < this.capacity) {
      const index = (this.head + this.count) % this.capacity
      this.count += 1
      return index
    }
    const index = this.head
    this.head = (this.head + 1) % this.capacity
    return index
  }

  clear(): void {
    this.head = 0
    this.count = 0
  }
}

/** Fixed-capacity object slots with exact tick keys and O(1) overwrite. */
export class SystemViewEventRing<T> extends TickRingBase {
  private readonly events: Array<T | undefined>
  private readonly ticks: BigUint64Array

  constructor(capacity: number) {
    super(capacity)
    this.events = new Array<T | undefined>(capacity)
    this.ticks = new BigUint64Array(capacity)
  }

  append(event: T, tick: bigint): void {
    const index = this.appendIndex()
    this.events[index] = event
    this.ticks[index] = tick
  }

  eventAt(logicalIndex: number): T {
    const event = this.events[this.physical(logicalIndex)]
    if (event === undefined) throw new RangeError('event slot is empty')
    return event
  }

  tickAt(logicalIndex: number): bigint {
    return this.ticks[this.physical(logicalIndex)]
  }

  lowerBound(target: bigint): number {
    let low = 0
    let high = this.count
    while (low < high) {
      const middle = (low + high) >>> 1
      if (this.tickAt(middle) < target) low = middle + 1
      else high = middle
    }
    return low
  }

  upperBound(target: bigint): number {
    let low = 0
    let high = this.count
    while (low < high) {
      const middle = (low + high) >>> 1
      if (this.tickAt(middle) <= target) low = middle + 1
      else high = middle
    }
    return low
  }

  override clear(): void {
    super.clear()
    this.events.fill(undefined)
  }
}

/** Fixed-capacity struct-of-arrays task intervals with exact 64-bit ticks. */
export class SystemViewIntervalRing extends TickRingBase {
  private readonly taskIds: Uint32Array
  private readonly contextTypes: Uint8Array
  private readonly startTicks: BigUint64Array
  private readonly endTicks: BigUint64Array

  constructor(capacity: number) {
    super(capacity)
    this.taskIds = new Uint32Array(capacity)
    this.contextTypes = new Uint8Array(capacity)
    this.startTicks = new BigUint64Array(capacity)
    this.endTicks = new BigUint64Array(capacity)
  }

  append(taskId: number, startTick: bigint, endTick: bigint, contextType = 1): void {
    const index = this.appendIndex()
    this.taskIds[index] = taskId
    this.contextTypes[index] = contextType
    this.startTicks[index] = startTick
    this.endTicks[index] = endTick
  }

  taskIdAt(logicalIndex: number): number {
    return this.taskIds[this.physical(logicalIndex)]
  }

  contextTypeAt(logicalIndex: number): number {
    return this.contextTypes[this.physical(logicalIndex)]
  }

  startTickAt(logicalIndex: number): bigint {
    return this.startTicks[this.physical(logicalIndex)]
  }

  endTickAt(logicalIndex: number): bigint {
    return this.endTicks[this.physical(logicalIndex)]
  }

  firstOverlappingIndex(target: bigint): number {
    let low = 0
    let high = this.count
    while (low < high) {
      const middle = (low + high) >>> 1
      if (this.endTickAt(middle) < target) low = middle + 1
      else high = middle
    }
    return low
  }

  selectEnvelope(start: bigint, end: bigint, pixelWidth: number): IntervalEnvelopeSelection {
    const firstByPixel = new Int32Array(pixelWidth)
    const lastByPixel = new Int32Array(pixelWidth)
    firstByPixel.fill(-1)
    lastByPixel.fill(-1)
    const first = this.firstOverlappingIndex(start)
    const span = end > start ? end - start : 1n
    let candidateCount = 0

    for (let logical = first; logical < this.count; logical++) {
      const intervalStart = this.startTickAt(logical)
      if (intervalStart > end) break
      const intervalEnd = this.endTickAt(logical)
      if (intervalEnd < start) continue
      candidateCount += 1
      const middle = (intervalStart + intervalEnd) / 2n
      const relative = middle <= start ? 0n : middle >= end ? span : middle - start
      const bucket = Math.min(
        pixelWidth - 1,
        Number(relative * BigInt(pixelWidth) / span),
      )
      if (firstByPixel[bucket] < 0) firstByPixel[bucket] = logical
      lastByPixel[bucket] = logical
    }

    const selected = new Int32Array(pixelWidth * 2)
    let selectedCount = 0
    for (let pixel = 0; pixel < pixelWidth; pixel++) {
      const firstLogical = firstByPixel[pixel]
      if (firstLogical < 0) continue
      selected[selectedCount++] = firstLogical
      const lastLogical = lastByPixel[pixel]
      if (lastLogical !== firstLogical) selected[selectedCount++] = lastLogical
    }
    return { logicalIndices: selected, count: selectedCount, candidateCount }
  }
}

export function safeTickDifference(tick: bigint, origin: bigint): number {
  const difference = tick - origin
  const limit = BigInt(Number.MAX_SAFE_INTEGER)
  if (difference < -limit || difference > limit) {
    throw new RangeError('tick difference exceeds Number safe integer range')
  }
  return Number(difference)
}
