import { describe, expect, it, vi } from 'vitest'
import {
  RTT_TERMINAL_UTF8,
  SERIAL_RX_BYTES,
  SERIAL_TX_BYTES,
  StreamType,
} from '../lib/stream/protocol'
import {
  StreamDecoder,
  type WorkerOutput,
} from './streamDecoder.worker'

function frame(
  sequence: bigint,
  itemCount: number,
  payload: Uint8Array,
  streamType = StreamType.WAVEFORM,
  timestampNs = 1_000_000_000n,
  flags = 0,
): ArrayBuffer {
  const buffer = new ArrayBuffer(36 + payload.byteLength)
  const bytes = new Uint8Array(buffer)
  bytes.set([0x4d, 0x4b, 0x53, 0x54])
  const view = new DataView(buffer)
  view.setUint8(4, 1)
  view.setUint8(5, streamType)
  view.setUint8(6, flags)
  view.setUint8(7, 36)
  view.setUint32(8, streamType, true)
  view.setBigUint64(12, sequence, true)
  view.setBigUint64(20, timestampNs, true)
  view.setUint32(28, itemCount, true)
  view.setUint32(32, payload.byteLength, true)
  bytes.set(payload, 36)
  return buffer
}

function floats(...values: number[]): Uint8Array {
  return new Uint8Array(Float32Array.from(values).buffer)
}

function rttLines(...lines: Array<{ timestampNs: bigint; level: number; text: string }>): Uint8Array {
  const encoder = new TextEncoder()
  const encoded = lines.map(line => ({ ...line, bytes: encoder.encode(line.text) }))
  const payload = new Uint8Array(encoded.reduce((size, line) => size + 13 + line.bytes.length, 0))
  const view = new DataView(payload.buffer)
  let offset = 0
  for (const line of encoded) {
    view.setBigUint64(offset, line.timestampNs, true)
    view.setUint8(offset + 8, line.level)
    view.setUint32(offset + 9, line.bytes.length, true)
    payload.set(line.bytes, offset + 13)
    offset += 13 + line.bytes.length
  }
  return payload
}

function systemViewRecords(...records: Array<{
  kind: number
  taskId: number
  ticks: bigint
  timeUs: number
  deltaUs?: number
  aux0?: number
  aux1?: number
  flags?: number
  reserved?: number
}>): Uint8Array {
  const bytes = new Uint8Array(records.length * 48)
  const view = new DataView(bytes.buffer)
  records.forEach((record, index) => {
    const offset = index * 48
    view.setUint8(offset, record.kind)
    view.setUint8(offset + 1, record.flags ?? 0x07)
    view.setUint16(offset + 2, record.reserved ?? 0, true)
    view.setUint32(offset + 4, record.taskId, true)
    view.setBigUint64(offset + 8, record.ticks, true)
    view.setFloat64(offset + 16, record.timeUs, true)
    view.setFloat64(offset + 24, record.deltaUs ?? 0, true)
    view.setFloat64(offset + 32, record.aux0 ?? 0, true)
    view.setFloat64(offset + 40, record.aux1 ?? 0, true)
  })
  return bytes
}

function systemViewTaskPairs(firstTask: number, pairCount: number, firstTick: bigint): Uint8Array {
  const bytes = new Uint8Array(pairCount * 2 * 48)
  const view = new DataView(bytes.buffer)
  for (let pair = 0; pair < pairCount; pair++) {
    for (let edge = 0; edge < 2; edge++) {
      const offset = (pair * 2 + edge) * 48
      const tick = firstTick + BigInt(pair * 2 + edge)
      view.setUint8(offset, edge === 0 ? 4 : 5)
      view.setUint8(offset + 1, 0x01)
      view.setUint32(offset + 4, firstTask + pair, true)
      view.setBigUint64(offset + 8, tick, true)
    }
  }
  return bytes
}

function setup() {
  const messages: WorkerOutput[] = []
  const transfers: Transferable[][] = []
  const decoder = new StreamDecoder((message, transfer = []) => {
    messages.push(message)
    transfers.push(transfer)
  })
  return { decoder, messages, transfers }
}

describe('StreamDecoder worker controller', () => {
  it('incrementally decodes serial RX in terminal mode without TX echo or log output', () => {
    const { decoder, messages } = setup()
    const encoded = new TextEncoder().encode('温度')
    decoder.handle({
      type: 'configure', capacity: 8, channelCount: 1, decoderMode: 'serial-terminal',
    })
    decoder.handle({
      type: 'frame', buffer: frame(
        1n, 2, encoded.slice(0, 2), StreamType.SERIAL, 10n, SERIAL_RX_BYTES,
      ), connectionGeneration: 1, frameTicket: 1,
    })
    decoder.handle({
      type: 'frame', buffer: frame(
        2n, encoded.length - 2, encoded.slice(2), StreamType.SERIAL, 20n, SERIAL_RX_BYTES,
      ), connectionGeneration: 1, frameTicket: 2,
    })
    decoder.handle({
      type: 'frame', buffer: frame(
        3n, 2, new TextEncoder().encode('TX'), StreamType.SERIAL, 30n, SERIAL_TX_BYTES,
      ), connectionGeneration: 1, frameTicket: 3,
    })

    expect(messages.filter(message => message.type === 'serial-terminal'))
      .toEqual([{ type: 'serial-terminal', sequence: 2n, text: '温度' }])
    expect(messages.some(message => message.type === 'serial-lines')).toBe(false)
    expect(messages.at(-1)).toMatchObject({
      type: 'telemetry', bufferedSamples: 8, acceptedFrames: 3,
    })
  })

  it('assembles bounded directional serial rows in log mode', () => {
    const { decoder, messages } = setup()
    const encoder = new TextEncoder()
    decoder.handle({
      type: 'configure', capacity: 1, channelCount: 1, decoderMode: 'serial-log',
    })
    decoder.handle({
      type: 'frame', buffer: frame(
        1n, 2, encoder.encode('OK'), StreamType.SERIAL, 10n, SERIAL_RX_BYTES,
      ), connectionGeneration: 1, frameTicket: 1,
    })
    decoder.handle({
      type: 'frame', buffer: frame(
        2n, 2, encoder.encode('\nX'), StreamType.SERIAL, 20n, SERIAL_RX_BYTES,
      ), connectionGeneration: 1, frameTicket: 2,
    })
    decoder.handle({
      type: 'frame', buffer: frame(
        3n, 2, Uint8Array.of(0xab, 0xcd), StreamType.SERIAL, 30n, SERIAL_TX_BYTES,
      ), connectionGeneration: 1, frameTicket: 3,
    })

    const batches = messages.filter(message => message.type === 'serial-lines')
    expect(batches).toEqual([
      {
        type: 'serial-lines', sequence: 2n,
        lines: [{ timestampNs: 10n, direction: 'RX', rawHex: '4F4B0A', ascii: 'OK\n' }],
      },
      {
        type: 'serial-lines', sequence: 3n,
        lines: [{ timestampNs: 30n, direction: 'TX', rawHex: 'ABCD', ascii: '��' }],
      },
    ])
    expect(messages.some(message => message.type === 'serial-terminal')).toBe(false)
    expect(messages.at(-1)).toMatchObject({
      type: 'telemetry', bufferedSamples: 1, acceptedFrames: 3,
    })
  })

  it('decodes RTT raw records with UTF-8 line metadata and preserves batch order', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 16, channelCount: 1 })
    decoder.handle({
      type: 'frame', buffer: frame(1n, 2, rttLines(
        { timestampNs: 10n, level: 0, text: 'hello' },
        { timestampNs: 20n, level: 1, text: '温度=25' },
      ), StreamType.RTT_RAW, 20n, 0x01),
      connectionGeneration: 1, frameTicket: 1,
    })
    expect(messages.find(message => message.type === 'rtt-lines')).toEqual({
      type: 'rtt-lines', sequence: 1n,
      lines: [
        { timestampNs: 10n, level: 'raw', text: 'hello' },
        { timestampNs: 20n, level: 'data', text: '温度=25' },
      ],
    })
  })

  it('decodes immediate RTT terminal chunks without line metadata', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 16, channelCount: 1 })
    decoder.handle({
      type: 'frame', buffer: frame(
        1n, 1, new TextEncoder().encode('\x1b[31mwarn\r'),
        StreamType.RTT_RAW, 20n, RTT_TERMINAL_UTF8,
      ),
      connectionGeneration: 1, frameTicket: 1,
    })
    expect(messages.find(message => message.type === 'rtt-terminal')).toEqual({
      type: 'rtt-terminal', sequence: 1n, text: '\x1b[31mwarn\r',
    })
  })

  it.each([
    ['unknown flags', 0x80, rttLines({ timestampNs: 1n, level: 0, text: 'x' })],
    ['invalid level', 0x01, rttLines({ timestampNs: 1n, level: 9, text: 'x' })],
    ['truncated record', 0x01, new Uint8Array(12)],
    ['invalid UTF-8', 0x01, Uint8Array.from([1,0,0,0,0,0,0,0,0,1,0,0,0,0xff])],
  ])('rejects malformed RTT %s atomically', (_name, flags, payload) => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 16, channelCount: 1 })
    decoder.handle({
      type: 'frame', buffer: frame(1n, 1, payload, StreamType.RTT_RAW, 1n, flags as number),
      connectionGeneration: 1, frameTicket: 1,
    })
    expect(messages.at(-1)).toMatchObject({ type: 'error', code: 'INVALID_FRAME' })
    decoder.handle({
      type: 'frame', buffer: frame(1n, 1, rttLines(
        { timestampNs: 2n, level: 0, text: 'ok' },
      ), StreamType.RTT_RAW, 2n, 0x01),
      connectionGeneration: 1, frameTicket: 2,
    })
    expect(messages.find(message => message.type === 'rtt-lines')).toMatchObject({
      type: 'rtt-lines', sequence: 1n,
    })
  })

  it('uses exact device sample times despite host jitter and preserves real gaps', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 16, channelCount: 1 })
    const metadata = new TextEncoder().encode(JSON.stringify({ version: 1, channels: [{ name: 'a' }] }))
    const send = (sequence: bigint, flags: number, payload: Uint8Array, count: number, host: bigint) => decoder.handle({
      type: 'frame', buffer: frame(sequence, count, payload, StreamType.SUPERWATCH, host, flags),
      connectionGeneration: 1, frameTicket: Number(sequence),
    })
    send(1n, 2, metadata, 0, 1n)
    const payload = (times: number[]) => {
      const bytes = new Uint8Array(times.length * 12)
      bytes.set(new Uint8Array(Float64Array.from(times).buffer))
      bytes.set(floats(...times), times.length * 8)
      return bytes
    }
    send(2n, 3, payload([0, 0.13]), 2, 20_000_000_000n)
    send(3n, 3, payload([0.26, 25]), 2, 20_050_000_000n)
    const batches = messages.filter(message => message.type === 'waveform-batch')
    expect(batches).toHaveLength(2)
    expect(batches.flatMap(batch => Array.from(new Float64Array(batch.times)))).toEqual([0, 0.13, 0.26, 25])
    // Reject invalid time order without poisoning the next valid batch.
    send(4n, 3, payload([25, 26]), 2, 21_000_000_000n)
    expect(messages.at(-1)).toMatchObject({ type: 'error', code: 'INVALID_FRAME' })
    send(4n, 3, payload([26, 27]), 2, 19_000_000_000n)
    expect(messages.filter(message => message.type === 'waveform-batch')).toHaveLength(3)
  })

  it('applies versioned SuperWatch metadata independently from sample batches', () => {
    const { decoder, messages, transfers } = setup()
    decoder.handle({ type: 'configure', capacity: 16, channelCount: 1 })
    const metadata = new TextEncoder().encode(JSON.stringify({
      version: 1, channels: [{ name: 'a', type: 'float' }, { name: 'b', type: 'uint32_t' }],
    }))
    decoder.handle({
      type: 'frame', buffer: frame(1n, 0, metadata, StreamType.SUPERWATCH, 1n, 0x02),
      connectionGeneration: 1, frameTicket: 1,
    })
    decoder.handle({
      type: 'frame', buffer: frame(2n, 2, floats(1, 2, 3, 4), StreamType.SUPERWATCH, 2n, 0x01),
      connectionGeneration: 1, frameTicket: 2,
    })
    expect(messages.find(message => message.type === 'superwatch-metadata')).toEqual({
      type: 'superwatch-metadata', version: 1,
      channels: [{ name: 'a', type: 'float' }, { name: 'b', type: 'uint32_t' }],
    })
    const batch = messages.find(message => message.type === 'waveform-batch')
    expect(batch).toMatchObject({ type: 'waveform-batch', itemCount: 2, channelCount: 2 })
    if (batch?.type !== 'waveform-batch') throw new Error('expected batch')
    expect(Array.from(new Float32Array(batch.values))).toEqual([1, 2, 3, 4])
    expect(transfers[messages.indexOf(batch)]).toEqual([batch.values, batch.times])
  })

  it('keeps full SuperWatch history in the Worker and emits only latest summaries normally', () => {
    const { decoder, messages, transfers } = setup()
    decoder.handle({
      type: 'configure', capacity: 3, channelCount: 1, waveformSummaryOnly: true,
    })
    const metadata = new TextEncoder().encode(JSON.stringify({
      version: 1, channels: [{ name: 'a', type: 'float' }],
    }))
    const send = (sequence: bigint, values: number[]) => decoder.handle({
      type: 'frame',
      buffer: frame(sequence, values.length, floats(...values), StreamType.SUPERWATCH, sequence * 1_000_000n, 0x01),
      connectionGeneration: 1,
      frameTicket: Number(sequence),
    })
    decoder.handle({
      type: 'frame', buffer: frame(1n, 0, metadata, StreamType.SUPERWATCH, 1n, 0x02),
      connectionGeneration: 1, frameTicket: 1,
    })
    send(2n, [1, 2])
    send(3n, [3, 4])

    expect(messages.filter(message => message.type === 'waveform-batch')).toHaveLength(0)
    const summaries = messages.filter(message => message.type === 'waveform-summary')
    expect(summaries).toHaveLength(2)
    const latest = summaries.at(-1)
    if (latest?.type !== 'waveform-summary') throw new Error('expected waveform summary')
    expect(latest).toMatchObject({ collectedItemCount: 2, bufferedItemCount: 3, channelCount: 1 })
    expect(Array.from(new Float32Array(latest.latestValues))).toEqual([4])

    decoder.handle({ type: 'history-snapshot', requestId: 7 })
    const snapshot = messages.at(-1)
    if (snapshot?.type !== 'history-snapshot') throw new Error('expected history snapshot')
    expect(snapshot).toMatchObject({ requestId: 7, itemCount: 3, channelCount: 1 })
    expect(Array.from(new Float32Array(snapshot.values))).toEqual([2, 3, 4])
    expect(transfers[messages.indexOf(snapshot)]).toEqual([snapshot.times, snapshot.values])

    decoder.handle({ type: 'waveform-detail', enabled: true })
    send(4n, [5])
    expect(messages.filter(message => message.type === 'waveform-batch')).toHaveLength(1)
  })

  it('rejects stale metadata, nonfinite samples, bad flags, and reset clears versions', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 16, channelCount: 1 })
    const metadata = (version: number) => new TextEncoder().encode(JSON.stringify({
      version, channels: [{ name: 'a' }],
    }))
    const send = (sequence: bigint, flags: number, payload: Uint8Array, itemCount = 0) => decoder.handle({
      type: 'frame', buffer: frame(sequence, itemCount, payload, StreamType.SUPERWATCH, 1n, flags),
      connectionGeneration: 1, frameTicket: Number(sequence),
    })
    send(1n, 0x02, metadata(2))
    send(2n, 0x02, metadata(1))
    expect(messages.at(-1)).toMatchObject({ type: 'error', code: 'INVALID_FRAME' })
    send(2n, 0x01, floats(Number.NaN), 1)
    expect(messages.at(-1)).toMatchObject({ type: 'error', code: 'INVALID_FRAME' })
    send(2n, 0x03, floats(1), 1)
    expect(messages.at(-1)).toMatchObject({ type: 'error', code: 'INVALID_FRAME' })
    decoder.handle({ type: 'reset' })
    send(1n, 0x02, metadata(1))
    expect(messages.filter(message => message.type === 'superwatch-metadata').at(-1)).toMatchObject({
      type: 'superwatch-metadata', version: 1,
    })
  })

  it('clears buffered samples when same-count SuperWatch metadata is renamed', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 16, channelCount: 1 })
    const metadata = (version: number, name: string) => new TextEncoder().encode(JSON.stringify({
      version, channels: [{ name }],
    }))
    const send = (sequence: bigint, flags: number, payload: Uint8Array, itemCount = 0) => decoder.handle({
      type: 'frame', buffer: frame(sequence, itemCount, payload, StreamType.SUPERWATCH, sequence, flags),
      connectionGeneration: 1, frameTicket: Number(sequence),
    })
    send(1n, 0x02, metadata(1, 'old'))
    send(2n, 0x01, floats(9), 1)
    expect(messages.filter(message => message.type === 'telemetry').at(-1)).toMatchObject({
      bufferedSamples: 1,
    })
    send(3n, 0x02, metadata(2, 'new'))
    expect(messages.at(-1)).toMatchObject({ type: 'telemetry', bufferedSamples: 0 })
  })

  it('accepts an identical same-version SuperWatch metadata replay without clearing samples', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 16, channelCount: 1 })
    const metadata = new TextEncoder().encode(JSON.stringify({
      version: 1, channels: [{ name: 'a', type: 'float' }],
    }))
    const send = (sequence: bigint, flags: number, payload: Uint8Array, itemCount = 0) => decoder.handle({
      type: 'frame', buffer: frame(sequence, itemCount, payload, StreamType.SUPERWATCH, sequence, flags),
      connectionGeneration: 1, frameTicket: Number(sequence),
    })
    send(1n, 0x02, metadata)
    send(2n, 0x01, floats(9), 1)
    send(3n, 0x02, metadata)
    expect(messages.at(-1)).toMatchObject({ type: 'telemetry', bufferedSamples: 1 })
    expect(messages.some(message => message.type === 'error')).toBe(false)
  })

  it('resets stream session for a newer connection generation and rejects late old frames', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 16, channelCount: 1 })
    const send = (sequence: bigint, generation: number, ticket: number, text: string) => decoder.handle({
      type: 'frame',
      buffer: frame(sequence, 1, rttLines({
        timestampNs: sequence, level: 0, text,
      }), StreamType.RTT_RAW, sequence, 0x01),
      connectionGeneration: generation,
      frameTicket: ticket,
    })

    send(10n, 1, 1, 'generation-one')
    send(1n, 2, 1, 'generation-two')
    send(11n, 1, 2, 'late-generation-one')

    expect(messages.filter(message => message.type === 'rtt-lines')).toEqual([
      expect.objectContaining({ sequence: 10n, lines: [expect.objectContaining({ text: 'generation-one' })] }),
      expect.objectContaining({ sequence: 1n, lines: [expect.objectContaining({ text: 'generation-two' })] }),
    ])
    expect(messages.at(-1)).toMatchObject({
      type: 'error', code: 'INVALID_FRAME', connectionGeneration: 1, frameTicket: 2,
    })
    expect(messages.filter(message => message.type === 'telemetry').at(-1)).toMatchObject({
      acceptedFrames: 1, lastSequence: 1n, acceptedConnectionGeneration: 2,
    })
  })

  it('accepts current SuperWatch metadata replay after generation reset', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 16, channelCount: 1 })
    const metadata = new TextEncoder().encode(JSON.stringify({
      version: 1, channels: [{ name: 'a' }],
    }))
    decoder.handle({
      type: 'frame', buffer: frame(9n, 0, metadata, StreamType.SUPERWATCH, 1n, 0x02),
      connectionGeneration: 1, frameTicket: 1,
    })
    decoder.handle({
      type: 'frame', buffer: frame(1n, 0, metadata, StreamType.SUPERWATCH, 2n, 0x02),
      connectionGeneration: 2, frameTicket: 1,
    })
    expect(messages.filter(message => message.type === 'superwatch-metadata')).toHaveLength(2)
    expect(messages.some(message => message.type === 'error')).toBe(false)
  })

  it('processes an accelerated 60-second 10 kHz RTT line stream without retaining raw text', () => {
    const targetLines = 60 * 10_000
    const batchLines = 100
    let receivedLines = 0
    let errors = 0
    let acceptedFrames = 0
    let bufferedSamples = 0
    const decoder = new StreamDecoder(message => {
      if (message.type === 'rtt-lines') receivedLines += message.lines.length
      if (message.type === 'error') errors++
      if (message.type === 'telemetry') {
        acceptedFrames = message.acceptedFrames
        bufferedSamples = message.bufferedSamples
      }
    })
    decoder.handle({ type: 'configure', capacity: 200_000, channelCount: 1 })
    const started = performance.now()
    for (let first = 0; first < targetLines; first += batchLines) {
      const count = Math.min(batchLines, targetLines - first)
      const payload = rttLines(...Array.from({ length: count }, (_, index) => ({
        timestampNs: BigInt(first + index + 1) * 100_000n,
        level: index % 2,
        text: `sample=${first + index}`,
      })))
      const sequence = BigInt(first / batchLines + 1)
      decoder.handle({
        type: 'frame', buffer: frame(
          sequence, count, payload, StreamType.RTT_RAW,
          BigInt(first + count) * 100_000n, 0x01,
        ), connectionGeneration: 1, frameTicket: Number(sequence),
      })
    }
    const elapsedMs = performance.now() - started
    expect(errors).toBe(0)
    expect(receivedLines).toBe(targetLines)
    expect(acceptedFrames).toBe(targetLines / batchLines)
    expect(bufferedSamples).toBe(0)
    expect(elapsedMs).toBeLessThan(60_000)
  }, 70_000)

  it('processes accelerated 60-second 10 kHz x8 SuperWatch data with bounded envelopes', () => {
    const targetSamples = 60 * 10_000
    const channelCount = 8
    const batchSamples = 2_000
    let errors = 0
    let acceptedFrames = 0
    let bufferedSamples = 0
    let maxEnvelopePoints = 0
    let envelopes = 0
    const decoder = new StreamDecoder(message => {
      if (message.type === 'error') errors++
      if (message.type === 'telemetry') {
        acceptedFrames = message.acceptedFrames
        bufferedSamples = message.bufferedSamples
      }
      if (message.type === 'render-envelope') {
        envelopes++
        maxEnvelopePoints = Math.max(maxEnvelopePoints, message.pointCount)
      }
    })
    decoder.handle({ type: 'configure', capacity: 200_000, channelCount: 1 })
    const metadata = new TextEncoder().encode(JSON.stringify({
      version: 1,
      channels: Array.from({ length: channelCount }, (_, index) => ({ name: `S${index}` })),
    }))
    decoder.handle({
      type: 'frame', buffer: frame(1n, 0, metadata, StreamType.SUPERWATCH, 1n, 0x02),
      connectionGeneration: 1, frameTicket: 1,
    })
    let requestId = 0
    const started = performance.now()
    for (let first = 0; first < targetSamples; first += batchSamples) {
      const values = new Float32Array(batchSamples * channelCount)
      for (let index = 0; index < values.length; index++) values[index] = (first + index) % 10_000
      const sequence = BigInt(first / batchSamples + 2)
      const endMs = (first + batchSamples) / 10_000 * 1_000
      decoder.handle({
        type: 'frame', buffer: frame(
          sequence, batchSamples, new Uint8Array(values.buffer), StreamType.SUPERWATCH,
          BigInt(Math.round(endMs * 1_000_000)), 0x01,
        ), connectionGeneration: 1, frameTicket: Number(sequence),
      })
      for (let request = 0; request < 6; request++) {
        decoder.handle({
          type: 'visible-range', requestId: ++requestId,
          start: Math.max(0, endMs - 1_000), end: endMs, pixelWidth: 320,
        })
      }
    }
    const elapsedMs = performance.now() - started
    expect(errors).toBe(0)
    expect(acceptedFrames).toBe(1 + targetSamples / batchSamples)
    expect(bufferedSamples).toBe(200_000)
    expect(envelopes).toBe(60 * 30)
    expect(maxEnvelopePoints).toBeLessThanOrEqual(2 * 320 * channelCount)
    expect(elapsedMs).toBeLessThan(60_000)
  }, 70_000)
  it('emits each VOFA sample-major batch as a transferable typed envelope', () => {
    const { decoder, messages, transfers } = setup()
    decoder.handle({ type: 'configure', capacity: 16, channelCount: 2 })
    decoder.handle({
      type: 'frame',
      buffer: frame(
        7n, 3, floats(1, 10, 2, 20, 3, 30),
        StreamType.WAVEFORM, 5_000_000n, 0x01,
      ),
      connectionGeneration: 1,
      frameTicket: 1,
    })

    const batch = messages.find(message => message.type === 'waveform-batch')
    if (batch?.type !== 'waveform-batch') throw new Error('expected waveform batch')
    expect(batch).toMatchObject({
      sequence: 7n,
      timestampNs: 5_000_000n,
      itemCount: 3,
      channelCount: 2,
      layout: 'sample-major-float32',
    })
    expect(Array.from(new Float32Array(batch.values))).toEqual([1, 10, 2, 20, 3, 30])
    const times = new Float64Array((batch as any).times)
    expect(times).toHaveLength(3)
    expect(times[0]).toBeLessThan(times[1])
    expect(times[1]).toBeLessThan(times[2])
    expect(transfers[messages.indexOf(batch)]).toEqual([batch.values, (batch as any).times])
  })

  it.each([0x02, 0x03])(
    'rejects unknown VOFA layout flags 0x%s before mutating the typed ring',
    flags => {
      const { decoder, messages } = setup()
      decoder.handle({ type: 'configure', capacity: 16, channelCount: 1 })
      decoder.handle({
        type: 'frame',
        buffer: frame(1n, 1, floats(9), StreamType.WAVEFORM, 1_000_000n, flags),
        connectionGeneration: 1,
        frameTicket: 1,
      })
      expect(messages.at(-1)).toMatchObject({ type: 'error', code: 'INVALID_FRAME' })

      decoder.handle({
        type: 'visible-range', requestId: 1, start: 0, end: 10, pixelWidth: 10,
      })
      expect(messages.at(-1)).toMatchObject({ type: 'render-envelope', pointCount: 0 })
    },
  )

  it('uses VOFA frame metadata to reconfigure a changed channel count', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 16, channelCount: 1 })
    decoder.handle({
      type: 'frame',
      buffer: frame(
        1n, 2, floats(1, 10, 2, 20), StreamType.WAVEFORM,
        1_000_000n, 0x01,
      ),
      connectionGeneration: 1,
      frameTicket: 1,
    })

    const batch = messages.find(message => message.type === 'waveform-batch')
    expect(batch).toMatchObject({ type: 'waveform-batch', channelCount: 2, itemCount: 2 })
    expect(messages).toContainEqual({ type: 'channels', channelCount: 2 })
    expect(messages.at(-1)).toMatchObject({ type: 'telemetry', bufferedSamples: 2 })
  })

  it.each([
    {
      name: 'unknown flags',
      invalid: () => frame(2n, 1, floats(9), StreamType.WAVEFORM, 2_000_000n, 0x03),
    },
    {
      name: 'misaligned length',
      invalid: () => frame(2n, 2, floats(9), StreamType.WAVEFORM, 2_000_000n, 0x01),
    },
    {
      name: 'more than 64 channels',
      invalid: () => frame(
        2n, 1, floats(...Array.from({ length: 65 }, (_, index) => index)),
        StreamType.WAVEFORM, 2_000_000n, 0x01,
      ),
    },
    {
      name: 'non-finite dynamic channel payload',
      invalid: () => frame(2n, 1, floats(9, Number.NaN), StreamType.WAVEFORM, 2_000_000n, 0x01),
    },
    {
      name: 'duplicate sequence',
      invalid: () => frame(1n, 1, floats(9), StreamType.WAVEFORM, 2_000_000n, 0x01),
    },
  ])('rejects $name before changing sequence, configuration, telemetry, or ring', ({ invalid }) => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 16, channelCount: 1 })
    decoder.handle({
      type: 'frame', buffer: frame(
        1n, 2, floats(1, 2), StreamType.WAVEFORM, 1_000_000n, 0x01,
      ), connectionGeneration: 1, frameTicket: 1,
    })
    const telemetryBefore = messages.filter(message => message.type === 'telemetry').at(-1)
    const channelMessagesBefore = messages.filter(message => message.type === 'channels').length

    decoder.handle({
      type: 'frame', buffer: invalid(), connectionGeneration: 1, frameTicket: 2,
    })

    expect(messages.at(-1)).toMatchObject({ type: 'error', code: 'INVALID_FRAME' })
    expect(messages.filter(message => message.type === 'telemetry').at(-1)).toEqual(telemetryBefore)
    expect(messages.filter(message => message.type === 'channels')).toHaveLength(channelMessagesBefore)
    decoder.handle({ type: 'visible-range', requestId: 90, start: -1, end: 10, pixelWidth: 10 })
    const visible = messages.at(-1) as any
    expect(visible).toMatchObject({
      type: 'render-envelope', mode: 'min-max-v1', channelCount: 1, pointCount: 2,
    })
    expect(Array.from(new Float32Array(visible.values))).toEqual([1, 2])
  })

  it('assigns strictly monotonic sample times across repeated timestamps, gaps, and wrap', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 4, channelCount: 1 })
    const emittedTimes: number[] = []
    const send = (sequence: bigint, timestampNs: bigint, values: number[]) => {
      decoder.handle({
        type: 'frame', buffer: frame(
          sequence, values.length, floats(...values), StreamType.WAVEFORM, timestampNs, 0x01,
        ), connectionGeneration: 1, frameTicket: Number(sequence),
      })
      const batch = messages.filter(message => message.type === 'waveform-batch').at(-1) as any
      emittedTimes.push(...new Float64Array(batch.times))
    }
    send(1n, 1_000_000n, [1, 2])
    send(2n, 1_000_000n, [3, 4])
    send(3n, 100_000_000_000n, [5, 6])
    send(4n, 100_001_000_000n, [7, 8])

    expect(emittedTimes.every((time, index) => index === 0 || time > emittedTimes[index - 1]))
      .toBe(true)
    decoder.handle({
      type: 'visible-range', requestId: 91,
      start: emittedTimes[0], end: emittedTimes.at(-1)!, pixelWidth: 10,
    })
    const visible = messages.at(-1) as any
    const selectedTimes = new Float64Array(visible.times)
    expect(Array.from(selectedTimes).every(
      (time, index) => index === 0 || time > selectedTimes[index - 1],
    )).toBe(true)
  })

  it('bounds a 200k x 8 numeric visible envelope to two points per pixel and channel', () => {
    const { decoder, messages } = setup()
    const channelCount = 8
    const sampleCount = 200_000
    const batchSamples = 2_000
    decoder.handle({ type: 'configure', capacity: sampleCount, channelCount })
    for (let base = 0; base < sampleCount; base += batchSamples) {
      const values = new Float32Array(batchSamples * channelCount)
      for (let sample = 0; sample < batchSamples; sample += 1) {
        for (let channel = 0; channel < channelCount; channel += 1) {
          values[sample * channelCount + channel] = Math.sin((base + sample) / (channel + 1))
        }
      }
      const sequence = BigInt(base / batchSamples + 1)
      decoder.handle({
        type: 'frame', buffer: frame(
          sequence, batchSamples, new Uint8Array(values.buffer), StreamType.WAVEFORM,
          BigInt(base + batchSamples) * 1_000_000n, 0x01,
        ), connectionGeneration: 1, frameTicket: Number(sequence),
      })
    }
    decoder.handle({
      type: 'visible-range', requestId: 92, start: 0, end: sampleCount, pixelWidth: 320,
    })

    const visible = messages.at(-1) as any
    expect(visible).toMatchObject({
      type: 'render-envelope', mode: 'min-max-v1', channelCount,
      candidateSampleCount: sampleCount,
    })
    expect(visible.pointCount).toBeLessThanOrEqual(2 * 320 * channelCount)
    expect(new Uint32Array(visible.channelOffsets)).toHaveLength(channelCount + 1)
    expect(new Uint32Array(visible.timeIndices)).toHaveLength(visible.pointCount)
    expect(new Float32Array(visible.values)).toHaveLength(visible.pointCount)
    expect(new Float64Array(visible.times).length).toBeLessThanOrEqual(visible.pointCount)
  }, 15_000)
  it('preserves ticks above Number.MAX_SAFE_INTEGER and exposes relative plot coordinates', () => {
    const { decoder, messages } = setup()
    const origin = 9_007_199_254_740_993n
    decoder.handle({ type: 'configure', capacity: 8, channelCount: 1 })
    decoder.handle({
      type: 'frame',
      buffer: frame(1n, 2, systemViewRecords(
        { kind: 4, taskId: 7, ticks: origin, timeUs: 0, flags: 0x01 },
        { kind: 5, taskId: 7, ticks: origin + 10n, timeUs: 0, flags: 0x01 },
      ), StreamType.SYSTEMVIEW),
      connectionGeneration: 1, frameTicket: 1,
    })
    decoder.handle({ type: 'visible-range', requestId: 1, start: 0, end: 20, pixelWidth: 100 })

    const visible = messages.at(-1)
    if (visible?.type !== 'systemview-visible') throw new Error('expected SystemView visible data')
    expect(visible.tickOrigin).toBe(origin)
    expect(visible.latestTime).toBe(10)
    expect(Array.from(new Float64Array(visible.starts))).toEqual([0])
    expect(Array.from(new Float64Array(visible.ends))).toEqual([10])
    expect(Array.from(new BigUint64Array(visible.startTicks))).toEqual([origin])
    expect(Array.from(new BigUint64Array(visible.endTicks))).toEqual([origin + 10n])
    expect(visible.events[0]).toMatchObject({
      t_ticks: origin, t_ticks_exact: origin.toString(), t_relative: 0,
    })
  })

  it('bounds interval envelopes to two records per pixel and scans only visible candidates', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 4_000, channelCount: 1 })
    decoder.handle({
      type: 'frame', buffer: frame(
        1n, 2_000, systemViewTaskPairs(1, 1_000, 1_000n), StreamType.SYSTEMVIEW,
      ), connectionGeneration: 1, frameTicket: 1,
    })
    decoder.handle({ type: 'visible-range', requestId: 2, start: 1_800, end: 2_000, pixelWidth: 10 })

    const visible = messages.at(-1)
    expect(visible).toMatchObject({ type: 'systemview-visible' })
    if (visible?.type !== 'systemview-visible') throw new Error('expected SystemView visible data')
    expect(visible.candidateIntervalCount).toBeLessThanOrEqual(102)
    expect(visible.intervalCount).toBeLessThanOrEqual(20)
  })

  it('keeps full-capacity append O(1) without Array.splice during 500 more batches', () => {
    const { decoder, messages } = setup()
    const splice = vi.spyOn(Array.prototype, 'splice')
    let spliceCalls = 0
    decoder.handle({ type: 'configure', capacity: 100_000, channelCount: 1 })
    let sequence = 1n
    let tick = 1n
    const started = performance.now()
    try {
      for (let batch = 0; batch < 200; batch++) {
        const payload = systemViewTaskPairs(1, 250, tick)
        decoder.handle({
          type: 'frame', buffer: frame(sequence++, 500, payload, StreamType.SYSTEMVIEW),
          connectionGeneration: 1, frameTicket: Number(sequence),
        })
        tick += 500n
      }
      for (let batch = 0; batch < 500; batch++) {
        const payload = systemViewTaskPairs(1, 250, tick)
        decoder.handle({
          type: 'frame', buffer: frame(sequence++, 500, payload, StreamType.SYSTEMVIEW),
          connectionGeneration: 1, frameTicket: Number(sequence),
        })
        tick += 500n
      }
    } finally {
      spliceCalls = splice.mock.calls.length
      splice.mockRestore()
    }
    expect(spliceCalls).toBe(0)
    expect(performance.now() - started).toBeLessThan(5_000)
    expect(messages.at(-1)).toMatchObject({ type: 'telemetry', bufferedSamples: 100_000 })
    const latestOffset = Number(tick - 1n - 1n)
    decoder.handle({
      type: 'visible-range', requestId: 99,
      start: latestOffset - 100, end: latestOffset, pixelWidth: 10,
    })
    const visible = messages.at(-1)
    if (visible?.type !== 'systemview-visible') throw new Error('expected SystemView visible data')
    expect(visible.candidateIntervalCount).toBeLessThanOrEqual(52)
    expect(visible.intervalCount).toBeLessThanOrEqual(20)
  }, 10_000)

  it('decodes SystemView records and returns only prefiltered visible intervals', () => {
    const { decoder, messages, transfers } = setup()
    decoder.handle({ type: 'configure', capacity: 8, channelCount: 1 })
    decoder.handle({
      type: 'frame',
      buffer: frame(1n, 4, systemViewRecords(
        { kind: 4, taskId: 1, ticks: 10n, timeUs: 10 },
        { kind: 5, taskId: 1, ticks: 20n, timeUs: 20 },
        { kind: 4, taskId: 2, ticks: 30n, timeUs: 30 },
        { kind: 5, taskId: 2, ticks: 50n, timeUs: 50 },
      ), StreamType.SYSTEMVIEW),
      connectionGeneration: 1,
      frameTicket: 1,
    })
    decoder.handle({ type: 'visible-range', requestId: 9, start: 15, end: 45, pixelWidth: 400 })

    const visible = messages.at(-1)
    expect(visible).toMatchObject({
      type: 'systemview-visible', requestId: 9, intervalCount: 1, eventCount: 2,
    })
    if (visible?.type !== 'systemview-visible') throw new Error('expected SystemView visible data')
    expect(Array.from(new Uint32Array(visible.taskIds))).toEqual([2])
    expect(Array.from(new Float64Array(visible.starts))).toEqual([20])
    expect(Array.from(new Float64Array(visible.ends))).toEqual([40])
    expect(transfers.at(-1)).toEqual([
      visible.taskIds, visible.contextTypes, visible.starts, visible.ends,
      visible.startTicks, visible.endTicks,
    ])
  })

  it('rejects malformed SystemView record payloads', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 8, channelCount: 1 })
    decoder.handle({
      type: 'frame', buffer: frame(1n, 1, new Uint8Array(47), StreamType.SYSTEMVIEW),
      connectionGeneration: 1, frameTicket: 1,
    })
    expect(messages.at(-1)).toMatchObject({ type: 'error', code: 'INVALID_FRAME' })
  })

  it.each([
    { name: 'unknown kind', kind: 30, flags: 0x07, reserved: 0 },
    { name: 'unknown flags', kind: 4, flags: 0x80, reserved: 0 },
    { name: 'reserved bytes', kind: 4, flags: 0x07, reserved: 1 },
  ])('rejects $name in fixed SystemView records', ({ kind, flags, reserved }) => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 8, channelCount: 1 })
    decoder.handle({
      type: 'frame',
      buffer: frame(1n, 1, systemViewRecords({
        kind, flags, reserved, taskId: 1, ticks: 1n, timeUs: 1,
      }), StreamType.SYSTEMVIEW),
      connectionGeneration: 1, frameTicket: 1,
    })
    expect(messages.at(-1)).toMatchObject({ type: 'error', code: 'INVALID_FRAME' })
  })

  it.each(
    ([0, 0x07] as const).flatMap(flags =>
      ([16, 24, 32, 40] as const).flatMap(slotOffset =>
        [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY].map(value => ({
          flags, slotOffset, value,
        })),
      ),
    ),
  )('rejects non-finite slot $slotOffset with flags $flags atomically', ({ flags, slotOffset, value }) => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 8, channelCount: 1 })
    const payload = systemViewRecords(
      { kind: 4, taskId: 1, ticks: 1n, timeUs: 1 },
      { kind: 5, taskId: 1, ticks: 2n, timeUs: 2, flags },
    )
    new DataView(payload.buffer).setFloat64(48 + slotOffset, value, true)
    decoder.handle({
      type: 'frame', buffer: frame(1n, 2, payload, StreamType.SYSTEMVIEW),
      connectionGeneration: 1, frameTicket: 1,
    })
    expect(messages.at(-1)).toMatchObject({ type: 'error', code: 'INVALID_FRAME' })
    decoder.handle({ type: 'visible-range', requestId: 1, start: 0, end: 10, pixelWidth: 100 })
    expect(messages.at(-1)).toMatchObject({ type: 'render-envelope', pointCount: 0 })
  })

  it('splits task intervals around nested ISR execution and resumes after outer exit', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 16, channelCount: 1 })
    decoder.handle({
      type: 'frame',
      buffer: frame(1n, 7, systemViewRecords(
        { kind: 4, taskId: 7, ticks: 10n, timeUs: 10 },
        { kind: 2, taskId: 15, ticks: 20n, timeUs: 20 },
        { kind: 2, taskId: 16, ticks: 22n, timeUs: 22 },
        { kind: 3, taskId: 0, ticks: 24n, timeUs: 24 },
        { kind: 3, taskId: 0, ticks: 30n, timeUs: 30 },
        { kind: 5, taskId: 7, ticks: 40n, timeUs: 40 },
        { kind: 17, taskId: 0, ticks: 41n, timeUs: 41 },
      ), StreamType.SYSTEMVIEW),
      connectionGeneration: 1, frameTicket: 1,
    })
    decoder.handle({ type: 'visible-range', requestId: 1, start: 0, end: 50, pixelWidth: 400 })
    const visible = messages.at(-1)
    if (visible?.type !== 'systemview-visible') throw new Error('expected SystemView visible data')
    expect(Array.from(new Uint32Array(visible.taskIds))).toEqual([7, 15, 16, 15, 7])
    expect(Array.from(new Uint8Array(visible.contextTypes))).toEqual([1, 2, 2, 2, 1])
    expect(Array.from(new Float64Array(visible.starts))).toEqual([0, 10, 12, 14, 20])
    expect(Array.from(new Float64Array(visible.ends))).toEqual([10, 12, 14, 20, 30])
  })

  it('does not fabricate a long task interval across a target overflow', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 32, channelCount: 1 })
    decoder.handle({
      type: 'frame', buffer: frame(1n, 6, systemViewRecords(
        { kind: 4, taskId: 7, ticks: 10n, timeUs: 10 },
        { kind: 1, ticks: 20n, timeUs: 20 },
        { kind: 17, ticks: 30n, timeUs: 30 },
        { kind: 4, taskId: 8, ticks: 40n, timeUs: 40 },
        { kind: 5, taskId: 8, ticks: 50n, timeUs: 50 },
        { kind: 17, ticks: 60n, timeUs: 60 },
      ), StreamType.SYSTEMVIEW),
      connectionGeneration: 1,
      frameTicket: 1,
    })
    decoder.handle({ type: 'visible-range', requestId: 1, start: 0, end: 70, pixelWidth: 400 })

    const visible = messages.at(-1)
    if (visible?.type !== 'systemview-visible') throw new Error('expected SystemView visible data')
    expect(Array.from(new Uint32Array(visible.taskIds))).toEqual([0, 8])
    expect(Array.from(new Float64Array(visible.starts))).toEqual([20, 30])
    expect(Array.from(new Float64Array(visible.ends))).toEqual([30, 40])
    expect(visible.events.find(event => event.kind === 'overflow')).toBeTruthy()
  })

  it('keeps inactive task metadata and computes exact Task ISR Scheduler Idle statistics', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 32, channelCount: 1 })
    const records = systemViewRecords(
      { kind: 9, taskId: 1, ticks: 1n, timeUs: 1, aux0: 10 },
      { kind: 21, taskId: 1, ticks: 2n, timeUs: 2, aux0: 0x20001000, aux1: 1024 },
      { kind: 9, taskId: 2, ticks: 3n, timeUs: 3, aux0: 20 },
      { kind: 4, taskId: 1, ticks: 10n, timeUs: 10 },
      { kind: 5, taskId: 1, ticks: 20n, timeUs: 20 },
      { kind: 2, taskId: 15, ticks: 20n, timeUs: 20 },
      { kind: 18, taskId: 0, ticks: 22n, timeUs: 22 },
      { kind: 17, taskId: 0, ticks: 23n, timeUs: 23 },
      { kind: 2, taskId: 15, ticks: 30n, timeUs: 30 },
      { kind: 18, taskId: 0, ticks: 32n, timeUs: 32 },
      { kind: 4, taskId: 1, ticks: 33n, timeUs: 33 },
      { kind: 11, taskId: 0, ticks: 40n, timeUs: 40 },
    )
    decoder.handle({
      type: 'frame', buffer: frame(1n, 12, records, StreamType.SYSTEMVIEW),
      connectionGeneration: 1, frameTicket: 1,
    })
    decoder.handle({ type: 'visible-range', requestId: 7, start: 0, end: 50, pixelWidth: 400 })

    const visible = messages.at(-1)
    if (visible?.type !== 'systemview-visible') throw new Error('expected SystemView visible data')
    const byContext = new Map(visible.contexts.map(context => [`${context.type}:${context.id}`, context]))
    expect(byContext.get('1:1')).toMatchObject({
      priority: 10, stackBase: 0x20001000, stackSize: 1024,
      count: 2, totalTicks: 17, minTicks: 7, maxTicks: 10,
    })
    expect(byContext.get('1:2')).toMatchObject({ priority: 20, count: 0, totalTicks: 0 })
    expect(byContext.get('2:15')).toMatchObject({ count: 2, totalTicks: 4 })
    expect(byContext.get('3:0')).toMatchObject({ count: 2, totalTicks: 2 })
    expect(byContext.get('4:0')).toMatchObject({ count: 1, totalTicks: 7 })
    expect(visible.events.find(event => event.kind === 'idle')).toMatchObject({ duration_ticks: 7 })
    expect(visible.events.filter(event => event.kind === 'isr_to_scheduler'))
      .toEqual(expect.arrayContaining([
        expect.objectContaining({ isr_id: 15, duration_ticks: 1 }),
      ]))
  })

  it('keeps context statistics cumulative when the visible range moves', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 32, channelCount: 1 })
    decoder.handle({
      type: 'frame', buffer: frame(1n, 4, systemViewRecords(
        { kind: 4, taskId: 1, ticks: 10n, timeUs: 10 },
        { kind: 5, taskId: 1, ticks: 20n, timeUs: 20 },
        { kind: 4, taskId: 1, ticks: 30n, timeUs: 30 },
        { kind: 5, taskId: 1, ticks: 40n, timeUs: 40 },
      ), StreamType.SYSTEMVIEW),
      connectionGeneration: 1, frameTicket: 1,
    })

    decoder.handle({ type: 'visible-range', requestId: 1, start: 0, end: 25, pixelWidth: 200 })
    const first = messages.at(-1)
    decoder.handle({ type: 'visible-range', requestId: 2, start: 25, end: 50, pixelWidth: 200 })
    const second = messages.at(-1)
    if (first?.type !== 'systemview-visible' || second?.type !== 'systemview-visible') {
      throw new Error('expected SystemView visible data')
    }

    const firstTask = first.contexts.find(context => context.type === 1 && context.id === 1)
    const secondTask = second.contexts.find(context => context.type === 1 && context.id === 1)
    expect(firstTask).toMatchObject({ count: 2, totalTicks: 20 })
    expect(secondTask).toMatchObject({ count: 2, totalTicks: 20 })
  })

  it('breaks interval pairing across a dropped batch and reset clears pending context', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 8, channelCount: 1 })
    decoder.handle({
      type: 'frame', buffer: frame(1n, 1, systemViewRecords(
        { kind: 4, taskId: 1, ticks: 10n, timeUs: 10 },
      ), StreamType.SYSTEMVIEW), connectionGeneration: 1, frameTicket: 1,
    })
    decoder.handle({
      type: 'frame', buffer: frame(3n, 2, systemViewRecords(
        { kind: 5, taskId: 1, ticks: 20n, timeUs: 20 },
        { kind: 4, taskId: 2, ticks: 30n, timeUs: 30 },
      ), StreamType.SYSTEMVIEW), connectionGeneration: 1, frameTicket: 2,
    })
    decoder.handle({ type: 'visible-range', requestId: 1, start: 0, end: 35, pixelWidth: 400 })
    expect(messages.at(-1)).toMatchObject({ type: 'systemview-visible', intervalCount: 0 })

    decoder.handle({ type: 'reset' })
    decoder.handle({
      type: 'frame', buffer: frame(1n, 1, systemViewRecords(
        { kind: 5, taskId: 2, ticks: 40n, timeUs: 40 },
      ), StreamType.SYSTEMVIEW), connectionGeneration: 1, frameTicket: 3,
    })
    decoder.handle({ type: 'visible-range', requestId: 2, start: 0, end: 50, pixelWidth: 400 })
    const visible = messages.at(-1)
    expect(visible).toMatchObject({ type: 'systemview-visible', intervalCount: 0 })
  })

  it('configures channels and decodes sample-major multi-channel frames', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 8, channelCount: 2 })
    decoder.handle({
      type: 'frame', buffer: frame(1n, 2, floats(1, 101, 2, 102)),
      connectionGeneration: 7, frameTicket: 11,
    })

    expect(messages[0]).toEqual({ type: 'channels', channelCount: 2 })
    expect(messages.at(-1)).toMatchObject({
      type: 'telemetry',
      bufferedSamples: 2,
      acceptedFrames: 1,
      acceptedConnectionGeneration: 7,
      acceptedFrameTicket: 11,
      transportDroppedBatches: 0,
      backendDroppedBatches: 0,
    })
  })

  it('reports only visible typed data and transfers its ArrayBuffers', () => {
    const { decoder, messages, transfers } = setup()
    decoder.handle({ type: 'configure', capacity: 8, channelCount: 2 })
    decoder.handle({
      type: 'frame',
      buffer: frame(1n, 3, floats(1, 10, 2, 20, 3, 30), StreamType.WAVEFORM, 5_000_000n),
      connectionGeneration: 1,
      frameTicket: 1,
    })
    const batch = messages.find(message => message.type === 'waveform-batch')
    expect(batch).toMatchObject({
      type: 'waveform-batch', bufferStartMs: 4.999998, bufferEndMs: 5,
    })
    decoder.handle({ type: 'visible-range', requestId: 7, start: 0, end: 5, pixelWidth: 320 })

    const envelope = messages.at(-1)
    expect(envelope).toMatchObject({
      type: 'render-envelope',
      mode: 'min-max-v1',
      requestId: 7,
      pixelWidth: 320,
      channelCount: 2,
      pointCount: 4,
      candidateSampleCount: 3,
      timestampKind: 'sample-milliseconds',
    })
    if (envelope?.type !== 'render-envelope') throw new Error('expected envelope')
    expect(Array.from(new Uint32Array(envelope.channelOffsets))).toEqual([0, 2, 4])
    expect(Array.from(new Float64Array(envelope.times))).toEqual([4.999998, 5])
    expect(Array.from(new Uint32Array(envelope.timeIndices))).toEqual([0, 1, 0, 1])
    expect(Array.from(new Float32Array(envelope.values))).toEqual([1, 3, 10, 30])
    expect(transfers.at(-1)).toEqual([
      envelope.channelOffsets, envelope.times, envelope.timeIndices, envelope.values,
    ])
  })

  it('keeps transport sequence gaps separate from backend-reported drops', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 8, channelCount: 1 })
    decoder.handle({
      type: 'frame', buffer: frame(10n, 1, floats(1)),
      connectionGeneration: 1, frameTicket: 1,
    })
    decoder.handle({
      type: 'frame', buffer: frame(13n, 1, floats(2)),
      connectionGeneration: 1, frameTicket: 2,
    })
    const control = new TextEncoder().encode(JSON.stringify({
      dropped_batches: 4,
      dropped_items: 40,
      dropped_bytes: 160,
    }))
    decoder.handle({
      type: 'frame', buffer: frame(13n, 0, control, StreamType.CONTROL),
      connectionGeneration: 1, frameTicket: 3,
    })

    expect(messages.at(-1)).toMatchObject({
      type: 'telemetry',
      transportDroppedBatches: 2,
      backendDroppedBatches: 4,
      acceptedFrames: 3,
      acceptedConnectionGeneration: 1,
      acceptedFrameTicket: 3,
      backendDroppedItems: 40,
      backendDroppedBytes: 160,
    })
  })

  it('rejects invalid configuration and numeric frame layouts as worker errors', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 0, channelCount: 2 })
    expect(messages.at(-1)).toMatchObject({ type: 'error', code: 'INVALID_CONFIG' })

    decoder.handle({ type: 'configure', capacity: 8, channelCount: 2 })
    decoder.handle({
      type: 'frame', buffer: frame(1n, 2, floats(1, 2, 3)),
      connectionGeneration: 1, frameTicket: 1,
    })
    expect(messages.at(-1)).toMatchObject({
      type: 'error', code: 'INVALID_FRAME',
      connectionGeneration: 1, frameTicket: 1,
    })
    expect(messages.some(message => (
      message.type === 'telemetry' && message.acceptedFrameTicket === 1
    ))).toBe(false)
  })

  it('resets buffered data and all drop telemetry while preserving configuration', () => {
    const { decoder, messages } = setup()
    decoder.handle({ type: 'configure', capacity: 8, channelCount: 1 })
    decoder.handle({
      type: 'frame', buffer: frame(1n, 1, floats(1)),
      connectionGeneration: 1, frameTicket: 1,
    })
    decoder.handle({
      type: 'frame', buffer: frame(3n, 1, floats(2)),
      connectionGeneration: 1, frameTicket: 2,
    })
    decoder.handle({ type: 'reset' })

    expect(messages.at(-1)).toMatchObject({
      type: 'telemetry',
      bufferedSamples: 0,
      acceptedFrames: 0,
      transportDroppedBatches: 0,
      backendDroppedBatches: 0,
    })
  })
})
