import type { DecoderMode, WorkerInput, WorkerOutput } from '../../workers/streamDecoder.worker'

export type StreamClientPhase = 'stopped' | 'connecting' | 'connected' | 'reconnecting' | 'error'

export interface StreamClientState {
  readonly phase: StreamClientPhase
  readonly reconnectDelayMs?: number
  readonly error?: string
}

export interface StreamClientOptions {
  readonly url: string
  readonly token?: string
  readonly capacity: number
  readonly channelCount: number
  readonly decoderMode?: DecoderMode
  /** Serialize frame delivery so control/range requests can interleave. */
  readonly serializeWorkerFrames?: boolean
  readonly reconnectBaseMs?: number
  readonly reconnectMaxMs?: number
  readonly createWebSocket?: (url: string) => WebSocket
  readonly worker?: Worker
  readonly createWorker?: () => Worker
  readonly onState?: (state: StreamClientState) => void
  readonly onWorkerMessage?: (message: WorkerOutput) => void
}

const DEFAULT_RECONNECT_BASE_MS = 250
const DEFAULT_RECONNECT_MAX_MS = 8_000

function requirePositiveInteger(name: string, value: number): void {
  if (!Number.isInteger(value) || value <= 0) {
    throw new RangeError(`${name} must be a positive integer`)
  }
}

function validateOptions(options: StreamClientOptions): void {
  if (typeof options.url !== 'string' || options.url.length === 0) {
    throw new TypeError('url must be a non-empty string')
  }
  requirePositiveInteger('capacity', options.capacity)
  requirePositiveInteger('channelCount', options.channelCount)
  const base = options.reconnectBaseMs ?? DEFAULT_RECONNECT_BASE_MS
  const maximum = options.reconnectMaxMs ?? DEFAULT_RECONNECT_MAX_MS
  if (!Number.isFinite(base) || !Number.isFinite(maximum) || base <= 0 || maximum < base) {
    throw new RangeError('reconnect delay bounds are invalid')
  }
}

export class StreamClient {
  private readonly options: StreamClientOptions
  private readonly worker: Worker
  private readonly createWebSocket: (url: string) => WebSocket
  private readonly reconnectBaseMs: number
  private readonly reconnectMaxMs: number
  private readonly serializeWorkerFrames: boolean
  private socket: WebSocket | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private reconnectAttempt = 0
  private shouldRun = false
  private disposed = false
  private awaitingReadyFrame = false
  private connectionGeneration = 0
  private currentGeneration = 0
  private nextFrameTicket = 0
  private readonly pendingReadyTickets = new Set<number>()
  private readonly workerFrameQueue: Array<{
    buffer: ArrayBuffer
    connectionGeneration: number
    frameTicket: number
  }> = []
  private workerFrameInFlight: number | null = null

  constructor(options: StreamClientOptions) {
    validateOptions(options)
    this.options = options
    this.worker = options.worker ?? options.createWorker?.() ?? new Worker(
      new URL('../../workers/streamDecoder.worker.ts', import.meta.url),
      { type: 'module' },
    )
    this.createWebSocket = options.createWebSocket ?? (url => new WebSocket(url))
    this.reconnectBaseMs = options.reconnectBaseMs ?? DEFAULT_RECONNECT_BASE_MS
    this.reconnectMaxMs = options.reconnectMaxMs ?? DEFAULT_RECONNECT_MAX_MS
    this.serializeWorkerFrames = options.serializeWorkerFrames === true
    this.worker.onmessage = event => {
      const message = event.data as WorkerOutput
      if (message.type === 'telemetry') {
        const currentAcknowledgement =
          message.acceptedConnectionGeneration === this.currentGeneration
          && message.acceptedFrameTicket !== null
          && this.pendingReadyTickets.delete(message.acceptedFrameTicket)
        if (this.awaitingReadyFrame && currentAcknowledgement) {
          this.reconnectAttempt = 0
          this.awaitingReadyFrame = false
          this.pendingReadyTickets.clear()
        }
        if (this.serializeWorkerFrames && message.acceptedFrameTicket === this.workerFrameInFlight) {
          this.workerFrameInFlight = null
          this.flushWorkerFrame()
        }
      } else if (
        message.type === 'error'
        && message.connectionGeneration === this.currentGeneration
        && message.frameTicket !== undefined
      ) {
        this.pendingReadyTickets.delete(message.frameTicket)
        if (this.serializeWorkerFrames && message.frameTicket === this.workerFrameInFlight) {
          this.workerFrameInFlight = null
          this.flushWorkerFrame()
        }
      }
      options.onWorkerMessage?.(message)
    }
    this.worker.postMessage({
      type: 'configure',
      capacity: options.capacity,
      channelCount: options.channelCount,
      decoderMode: options.decoderMode,
    } satisfies WorkerInput)
  }

  start(): void {
    if (this.disposed) throw new Error('stream client is disposed')
    if (this.shouldRun) return
    this.shouldRun = true
    this.reconnectAttempt = 0
    this.connect()
  }

  stop(): void {
    if (this.disposed) return
    this.shouldRun = false
    this.awaitingReadyFrame = false
    this.currentGeneration = 0
    this.pendingReadyTickets.clear()
    this.clearWorkerFrames()
    this.clearReconnectTimer()
    const socket = this.socket
    this.socket = null
    if (socket) {
      this.detach(socket)
      socket.close()
    }
    this.emitState({ phase: 'stopped' })
  }

  reset(): void {
    if (this.disposed) return
    this.worker.postMessage({ type: 'reset' } satisfies WorkerInput)
  }

  configure(capacity: number, channelCount: number): void {
    if (this.disposed) return
    requirePositiveInteger('capacity', capacity)
    requirePositiveInteger('channelCount', channelCount)
    this.worker.postMessage({
      type: 'configure', capacity, channelCount,
      decoderMode: this.options.decoderMode,
    } satisfies WorkerInput)
  }

  requestVisibleRange(requestId: number, start: number, end: number, pixelWidth: number): void {
    if (this.disposed) return
    this.worker.postMessage({
      type: 'visible-range', requestId, start, end, pixelWidth,
    } satisfies WorkerInput)
  }

  dispose(): void {
    if (this.disposed) return
    this.stop()
    this.disposed = true
    this.worker.onmessage = null
    this.worker.terminate()
  }

  private connect(): void {
    if (!this.shouldRun || this.disposed) return
    this.emitState({ phase: this.reconnectAttempt === 0 ? 'connecting' : 'reconnecting' })
    let socket: WebSocket
    try {
      socket = this.createWebSocket(this.options.url)
    } catch (error) {
      this.emitState({
        phase: 'error',
        error: error instanceof Error ? error.message : String(error),
      })
      if (this.shouldRun) this.scheduleReconnect()
      return
    }
    const generation = ++this.connectionGeneration
    this.currentGeneration = generation
    this.nextFrameTicket = 0
    this.pendingReadyTickets.clear()
    this.clearWorkerFrames()
    this.socket = socket
    socket.binaryType = 'arraybuffer'
    socket.onopen = () => {
      if (socket !== this.socket || !this.shouldRun) return
      this.awaitingReadyFrame = true
      if (this.options.token) {
        // Task 3 accepts token at top level or in params on the first JSON text
        // frame. params.token matches the existing RPC authentication shape.
        socket.send(JSON.stringify({ params: { token: this.options.token } }))
      }
      this.emitState({ phase: 'connected' })
    }
    socket.onmessage = event => {
      if (socket !== this.socket || !this.shouldRun) return
      if (!(event.data instanceof ArrayBuffer)) {
        this.emitState({ phase: 'error', error: 'binary stream returned a non-ArrayBuffer message' })
        return
      }
      const buffer = event.data
      const frameTicket = ++this.nextFrameTicket
      this.pendingReadyTickets.add(frameTicket)
      const frame = { buffer, connectionGeneration: generation, frameTicket }
      if (this.serializeWorkerFrames) {
        this.workerFrameQueue.push(frame)
        this.flushWorkerFrame()
      } else {
        this.worker.postMessage({
          type: 'frame',
          ...frame,
        } satisfies WorkerInput, [buffer])
      }
    }
    socket.onerror = () => {
      if (socket === this.socket && this.shouldRun) {
        this.emitState({ phase: 'error', error: 'binary stream WebSocket error' })
      }
    }
    socket.onclose = () => {
      if (socket !== this.socket) return
      this.socket = null
      this.awaitingReadyFrame = false
      this.pendingReadyTickets.clear()
      this.clearWorkerFrames()
      if (this.shouldRun) this.scheduleReconnect()
    }
  }

  private scheduleReconnect(): void {
    this.clearReconnectTimer()
    const delay = Math.min(
      this.reconnectMaxMs,
      this.reconnectBaseMs * (2 ** this.reconnectAttempt),
    )
    this.reconnectAttempt += 1
    this.emitState({ phase: 'reconnecting', reconnectDelayMs: delay })
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this.connect()
    }, delay)
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
  }

  private detach(socket: WebSocket): void {
    socket.onopen = null
    socket.onmessage = null
    socket.onerror = null
    socket.onclose = null
  }

  private flushWorkerFrame(): void {
    if (!this.serializeWorkerFrames || this.workerFrameInFlight !== null) return
    const frame = this.workerFrameQueue.shift()
    if (!frame) return
    this.workerFrameInFlight = frame.frameTicket
    this.worker.postMessage({
      type: 'frame',
      ...frame,
    } satisfies WorkerInput, [frame.buffer])
  }

  private clearWorkerFrames(): void {
    this.workerFrameQueue.length = 0
    this.workerFrameInFlight = null
  }

  private emitState(state: StreamClientState): void {
    this.options.onState?.(state)
  }
}
