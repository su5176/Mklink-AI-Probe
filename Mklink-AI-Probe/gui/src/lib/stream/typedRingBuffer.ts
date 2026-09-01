export interface VisibleRangeDestination {
  readonly times: Float64Array
  readonly values: Float32Array
}

export interface RingBufferSnapshot {
  readonly times: number[]
  readonly channels: number[][]
}

export interface TypedRingBufferSnapshot {
  readonly times: Float64Array
  readonly values: Float32Array
}

export interface NumericEnvelopeSelection {
  readonly logicalIndices: Uint32Array
  readonly channelOffsets: Uint32Array
  readonly pointCount: number
  readonly candidateSampleCount: number
}

/**
 * Fixed-capacity, sample-major storage.
 *
 * `values[slot * channelCount + channel]` stores one channel value for a
 * sample. The backing arrays are allocated exactly once and oldest samples
 * are overwritten after the buffer fills.
 */
export class TypedRingBuffer {
  readonly timestamps: Float64Array
  readonly values: Float32Array
  readonly capacity: number
  readonly channelCount: number

  private startSlot = 0
  private sampleCount = 0

  constructor(capacity: number, channelCount: number) {
    if (!Number.isInteger(capacity) || capacity <= 0) {
      throw new RangeError('capacity must be a positive integer')
    }
    if (!Number.isInteger(channelCount) || channelCount <= 0) {
      throw new RangeError('channelCount must be a positive integer')
    }
    this.capacity = capacity
    this.channelCount = channelCount
    this.timestamps = new Float64Array(capacity)
    this.values = new Float32Array(capacity * channelCount)
  }

  get length(): number {
    return this.sampleCount
  }

  timeAt(logicalIndex: number): number {
    if (!Number.isInteger(logicalIndex) || logicalIndex < 0 || logicalIndex >= this.sampleCount) {
      return Number.NaN
    }
    return this.timestamps[(this.startSlot + logicalIndex) % this.capacity]
  }

  valueAt(logicalIndex: number, channel: number): number {
    if (
      !Number.isInteger(logicalIndex) || logicalIndex < 0 || logicalIndex >= this.sampleCount
      || !Number.isInteger(channel) || channel < 0 || channel >= this.channelCount
    ) {
      return Number.NaN
    }
    const slot = (this.startSlot + logicalIndex) % this.capacity
    return this.values[slot * this.channelCount + channel]
  }

  append(timestamp: number, channels: Float32Array): void {
    if (channels.length !== this.channelCount) {
      throw new RangeError(`expected ${this.channelCount} channel values`)
    }
    const slot = this.sampleCount < this.capacity
      ? (this.startSlot + this.sampleCount) % this.capacity
      : this.startSlot
    if (this.sampleCount < this.capacity) {
      this.sampleCount += 1
    } else {
      this.startSlot = (this.startSlot + 1) % this.capacity
    }
    this.timestamps[slot] = timestamp
    this.values.set(channels, slot * this.channelCount)
  }

  /** Append sample-major values: sample 0 channels, then sample 1 channels. */
  appendBatch(timestamps: Float64Array, sampleMajorValues: Float32Array): void {
    if (sampleMajorValues.length !== timestamps.length * this.channelCount) {
      throw new RangeError('sample-major values must contain channelCount values per timestamp')
    }
    for (let sample = 0; sample < timestamps.length; sample += 1) {
      const offset = sample * this.channelCount
      this.append(
        timestamps[sample],
        sampleMajorValues.subarray(offset, offset + this.channelCount),
      )
    }
  }

  visibleRangeLength(start: number, end: number): number {
    let count = 0
    for (let logical = 0; logical < this.sampleCount; logical += 1) {
      const slot = (this.startSlot + logical) % this.capacity
      const timestamp = this.timestamps[slot]
      if (timestamp >= start && timestamp <= end) count += 1
    }
    return count
  }

  copyVisibleRange(
    start: number,
    end: number,
    destination: VisibleRangeDestination,
  ): number {
    let count = 0
    for (let logical = 0; logical < this.sampleCount; logical += 1) {
      const slot = (this.startSlot + logical) % this.capacity
      const timestamp = this.timestamps[slot]
      if (timestamp < start || timestamp > end) continue
      if (
        count >= destination.times.length
        || (count + 1) * this.channelCount > destination.values.length
      ) {
        throw new RangeError('destination is too small for visible range')
      }
      destination.times[count] = timestamp
      const sourceOffset = slot * this.channelCount
      destination.values.set(
        this.values.subarray(sourceOffset, sourceOffset + this.channelCount),
        count * this.channelCount,
      )
      count += 1
    }
    return count
  }

  private lowerBound(target: number): number {
    let low = 0
    let high = this.sampleCount
    while (low < high) {
      const middle = (low + high) >>> 1
      if (this.timeAt(middle) < target) low = middle + 1
      else high = middle
    }
    return low
  }

  private upperBound(target: number): number {
    let low = 0
    let high = this.sampleCount
    while (low < high) {
      const middle = (low + high) >>> 1
      if (this.timeAt(middle) <= target) low = middle + 1
      else high = middle
    }
    return low
  }

  selectMinMaxEnvelope(start: number, end: number, pixelWidth: number): NumericEnvelopeSelection {
    if (
      !Number.isFinite(start) || !Number.isFinite(end) || end < start
      || !Number.isSafeInteger(pixelWidth) || pixelWidth <= 0
    ) {
      throw new RangeError('visible range or pixel width is invalid')
    }
    const first = this.lowerBound(start)
    const afterLast = this.upperBound(end)
    const candidateSampleCount = afterLast - first
    const bucketSlots = pixelWidth * this.channelCount
    const minIndices = new Int32Array(bucketSlots)
    const maxIndices = new Int32Array(bucketSlots)
    const minValues = new Float32Array(bucketSlots)
    const maxValues = new Float32Array(bucketSlots)
    minIndices.fill(-1)
    maxIndices.fill(-1)
    minValues.fill(Number.POSITIVE_INFINITY)
    maxValues.fill(Number.NEGATIVE_INFINITY)
    const span = end > start ? end - start : 1

    for (let logical = first; logical < afterLast; logical += 1) {
      const relative = Math.max(0, Math.min(span, this.timeAt(logical) - start))
      const bucket = Math.min(pixelWidth - 1, Math.floor(relative / span * pixelWidth))
      for (let channel = 0; channel < this.channelCount; channel += 1) {
        const bucketIndex = channel * pixelWidth + bucket
        const value = this.valueAt(logical, channel)
        if (minIndices[bucketIndex] < 0 || value < minValues[bucketIndex]) {
          minIndices[bucketIndex] = logical
          minValues[bucketIndex] = value
        }
        if (maxIndices[bucketIndex] < 0 || value > maxValues[bucketIndex]) {
          maxIndices[bucketIndex] = logical
          maxValues[bucketIndex] = value
        }
      }
    }

    const logicalIndices = new Uint32Array(pixelWidth * this.channelCount * 2)
    const channelOffsets = new Uint32Array(this.channelCount + 1)
    let pointCount = 0
    for (let channel = 0; channel < this.channelCount; channel += 1) {
      channelOffsets[channel] = pointCount
      for (let pixel = 0; pixel < pixelWidth; pixel += 1) {
        const bucketIndex = channel * pixelWidth + pixel
        const minimum = minIndices[bucketIndex]
        const maximum = maxIndices[bucketIndex]
        if (minimum < 0) continue
        if (minimum <= maximum) {
          logicalIndices[pointCount++] = minimum
          if (maximum !== minimum) logicalIndices[pointCount++] = maximum
        } else {
          logicalIndices[pointCount++] = maximum
          logicalIndices[pointCount++] = minimum
        }
      }
    }
    channelOffsets[this.channelCount] = pointCount
    return { logicalIndices, channelOffsets, pointCount, candidateSampleCount }
  }

  /** Test/debug helper. Rendering code must use copyVisibleRange instead. */
  snapshot(): RingBufferSnapshot {
    const times = new Array<number>(this.sampleCount)
    const channels = Array.from(
      { length: this.channelCount },
      () => new Array<number>(this.sampleCount),
    )
    for (let logical = 0; logical < this.sampleCount; logical += 1) {
      const slot = (this.startSlot + logical) % this.capacity
      times[logical] = this.timestamps[slot]
      for (let channel = 0; channel < this.channelCount; channel += 1) {
        channels[channel][logical] = this.values[slot * this.channelCount + channel]
      }
    }
    return { times, channels }
  }

  /** Copy retained history in logical order for an explicit export request. */
  copyAll(): TypedRingBufferSnapshot {
    const times = new Float64Array(this.sampleCount)
    const values = new Float32Array(this.sampleCount * this.channelCount)
    for (let logical = 0; logical < this.sampleCount; logical += 1) {
      const slot = (this.startSlot + logical) % this.capacity
      times[logical] = this.timestamps[slot]
      const sourceOffset = slot * this.channelCount
      values.set(
        this.values.subarray(sourceOffset, sourceOffset + this.channelCount),
        logical * this.channelCount,
      )
    }
    return { times, values }
  }

  reset(): void {
    this.startSlot = 0
    this.sampleCount = 0
  }
}
