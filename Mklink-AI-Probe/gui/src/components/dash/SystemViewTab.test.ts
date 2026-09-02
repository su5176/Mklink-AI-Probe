import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick, ref, shallowRef } from 'vue'
import SystemViewTab from './SystemViewTab.vue'
import {
  DESKTOP_SETTINGS_STORAGE_KEY,
  saveDesktopSettings,
  type DesktopSettings,
} from '../../lib/desktopSettings'

class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>()
  get length(): number { return this.values.size }
  clear(): void { this.values.clear() }
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  key(index: number): string | null { return [...this.values.keys()][index] ?? null }
  removeItem(key: string): void { this.values.delete(key) }
  setItem(key: string, value: string): void { this.values.set(key, value) }
}

const mocks = vi.hoisted(() => ({
  dash: {
    state: { __v_isRef: true, value: 'idle' },
    error: { __v_isRef: true, value: null },
    getStatus: vi.fn(), start: vi.fn(), stop: vi.fn(),
    pause: vi.fn(), resume: vi.fn(),
  },
  status: {
    data: { __v_isRef: true, value: [] },
    connect: vi.fn(), disconnect: vi.fn(),
  },
  binary: {
    telemetry: { __v_isRef: true, value: null },
    systemViewVisible: { __v_isRef: true, value: null },
    start: vi.fn(), stop: vi.fn(), reset: vi.fn(), requestVisibleRange: vi.fn(),
  },
  checkConflict: vi.fn(),
  scheduler: {
    start: vi.fn(), stop: vi.fn(), invalidate: vi.fn(),
    recordCollection: vi.fn(), setFrameRate: vi.fn(), dispose: vi.fn(),
    options: null as unknown,
    render: null as ((reasons: ReadonlySet<string>) => void) | null,
  },
  timeline: {
    construct: vi.fn(),
    pauseRendering: vi.fn(), resumeRendering: vi.fn(), renderFrame: vi.fn(),
    setPrefilteredIntervals: vi.fn(),
  },
  importLog: vi.fn(),
  api: {
    findRtt: vi.fn(),
  },
}))

vi.mock('../../composables/useDashboard', () => ({ useDashboard: () => mocks.dash }))
vi.mock('../../composables/useEventSource', () => ({ useEventSource: () => mocks.status }))
vi.mock('../../composables/useBinaryStream', () => ({ useBinaryStream: () => mocks.binary }))
vi.mock('../../composables/useResourceStatus', () => ({
  useResourceStatus: () => ({ checkConflict: mocks.checkConflict }),
}))
vi.mock('../../composables/useMklinkApi', () => ({ useMklinkApi: () => mocks.api }))
vi.mock('../../lib/svTimeline', () => ({
  SvTimeline: class {
    constructor(_roots: unknown, data: unknown) { mocks.timeline.construct(data) }
    setData() {}
    setTickOrigin() {}
    setPrefilteredIntervals = mocks.timeline.setPrefilteredIntervals
    renderFrame = mocks.timeline.renderFrame
    setWindowSize() {}
    pauseRendering = mocks.timeline.pauseRendering
    resumeRendering = mocks.timeline.resumeRendering
    reset() {}
    destroy() {}
  },
}))
vi.mock('../../lib/stream/renderScheduler', () => ({
  AdaptiveFrameRateController: class {
    reset() {}
    observe() { return 60 }
  },
  RenderScheduler: class {
    constructor(
      render: (reasons: ReadonlySet<string>) => void,
      _dependencies: unknown,
      _collectionTelemetry: unknown,
      options: unknown,
    ) {
      mocks.scheduler.render = render
      mocks.scheduler.options = options
    }
    start = mocks.scheduler.start
    stop = mocks.scheduler.stop
    invalidate = mocks.scheduler.invalidate
    recordCollection = mocks.scheduler.recordCollection
    setFrameRate = mocks.scheduler.setFrameRate
    dispose = mocks.scheduler.dispose
  },
}))
vi.mock('../../lib/systemViewImport', () => ({
  importSystemViewJsonl: mocks.importLog,
}))

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

function desktopSettings(overrides: Partial<DesktopSettings> = {}): DesktopSettings {
  return {
    version: 1,
    symbolPath: '',
    symbolDisplayPath: '',
    rttAddress: '0x0008E488',
    rttEncoding: 'utf-8',
    transmitMode: 'text',
    lineEnding: '',
    sendHistory: [],
    ...overrides,
  }
}

describe('SystemViewTab asynchronous lifecycle', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.dash.state = ref('idle') as typeof mocks.dash.state
    mocks.dash.error = ref(null) as typeof mocks.dash.error
    mocks.status.data = shallowRef([]) as typeof mocks.status.data
    mocks.binary.telemetry = shallowRef(null) as typeof mocks.binary.telemetry
    mocks.binary.systemViewVisible = shallowRef(null) as typeof mocks.binary.systemViewVisible
    mocks.dash.stop.mockResolvedValue(undefined)
    mocks.dash.start.mockResolvedValue(true)
    mocks.dash.getStatus.mockResolvedValue({ running: false })
    mocks.checkConflict.mockResolvedValue([])
    mocks.api.findRtt.mockResolvedValue({
      found: true,
      addr: '0x0008E488',
      source: 'binary:rtthread.elf',
    })
    mocks.importLog.mockResolvedValue({ events: 0, skipped: 0, parseErrors: 0 })
    vi.stubGlobal('localStorage', new MemoryStorage())
    localStorage.clear()
    saveDesktopSettings(localStorage, desktopSettings())
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }))
    mocks.scheduler.render = null
  })

  it('searches and persists an RTT address without visiting RTT View', async () => {
    localStorage.setItem(DESKTOP_SETTINGS_STORAGE_KEY, JSON.stringify(desktopSettings({
      symbolPath: 'D:\\project\\rtthread.elf',
      rttAddress: '',
    })))
    mocks.dash.getStatus.mockResolvedValue({ running: false })
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    await flushPromises()

    await wrapper.get('[data-testid="systemview-rtt-search"]').trigger('click')
    await flushPromises()

    expect(mocks.api.findRtt).toHaveBeenCalledWith('D:\\project\\rtthread.elf')
    expect((wrapper.get('[data-testid="systemview-rtt-address"]').element as HTMLInputElement).value)
      .toBe('0x0008E488')
    expect(JSON.parse(localStorage.getItem(DESKTOP_SETTINGS_STORAGE_KEY) ?? '{}').rttAddress)
      .toBe('0x0008E488')
    wrapper.unmount()
  })

  it('does not let an in-flight search overwrite a newer manual address', async () => {
    const pending = deferred<{ found: boolean, addr: string, source: string }>()
    mocks.api.findRtt.mockReturnValueOnce(pending.promise)
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    await flushPromises()

    await wrapper.get('[data-testid="systemview-rtt-search"]').trigger('click')
    await wrapper.get('[data-testid="systemview-rtt-address"]').setValue('0x20003333')
    pending.resolve({ found: true, addr: '0x20001111', source: 'binary:old.axf' })
    await flushPromises()

    expect((wrapper.get('[data-testid="systemview-rtt-address"]').element as HTMLInputElement).value)
      .toBe('0x20003333')
    expect(JSON.parse(localStorage.getItem(DESKTOP_SETTINGS_STORAGE_KEY) ?? '{}').rttAddress)
      .toBe('0x20003333')
    expect(wrapper.text()).not.toContain('binary:old.axf')
    wrapper.unmount()
  })

  it('does not apply a search result after the selected AXF changes', async () => {
    const pending = deferred<{ found: boolean, addr: string, source: string }>()
    mocks.api.findRtt.mockReturnValueOnce(pending.promise)
    saveDesktopSettings(localStorage, desktopSettings({
      symbolPath: 'C:\\firmware\\old.axf',
      rttAddress: '0x20000000',
    }))
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    await flushPromises()

    await wrapper.get('[data-testid="systemview-rtt-search"]').trigger('click')
    saveDesktopSettings(localStorage, desktopSettings({
      symbolPath: 'D:\\build\\next.axf',
      rttAddress: '',
    }))
    pending.resolve({ found: true, addr: '0x20001111', source: 'binary:old.axf' })
    await flushPromises()

    expect((wrapper.get('[data-testid="systemview-rtt-address"]').element as HTMLInputElement).value)
      .toBe('')
    expect(JSON.parse(localStorage.getItem(DESKTOP_SETTINGS_STORAGE_KEY) ?? '{}')).toMatchObject({
      symbolPath: 'D:\\build\\next.axf',
      rttAddress: '',
    })
    expect(wrapper.text()).not.toContain('binary:old.axf')
    wrapper.unmount()
  })

  it('starts with the shared RTT address and SystemView channel', async () => {
    mocks.dash.getStatus
      .mockResolvedValueOnce({ running: false })
      .mockResolvedValue({ running: true, progress_state: 'streaming', stats: { bytes: 64 } })
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    await flushPromises()

    await wrapper.get('.control-toolbar .btn-primary').trigger('click')
    await flushPromises()

    expect(mocks.dash.start).toHaveBeenCalledWith({
      addr: '0x0008E488',
      channel: 1,
      mode: 0,
      search_size: 1024,
    })
    wrapper.unmount()
  })

  it('coalesces visible-range requests while the Worker is still processing one', async () => {
    mocks.dash.getStatus.mockResolvedValue({ running: false })
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    await flushPromises()
    expect(mocks.scheduler.render).not.toBeNull()

    mocks.scheduler.render?.(new Set(['data']))
    mocks.scheduler.render?.(new Set(['data']))
    expect(mocks.binary.requestVisibleRange).toHaveBeenCalledOnce()

    const requestId = mocks.binary.requestVisibleRange.mock.calls[0][0] as number
    publishVisibleEvent(1_000_000, requestId)
    await nextTick()
    expect(mocks.scheduler.invalidate).toHaveBeenCalledWith('data')
    wrapper.unmount()
  })

  it('keeps earlier envelope intervals while the follow viewport advances', async () => {
    mocks.dash.getStatus.mockResolvedValue({ running: false })
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    await flushPromises()

    publishVisibleIntervals(1_000_000, 0, 1_000_000, 100, 200)
    await nextTick()
    publishVisibleIntervals(1_000_000, 0, 2_000_000, 300, 400)
    await nextTick()

    const last = mocks.timeline.setPrefilteredIntervals.mock.calls.at(-1)?.[0] as Array<{ start: number }>
    expect(last.map(interval => interval.start)).toEqual([100, 300])
    wrapper.unmount()
  })

  it('keeps the startup Timeline cadence fixed until the live window is filled', async () => {
    mocks.dash.getStatus.mockResolvedValue({ running: false })
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    await flushPromises()

    expect(mocks.scheduler.options).toEqual({ frameRate: 30, continuous: true })
    mocks.scheduler.render?.(new Set(['data']))
    expect(mocks.scheduler.setFrameRate).toHaveBeenLastCalledWith(30)

    const firstRequestId = mocks.binary.requestVisibleRange.mock.calls.at(-1)?.[0] as number
    publishVisibleEvent(1_000_000, firstRequestId, 1)
    await nextTick()
    mocks.scheduler.render?.(new Set(['data']))
    expect(mocks.scheduler.setFrameRate).toHaveBeenLastCalledWith(30)

    const filledRequestId = mocks.binary.requestVisibleRange.mock.calls.at(-1)?.[0] as number
    publishVisibleEvent(1_000_000, filledRequestId, 10_000_000)
    await nextTick()
    mocks.scheduler.render?.(new Set())
    expect(mocks.scheduler.setFrameRate).toHaveBeenLastCalledWith(60)
    wrapper.unmount()
  })

  it('updates its address when RTT View saves a new shared value', async () => {
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    await flushPromises()

    saveDesktopSettings(localStorage, desktopSettings({ rttAddress: '0x20001A40' }))
    await nextTick()

    expect((wrapper.get('[data-testid="systemview-rtt-address"]').element as HTMLInputElement).value)
      .toBe('0x20001A40')
    wrapper.unmount()
  })

  it('starts and stops durable recording without stopping RTOS Trace', async () => {
    mocks.dash.getStatus.mockResolvedValue({ running: false })
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockReset()
    fetchMock.mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/recording/start')) return {
        ok: true,
        json: async () => ({
          recording: true,
          recording_path: 'D:\\logs\\trace.jsonl',
          recording_summary_path: 'D:\\logs\\trace-summary.txt',
        }),
      } as Response
      if (url.endsWith('/recording/stop')) return {
        ok: true,
        json: async () => ({
          recording: false,
          recording_path: 'D:\\logs\\trace.jsonl',
          recording_summary_path: 'D:\\logs\\trace-summary.txt',
        }),
      } as Response
      return { ok: true, json: async () => ({ logs: [] }) } as Response
    })
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    await flushPromises()
    mocks.dash.state.value = 'running'
    await nextTick()

    const button = wrapper.get('[data-testid="systemview-recording"]')
    await button.trigger('click')
    await flushPromises()
    expect(fetchMock).toHaveBeenCalledWith('/api/dash/systemview/recording/start', { method: 'POST' })
    expect(button.text()).toContain('停止保存')
    expect(mocks.dash.stop).not.toHaveBeenCalled()

    await button.trigger('click')
    await flushPromises()
    expect(fetchMock).toHaveBeenCalledWith('/api/dash/systemview/recording/stop', { method: 'POST' })
    expect(button.text()).toContain('实时保存')
    expect(mocks.dash.stop).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('does not start either transport when getStatus resolves after unmount', async () => {
    const status = deferred<{ running: boolean }>()
    mocks.dash.getStatus.mockReturnValue(status.promise)
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })

    wrapper.unmount()
    status.resolve({ running: true })
    await flushPromises()

    expect(mocks.dash.start).not.toHaveBeenCalled()
    expect(mocks.status.connect).not.toHaveBeenCalled()
    expect(mocks.binary.start).not.toHaveBeenCalled()
  })

  it('restores task metadata when attaching to an already running trace', async () => {
    mocks.dash.getStatus.mockResolvedValue({
      running: true,
      synced: true,
      cpu_freq: 360_000_000,
      cpu_freq_source: 'INIT',
      dropped_bytes: 8,
      dropped_packets: 0,
      task_names: {
        528400: 'task3',
        530576: 'task1',
        532752: 'task2',
        535216: 'Tmr Svc',
      },
    })
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    await flushPromises()

    const healthText = wrapper.find('.sv-health-grid').text()
    expect(healthText).toContain('Tasks4')
    expect(wrapper.text()).toContain('task1')
    expect(wrapper.text()).toContain('task2')
    expect(wrapper.text()).toContain('task3')
    expect(wrapper.text()).toContain('Tmr Svc')
    expect(mocks.status.connect).toHaveBeenCalledOnce()
    expect(mocks.binary.start).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('shows target overflow separately from runtime stream drops', async () => {
    mocks.dash.getStatus.mockResolvedValue({ running: false })
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    await flushPromises()

    mocks.status.data.value = [{
      _streamSeq: 1,
      target_overflow_events: 1,
      target_drop_count: 3,
      target_dropped_packets_since_baseline: 3,
      dropped_bytes: 0,
      dropped_packets: 0,
    }] as never[]
    await nextTick()

    const health = wrapper.get('.sv-health-grid')
    expect(health.text()).toContain('Target Overflow1')
    expect(health.text()).toContain('Runtime Drop0')
    const overflowCard = health.findAll('.sv-health-card')
      .find(card => card.text().includes('Target Overflow'))
    expect(overflowCard?.classes()).toContain('warn')
    wrapper.unmount()
  })

  it('shows one visible recovery notice and resets the failed first-start burst', async () => {
    mocks.dash.getStatus.mockResolvedValue({ running: false })
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    await flushPromises()
    const resetsBeforeRetry = mocks.binary.reset.mock.calls.length

    mocks.status.data.value = [{
      _streamSeq: 1,
      event: 'status',
      connection_generation: 3,
      session_generation: 7,
      auto_retry_count: 1,
      auto_retry_reason: 'first_start_data_then_idle',
      stats: { events: 0 },
    }] as never[]
    await nextTick()

    const recovery = wrapper.get('[data-testid="systemview-auto-retry"]')
    expect(recovery.text()).toContain('首次启动已自动重试')
    expect(recovery.get('b').attributes('title')).toBe('first_start_data_then_idle')
    expect(mocks.binary.reset).toHaveBeenCalledTimes(resetsBeforeRetry + 1)

    mocks.status.data.value = [{
      _streamSeq: 2,
      event: 'status',
      connection_generation: 3,
      session_generation: 7,
      auto_retry_count: 1,
      auto_retry_reason: 'first_start_data_then_idle',
      stats: { events: 10 },
    }] as never[]
    await nextTick()
    expect(mocks.binary.reset).toHaveBeenCalledTimes(resetsBeforeRetry + 1)

    mocks.status.data.value = [{
      _streamSeq: 3,
      event: 'status',
      connection_generation: 3,
      session_generation: 7,
      auto_retry_count: 2,
      auto_retry_reason: 'first_start_data_then_idle',
      stats: { events: 0 },
    }] as never[]
    await nextTick()
    expect(recovery.text()).toContain('首次启动已自动重试 2 次')
    expect(mocks.binary.reset).toHaveBeenCalledTimes(resetsBeforeRetry + 2)
    wrapper.unmount()
  })

  it('does not connect when a running-trace start resolves after unmount', async () => {
    const started = deferred<void>()
    mocks.dash.getStatus.mockResolvedValue({ running: true })
    mocks.dash.start.mockReturnValue(started.promise)
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    await flushPromises()
    expect(mocks.dash.start).toHaveBeenCalledOnce()

    wrapper.unmount()
    started.resolve()
    await flushPromises()

    expect(mocks.status.connect).not.toHaveBeenCalled()
    expect(mocks.binary.start).not.toHaveBeenCalled()
  })

  it('does not arm delayed transports when a user start resolves after unmount', async () => {
    const started = deferred<void>()
    mocks.dash.getStatus.mockResolvedValue({ running: false })
    mocks.dash.start.mockReturnValue(started.promise)
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    await flushPromises()
    await wrapper.get('.btn-primary').trigger('click')
    await flushPromises()
    expect(mocks.dash.start).toHaveBeenCalledOnce()

    wrapper.unmount()
    started.resolve()
    await flushPromises()

    expect(mocks.status.connect).not.toHaveBeenCalled()
    expect(mocks.binary.start).not.toHaveBeenCalled()
  })

  it('pauses only timeline rendering while Worker acquisition remains active', async () => {
    mocks.dash.getStatus.mockResolvedValue({ running: false })
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    await flushPromises()
    mocks.dash.state.value = 'running'
    await nextTick()

    await wrapper.get('.control-toolbar .btn:not(.btn-danger)').trigger('click')
    expect(mocks.scheduler.stop).toHaveBeenCalled()
    expect(mocks.timeline.pauseRendering).toHaveBeenCalled()
    expect(mocks.dash.pause).not.toHaveBeenCalled()
    expect(mocks.binary.stop).not.toHaveBeenCalled()
    publishVisibleEvent(72_000_000)
    await nextTick()
    expect(mocks.timeline.setPrefilteredIntervals).not.toHaveBeenCalled()

    await wrapper.get('.control-toolbar .btn-primary').trigger('click')
    expect(mocks.scheduler.start).toHaveBeenCalledTimes(2)
    expect(mocks.scheduler.invalidate).toHaveBeenCalledWith('data')
    expect(mocks.timeline.resumeRendering).toHaveBeenCalled()
    expect(mocks.dash.resume).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('constructs a replacement timeline already paused when the CPU clock changes', async () => {
    mocks.dash.getStatus.mockResolvedValue({ running: false })
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    await flushPromises()
    mocks.dash.state.value = 'running'
    await nextTick()

    await wrapper.get('.control-toolbar .btn:not(.btn-danger)').trigger('click')
    publishVisibleEvent(72_000_000)
    await nextTick()

    expect(mocks.timeline.construct).toHaveBeenLastCalledWith(
      expect.objectContaining({ renderPaused: true }),
    )
    wrapper.unmount()
  })

  it('clears the timeline pause across stop and start lifecycle resets', async () => {
    mocks.dash.getStatus.mockResolvedValue({ running: false })
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    await flushPromises()
    mocks.dash.state.value = 'running'
    await nextTick()

    await wrapper.get('.control-toolbar .btn:not(.btn-danger)').trigger('click')
    await wrapper.get('.control-toolbar .btn-danger').trigger('click')
    await flushPromises()
    expect(mocks.timeline.resumeRendering).toHaveBeenCalledTimes(1)
    expect(mocks.scheduler.start).toHaveBeenCalledTimes(2)

    mocks.dash.state.value = 'idle'
    await nextTick()
    await wrapper.get('.control-toolbar .btn-primary').trigger('click')
    await flushPromises()
    expect(mocks.timeline.resumeRendering).toHaveBeenCalledTimes(2)
    expect(mocks.scheduler.start).toHaveBeenCalledTimes(3)
    wrapper.unmount()
  })

  it('resumes rendering before importing an offline log from a paused trace', async () => {
    mocks.dash.getStatus.mockResolvedValue({ running: false })
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    await flushPromises()
    mocks.dash.state.value = 'running'
    await nextTick()

    await wrapper.get('.control-toolbar .btn:not(.btn-danger)').trigger('click')
    const input = wrapper.get('input[type="file"]')
    Object.defineProperty(input.element, 'files', {
      configurable: true,
      value: [new File([''], 'trace.jsonl', { type: 'application/x-ndjson' })],
    })
    await input.trigger('change')
    await flushPromises()

    expect(mocks.timeline.resumeRendering).toHaveBeenCalled()
    expect(mocks.scheduler.start).toHaveBeenCalledTimes(2)
    expect(mocks.scheduler.invalidate).toHaveBeenCalledWith('data')
    wrapper.unmount()
  })

  it('streams an imported JSONL file through offline replay controls', async () => {
    mocks.dash.getStatus.mockResolvedValue({ running: false })
    mocks.importLog.mockImplementation(async (options: any) => {
      await options.onSession?.({ cpu_freq: 1_000_000 })
      await options.onProgress?.(128)
      await options.onBatch([{ kind: 'task_start_exec', task_id: 1, t_us: 10, t_ticks: 10 }])
      return { events: 1, skipped: 0, parseErrors: 0 }
    })
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    await flushPromises()
    const input = wrapper.get('input[type="file"]')
    const file = new File(['x'.repeat(128)], 'trace.jsonl', { type: 'application/x-ndjson' })
    Object.defineProperty(input.element, 'files', { configurable: true, value: [file] })

    await input.trigger('change')
    await flushPromises()

    expect(mocks.importLog).toHaveBeenCalledWith(expect.objectContaining({ batchSize: 500 }))
    expect(wrapper.get('[data-testid="systemview-replay-controls"]').text()).toContain('重新播放')
    expect(wrapper.get('.sv-replay-progress').text()).toBe('100.0%')
    expect(wrapper.text()).toContain('回放完成 1')
    wrapper.unmount()
  })
})

function publishVisibleEvent(cpuFreq: number, requestId = 0, latestTime = 1) {
  mocks.status.data.value = cpuFreq > 0 ? [{ _streamSeq: 1, cpu_freq: cpuFreq }] as never[] : []
  mocks.binary.systemViewVisible.value = {
    type: 'systemview-visible',
    requestId,
    intervalCount: 0,
    candidateIntervalCount: 0,
    eventCount: 1,
    latestTime,
    tickOrigin: 9007199254740992n,
    taskIds: new Uint32Array().buffer,
    starts: new Float64Array().buffer,
    ends: new Float64Array().buffer,
    startTicks: new BigUint64Array().buffer,
    endTicks: new BigUint64Array().buffer,
    events: [{
      kind: 'task_start_exec',
      task_id: 1,
      t_ticks: 9007199254740993n,
      t_ticks_exact: '9007199254740993',
      t_relative: 1,
    }],
  } as never
}

function publishVisibleIntervals(
  cpuFreq: number,
  requestId: number,
  latestTime: number,
  start: number,
  end: number,
) {
  mocks.status.data.value = [{ _streamSeq: requestId, cpu_freq: cpuFreq }] as never[]
  mocks.binary.systemViewVisible.value = {
    type: 'systemview-visible',
    requestId,
    intervalCount: 1,
    candidateIntervalCount: 1,
    eventCount: 0,
    latestTime,
    tickOrigin: 0n,
    taskIds: new Uint32Array([1]).buffer,
    contextTypes: new Uint8Array([1]).buffer,
    starts: new Float64Array([start]).buffer,
    ends: new Float64Array([end]).buffer,
    startTicks: new BigUint64Array([BigInt(start)]).buffer,
    endTicks: new BigUint64Array([BigInt(end)]).buffer,
    events: [],
    contexts: [],
  } as never
}

describe('SystemViewTab event time units', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.dash.state = ref('idle') as typeof mocks.dash.state
    mocks.dash.error = ref(null) as typeof mocks.dash.error
    mocks.status.data = shallowRef([]) as typeof mocks.status.data
    mocks.binary.telemetry = shallowRef(null) as typeof mocks.binary.telemetry
    mocks.binary.systemViewVisible = shallowRef(null) as typeof mocks.binary.systemViewVisible
    mocks.dash.getStatus.mockResolvedValue({ running: false })
    mocks.dash.stop.mockResolvedValue(undefined)
    mocks.checkConflict.mockResolvedValue([])
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }))
  })

  it('shows formatted seconds when CPU frequency is known', async () => {
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    publishVisibleEvent(1_000_000)
    await nextTick()

    expect(wrapper.find('.sv-events-table tbody tr td:nth-child(2)').text()).toBe('0.000001000s')
    wrapper.unmount()
  })

  it('shows the exact tick string when CPU frequency is unknown', async () => {
    const wrapper = mount(SystemViewTab, { props: { deviceConnected: true } })
    publishVisibleEvent(0)
    await nextTick()

    expect(wrapper.find('.sv-events-table tbody tr td:nth-child(2)').text().replaceAll(',', ''))
      .toBe('9007199254740993 tk')
    wrapper.unmount()
  })
})
