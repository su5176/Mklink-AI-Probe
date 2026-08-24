import { computed, onUnmounted, readonly, ref, shallowRef } from 'vue'
import { StreamClient } from '../lib/stream/streamClient'
import type { StreamClientOptions, StreamClientState } from '../lib/stream/streamClient'
import type { StreamTelemetry, WorkerOutput } from '../workers/streamDecoder.worker'
import type { DecoderMode } from '../workers/streamDecoder.worker'
import { API_BASE } from '../lib/runtimeEndpoint'

export type BinaryStreamName = 'systemview' | 'vofa' | 'rtt' | 'rtt-terminal' | 'serial' | 'superwatch'

export interface BinaryStreamClient {
  start(): void
  stop(): void
  reset(): void
  configure(capacity: number, channelCount: number): void
  requestVisibleRange(requestId: number, start: number, end: number, pixelWidth: number): void
  dispose(): void
}

export interface UseBinaryStreamOptions {
  readonly capacity: number
  readonly channelCount: number
  readonly decoderMode?: DecoderMode
  readonly token?: string
  readonly autoStart?: boolean
  readonly createClient?: (options: StreamClientOptions) => BinaryStreamClient
}

type RenderEnvelope = Extract<WorkerOutput, { type: 'render-envelope' }>
type SystemViewVisible = Extract<WorkerOutput, { type: 'systemview-visible' }>
type WaveformBatch = Extract<WorkerOutput, { type: 'waveform-batch' }>
type RttLines = Extract<WorkerOutput, { type: 'rtt-lines' }>
type RttTerminal = Extract<WorkerOutput, { type: 'rtt-terminal' }>
type SuperWatchMetadata = Extract<WorkerOutput, { type: 'superwatch-metadata' }>
type SerialLines = Extract<WorkerOutput, { type: 'serial-lines' }>
type SerialTerminal = Extract<WorkerOutput, { type: 'serial-terminal' }>

function streamUrl(stream: BinaryStreamName): string {
  if (API_BASE) {
    const base = new URL(API_BASE, window.location.href)
    base.protocol = base.protocol === 'https:' ? 'wss:' : 'ws:'
    base.pathname = `/ws/streams/${stream}`
    base.search = ''
    base.hash = ''
    return base.toString()
  }
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/ws/streams/${stream}`
}

export function useBinaryStream(
  stream: BinaryStreamName,
  options: UseBinaryStreamOptions,
) {
  const state = ref<StreamClientState>({ phase: 'stopped' })
  const telemetry = shallowRef<StreamTelemetry | null>(null)
  const channelCount = ref(options.channelCount)
  const envelope = shallowRef<RenderEnvelope | null>(null)
  const systemViewVisible = shallowRef<SystemViewVisible | null>(null)
  const waveformBatch = shallowRef<WaveformBatch | null>(null)
  const rttLines = shallowRef<RttLines | null>(null)
  const rttTerminal = shallowRef<RttTerminal | null>(null)
  const superwatchMetadata = shallowRef<SuperWatchMetadata | null>(null)
  const serialLines = shallowRef<SerialLines | null>(null)
  const serialTerminal = shallowRef<SerialTerminal | null>(null)
  const error = ref<string | null>(null)

  function onState(next: StreamClientState): void {
    state.value = next
    if (next.error) error.value = next.error
  }

  function onWorkerMessage(message: WorkerOutput): void {
    switch (message.type) {
      case 'telemetry':
        telemetry.value = message
        break
      case 'channels':
        channelCount.value = message.channelCount
        break
      case 'render-envelope':
        envelope.value = message
        break
      case 'systemview-visible':
        systemViewVisible.value = message
        break
      case 'waveform-batch':
        waveformBatch.value = message
        break
      case 'rtt-lines':
        rttLines.value = message
        break
      case 'rtt-terminal':
        rttTerminal.value = message
        break
      case 'superwatch-metadata':
        superwatchMetadata.value = message
        break
      case 'serial-lines':
        serialLines.value = message
        break
      case 'serial-terminal':
        serialTerminal.value = message
        break
      case 'error':
        error.value = message.message
        break
    }
  }

  const createClient = options.createClient ?? (clientOptions => new StreamClient(clientOptions))
  const client = createClient({
    url: streamUrl(stream),
    token: options.token,
    capacity: options.capacity,
    channelCount: options.channelCount,
    decoderMode: options.decoderMode,
    serializeWorkerFrames: stream === 'systemview',
    onState,
    onWorkerMessage,
  })

  function start(): void {
    error.value = null
    client.start()
  }

  function stop(): void {
    client.stop()
  }

  function reset(): void {
    telemetry.value = null
    envelope.value = null
    systemViewVisible.value = null
    waveformBatch.value = null
    rttLines.value = null
    rttTerminal.value = null
    superwatchMetadata.value = null
    serialLines.value = null
    serialTerminal.value = null
    error.value = null
    client.reset()
  }

  function configure(nextChannelCount: number): void {
    channelCount.value = nextChannelCount
    telemetry.value = null
    envelope.value = null
    waveformBatch.value = null
    rttLines.value = null
    rttTerminal.value = null
    superwatchMetadata.value = null
    serialLines.value = null
    serialTerminal.value = null
    client.configure(options.capacity, nextChannelCount)
  }

  function requestVisibleRange(
    requestId: number,
    start: number,
    end: number,
    pixelWidth: number,
  ): void {
    client.requestVisibleRange(requestId, start, end, pixelWidth)
  }

  if (options.autoStart) start()

  onUnmounted(() => client.dispose())

  return {
    state: readonly(state),
    connected: computed(() => state.value.phase === 'connected'),
    telemetry: readonly(telemetry),
    channelCount: readonly(channelCount),
    envelope: readonly(envelope),
    systemViewVisible: readonly(systemViewVisible),
    waveformBatch: readonly(waveformBatch),
    rttLines: readonly(rttLines),
    rttTerminal: readonly(rttTerminal),
    superwatchMetadata: readonly(superwatchMetadata),
    serialLines: readonly(serialLines),
    serialTerminal: readonly(serialTerminal),
    error: readonly(error),
    start,
    stop,
    reset,
    configure,
    requestVisibleRange,
  }
}
