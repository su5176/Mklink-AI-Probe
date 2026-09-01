const MAGIC = [0x4d, 0x4b, 0x53, 0x54] as const
const VERSION = 1
const HEADER_SIZE = 36
const MAX_PAYLOAD_SIZE = 4 * 1024 * 1024

export const StreamType = {
  SYSTEMVIEW: 1,
  WAVEFORM: 2,
  RTT_RAW: 3,
  SUPERWATCH: 4,
  SERIAL: 5,
  CONTROL: 255,
} as const

export const WAVEFORM_SAMPLE_MAJOR_FLOAT32 = 0x01
export const RTT_RAW_UTF8_LINES = 0x01
export const RTT_TERMINAL_UTF8 = 0x02
export const SERIAL_RX_BYTES = 0x01
export const SERIAL_TX_BYTES = 0x02
export const SUPERWATCH_SAMPLE_MAJOR_FLOAT32 = 0x01
export const SUPERWATCH_METADATA_JSON = 0x02
export const SUPERWATCH_TIMESTAMPED_FLOAT32 = 0x03

export type StreamType = (typeof StreamType)[keyof typeof StreamType]

export interface StreamFrame {
  readonly streamType: StreamType
  readonly flags: number
  readonly streamId: number
  readonly sequence: bigint
  readonly timestampNs: bigint
  readonly itemCount: number
  readonly payload: ArrayBuffer
}

function isStreamType(value: number): value is StreamType {
  return Object.values(StreamType).includes(value as StreamType)
}

export function decodeFrame(buffer: ArrayBuffer): StreamFrame {
  if (buffer.byteLength < HEADER_SIZE) {
    throw new Error('frame is shorter than the 36-byte header size')
  }
  const view = new DataView(buffer)
  if (MAGIC.some((value, index) => view.getUint8(index) !== value)) {
    throw new Error('invalid stream frame magic')
  }
  const version = view.getUint8(4)
  if (version !== VERSION) {
    throw new Error(`unsupported stream frame version: ${version}`)
  }
  const streamType = view.getUint8(5)
  if (!isStreamType(streamType)) {
    throw new Error(`unknown stream type: ${streamType}`)
  }
  const headerSize = view.getUint8(7)
  if (headerSize !== HEADER_SIZE) {
    throw new Error(`invalid header size: ${headerSize}`)
  }
  const payloadLength = view.getUint32(32, true)
  if (payloadLength > MAX_PAYLOAD_SIZE) {
    throw new Error('payload exceeds 4 MiB limit')
  }
  if (buffer.byteLength - HEADER_SIZE !== payloadLength) {
    throw new Error('payload length does not match frame size')
  }
  return {
    streamType,
    flags: view.getUint8(6),
    streamId: view.getUint32(8, true),
    sequence: view.getBigUint64(12, true),
    timestampNs: view.getBigUint64(20, true),
    itemCount: view.getUint32(28, true),
    payload: buffer.slice(HEADER_SIZE),
  }
}
