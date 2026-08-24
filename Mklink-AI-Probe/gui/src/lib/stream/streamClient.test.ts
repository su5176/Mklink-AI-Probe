import { afterEach, describe, expect, it, vi } from 'vitest'
import { StreamClient, type StreamClientState } from './streamClient'

class FakeSocket {
  static readonly OPEN = 1
  binaryType = ''
  readyState = 0
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  readonly sent: string[] = []
  closeCalls = 0

  constructor(readonly url: string) {}

  open() {
    this.readyState = FakeSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  send(value: string) {
    this.sent.push(value)
  }

  close() {
    this.closeCalls += 1
    this.readyState = 3
  }

  emitClose() {
    this.readyState = 3
    this.onclose?.(new CloseEvent('close'))
  }
}

function setup(token?: string, serializeWorkerFrames = false) {
  const sockets: FakeSocket[] = []
  const worker = {
    postMessage: vi.fn(),
    terminate: vi.fn(),
    onmessage: null as ((event: MessageEvent) => void) | null,
  }
  const states: StreamClientState[] = []
  const outputs: unknown[] = []
  const client = new StreamClient({
    url: 'ws://localhost/ws/streams/vofa',
    token,
    capacity: 100,
    channelCount: 2,
    reconnectBaseMs: 100,
    reconnectMaxMs: 400,
    createWebSocket: url => {
      const socket = new FakeSocket(url)
      sockets.push(socket)
      return socket as unknown as WebSocket
    },
    worker: worker as unknown as Worker,
    serializeWorkerFrames,
    onState: state => states.push(state),
    onWorkerMessage: message => outputs.push(message),
  })
  return { client, sockets, worker, states, outputs }
}

afterEach(() => vi.useRealTimers())

describe('StreamClient', () => {
  it('uses arraybuffer frames, transfers ownership to the worker, and authenticates first', () => {
    const { client, sockets, worker } = setup('secret')
    client.start()
    const socket = sockets[0]
    expect(socket.binaryType).toBe('arraybuffer')
    socket.open()
    expect(socket.sent).toEqual([JSON.stringify({ params: { token: 'secret' } })])

    const buffer = new ArrayBuffer(16)
    socket.onmessage?.(new MessageEvent('message', { data: buffer }))
    expect(worker.postMessage).toHaveBeenNthCalledWith(1, {
      type: 'configure', capacity: 100, channelCount: 2,
    })
    expect(worker.postMessage).toHaveBeenNthCalledWith(2, {
      type: 'frame', buffer, connectionGeneration: 1, frameTicket: 1,
    }, [buffer])
  })

  it('serializes frames when requested so range work can interleave', () => {
    const { client, sockets, worker } = setup(undefined, true)
    client.start()
    sockets[0].open()
    const first = new ArrayBuffer(16)
    const second = new ArrayBuffer(16)
    sockets[0].onmessage?.(new MessageEvent('message', { data: first }))
    sockets[0].onmessage?.(new MessageEvent('message', { data: second }))
    expect(worker.postMessage).toHaveBeenCalledTimes(2)
    client.requestVisibleRange(9, 0, 100, 640)
    expect(worker.postMessage).toHaveBeenLastCalledWith({
      type: 'visible-range', requestId: 9, start: 0, end: 100, pixelWidth: 640,
    })
    worker.onmessage?.(new MessageEvent('message', { data: {
      type: 'telemetry', acceptedFrames: 1, bufferedSamples: 1,
      transportDroppedBatches: 0, backendDroppedBatches: 0,
      backendDroppedItems: 0, backendDroppedBytes: 0, lastSequence: 1n,
      acceptedConnectionGeneration: 1, acceptedFrameTicket: 1,
    } }))
    expect(worker.postMessage).toHaveBeenCalledTimes(4)
  })

  it('backs off across open-then-immediate-close loops until valid data arrives', () => {
    vi.useFakeTimers()
    const { client, sockets, states } = setup()
    client.start()
    sockets[0].open()
    sockets[0].emitClose()
    expect(states.at(-1)).toMatchObject({ phase: 'reconnecting', reconnectDelayMs: 100 })
    vi.advanceTimersByTime(100)
    expect(sockets).toHaveLength(2)
    sockets[1].open()
    sockets[1].emitClose()
    expect(states.at(-1)).toMatchObject({ phase: 'reconnecting', reconnectDelayMs: 200 })
    vi.advanceTimersByTime(199)
    expect(sockets).toHaveLength(2)
    vi.advanceTimersByTime(1)
    expect(sockets).toHaveLength(3)
    sockets[2].open()
    sockets[2].emitClose()
    expect(states.at(-1)).toMatchObject({ phase: 'reconnecting', reconnectDelayMs: 400 })
    vi.advanceTimersByTime(400)
    expect(sockets).toHaveLength(4)
    sockets[3].open()
    sockets[3].emitClose()
    expect(states.at(-1)).toMatchObject({ phase: 'reconnecting', reconnectDelayMs: 400 })
    vi.advanceTimersByTime(400)
    expect(sockets).toHaveLength(5)
  })

  it('resets reconnect backoff only after the worker confirms a valid frame', () => {
    vi.useFakeTimers()
    const { client, sockets, worker, states } = setup()
    client.start()
    sockets[0].open()
    sockets[0].emitClose()
    vi.advanceTimersByTime(100)
    sockets[1].open()
    sockets[1].emitClose()
    vi.advanceTimersByTime(200)
    sockets[2].open()

    const buffer = new ArrayBuffer(36)
    sockets[2].onmessage?.(new MessageEvent('message', { data: buffer }))
    worker.onmessage?.(new MessageEvent('message', { data: {
      type: 'telemetry', bufferedSamples: 0, transportDroppedBatches: 0,
      backendDroppedBatches: 0, backendDroppedItems: 0, backendDroppedBytes: 0,
      lastSequence: null, acceptedFrames: 1,
      acceptedConnectionGeneration: 3, acceptedFrameTicket: 1,
    } }))
    sockets[2].emitClose()

    expect(states.at(-1)).toMatchObject({ phase: 'reconnecting', reconnectDelayMs: 100 })
  })

  it('ignores a delayed accepted acknowledgement from the previous socket generation', () => {
    vi.useFakeTimers()
    const { client, sockets, worker, states } = setup()
    client.start()
    const socket0 = sockets[0]
    socket0.open()
    const oldBuffer = new ArrayBuffer(36)
    socket0.onmessage?.(new MessageEvent('message', { data: oldBuffer }))
    expect(worker.postMessage).toHaveBeenLastCalledWith({
      type: 'frame',
      buffer: oldBuffer,
      connectionGeneration: 1,
      frameTicket: 1,
    }, [oldBuffer])

    socket0.emitClose()
    vi.advanceTimersByTime(100)
    const socket1 = sockets[1]
    socket1.open()
    const postsBeforeStaleData = worker.postMessage.mock.calls.length
    socket0.onmessage?.(new MessageEvent('message', { data: new ArrayBuffer(36) }))
    expect(worker.postMessage).toHaveBeenCalledTimes(postsBeforeStaleData)
    worker.onmessage?.(new MessageEvent('message', { data: {
      type: 'telemetry', acceptedFrames: 1, bufferedSamples: 0,
      transportDroppedBatches: 0, backendDroppedBatches: 0,
      backendDroppedItems: 0, backendDroppedBytes: 0, lastSequence: null,
      acceptedConnectionGeneration: 1, acceptedFrameTicket: 1,
    } }))
    socket1.emitClose()

    expect(states.at(-1)).toMatchObject({ phase: 'reconnecting', reconnectDelayMs: 200 })
  })

  it('uses a new generation and resets frame tickets after explicit stop and restart', () => {
    const { client, sockets, worker } = setup()
    client.start()
    sockets[0].open()
    client.stop()
    client.start()
    sockets[1].open()
    const buffer = new ArrayBuffer(36)
    sockets[1].onmessage?.(new MessageEvent('message', { data: buffer }))

    expect(worker.postMessage).toHaveBeenLastCalledWith({
      type: 'frame', buffer, connectionGeneration: 2, frameTicket: 1,
    }, [buffer])
  })

  it('does not reconnect after explicit stop', () => {
    vi.useFakeTimers()
    const { client, sockets, states } = setup()
    client.start()
    sockets[0].emitClose()
    client.stop()
    vi.runAllTimers()
    expect(sockets).toHaveLength(1)
    expect(states.at(-1)).toMatchObject({ phase: 'stopped' })
  })

  it('disposes the socket and worker exactly once and forwards worker messages', () => {
    const { client, sockets, worker, outputs } = setup()
    client.start()
    worker.onmessage?.(new MessageEvent('message', { data: { type: 'channels', channelCount: 2 } }))
    expect(outputs).toEqual([{ type: 'channels', channelCount: 2 }])

    client.dispose()
    client.dispose()
    expect(sockets[0].closeCalls).toBe(1)
    expect(worker.terminate).toHaveBeenCalledTimes(1)
    expect(() => client.start()).toThrow(/disposed/)
  })

  it('rejects non-ArrayBuffer messages instead of posting malformed frames', () => {
    const { client, sockets, worker, states } = setup()
    client.start()
    sockets[0].open()
    sockets[0].onmessage?.(new MessageEvent('message', { data: 'not binary' }))
    expect(worker.postMessage).toHaveBeenCalledTimes(1)
    expect(states.at(-1)).toMatchObject({ phase: 'error' })
  })

  it('recovers from synchronous WebSocket factory failures with controlled backoff', () => {
    vi.useFakeTimers()
    const worker = {
      postMessage: vi.fn(), terminate: vi.fn(), onmessage: null,
    }
    const socket = new FakeSocket('ws://localhost/ws/streams/vofa')
    const createWebSocket = vi.fn()
      .mockImplementationOnce(() => { throw new Error('factory one') })
      .mockImplementationOnce(() => { throw new Error('factory two') })
      .mockReturnValue(socket as unknown as WebSocket)
    const states: StreamClientState[] = []
    const client = new StreamClient({
      url: socket.url, capacity: 10, channelCount: 1,
      reconnectBaseMs: 100, reconnectMaxMs: 400,
      worker: worker as unknown as Worker,
      createWebSocket,
      onState: state => states.push(state),
    })

    expect(() => client.start()).not.toThrow()
    expect(states).toContainEqual({ phase: 'error', error: 'factory one' })
    expect(states.at(-1)).toMatchObject({ phase: 'reconnecting', reconnectDelayMs: 100 })
    vi.advanceTimersByTime(100)
    expect(states).toContainEqual({ phase: 'error', error: 'factory two' })
    vi.advanceTimersByTime(200)
    expect(createWebSocket).toHaveBeenCalledTimes(3)

    socket.emitClose()
    client.stop()
    vi.runAllTimers()
    expect(createWebSocket).toHaveBeenCalledTimes(3)
  })

  it('validates pure options before constructing a worker', () => {
    const createWorker = vi.fn()
    expect(() => new StreamClient({
      url: 'ws://localhost/ws/streams/vofa',
      capacity: 10,
      channelCount: 1,
      reconnectBaseMs: 0,
      reconnectMaxMs: 400,
      createWorker,
    })).toThrow(/reconnect/)
    expect(createWorker).not.toHaveBeenCalled()

    expect(() => new StreamClient({
      url: 'ws://localhost/ws/streams/vofa',
      capacity: 0,
      channelCount: 1,
      createWorker,
    })).toThrow(/capacity/)
    expect(createWorker).not.toHaveBeenCalled()
  })
})
