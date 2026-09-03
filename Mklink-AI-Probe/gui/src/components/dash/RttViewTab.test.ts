import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref, shallowRef } from 'vue'
import { nextTick } from 'vue'
import { saveDesktopSettings, type DesktopSettings } from '../../lib/desktopSettings'
import { StreamType } from '../../lib/stream/protocol'
import { StreamDecoder } from '../../workers/streamDecoder.worker'
import RttViewTab from './RttViewTab.vue'

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
  useBinaryStream: vi.fn(),
  binary: {
    rttLines: null as any,
    rttTerminal: null as any,
    waveformBatch: null as any,
    envelope: null as any,
    telemetry: null as any,
    state: null as any,
    error: null as any,
    start: vi.fn(), stop: vi.fn(), reset: vi.fn(), configure: vi.fn(),
    requestVisibleRange: vi.fn(),
  },
  terminalBinary: {
    rttLines: null as any,
    rttTerminal: null as any,
    waveformBatch: null as any,
    envelope: null as any,
    telemetry: null as any,
    state: null as any,
    error: null as any,
    start: vi.fn(), stop: vi.fn(), reset: vi.fn(), configure: vi.fn(),
    requestVisibleRange: vi.fn(),
  },
  downloadTextFile: vi.fn(),
  dash: {
    state: null as any, error: null as any,
    start: vi.fn(), stop: vi.fn(), pause: vi.fn(), resume: vi.fn(),
  },
  checkConflict: vi.fn(),
  api: {
    findRtt: vi.fn(),
    writeRtt: vi.fn(),
    setRttEncoding: vi.fn(),
  },
  status: { running: false, numeric_channels: [], down_buffers: [] } as Record<string, unknown>,
  scheduler: {
    render: null as null | (() => void),
    start: vi.fn(), stop: vi.fn(), invalidate: vi.fn(),
    recordCollection: vi.fn(), dispose: vi.fn(),
  },
}))

vi.mock('../../composables/useBinaryStream', () => ({ useBinaryStream: mocks.useBinaryStream }))
vi.mock('../../lib/downloadTextFile', () => ({
  downloadTextFile: mocks.downloadTextFile,
  timestampedLogName: (prefix: string) => `${prefix}-test.log`,
}))
vi.mock('../../composables/useDashboard', () => ({ useDashboard: () => mocks.dash }))
vi.mock('../../composables/useEventSource', () => ({
  useEventSource: () => { throw new Error('RTT high-rate SSE must not be constructed') },
}))
vi.mock('../../composables/useResourceStatus', () => ({
  useResourceStatus: () => ({ checkConflict: mocks.checkConflict }),
}))
vi.mock('../../composables/useMklinkApi', () => ({ useMklinkApi: () => mocks.api }))
vi.mock('../../lib/stream/renderScheduler', () => ({
  RenderScheduler: class {
    constructor(render: () => void) { mocks.scheduler.render = render }
    start = mocks.scheduler.start
    stop = mocks.scheduler.stop
    invalidate = mocks.scheduler.invalidate
    recordCollection = mocks.scheduler.recordCollection
    dispose = mocks.scheduler.dispose
  },
}))

describe('RttViewTab binary migration', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllEnvs()
  })

  beforeEach(() => {
    vi.clearAllMocks()
    mocks.binary.rttLines = shallowRef(null)
    mocks.binary.rttTerminal = shallowRef(null)
    mocks.binary.waveformBatch = shallowRef(null)
    mocks.binary.envelope = shallowRef(null)
    mocks.binary.telemetry = shallowRef(null)
    mocks.binary.state = shallowRef({ phase: 'stopped' })
    mocks.binary.error = shallowRef(null)
    mocks.terminalBinary.rttLines = shallowRef(null)
    mocks.terminalBinary.rttTerminal = shallowRef(null)
    mocks.terminalBinary.waveformBatch = shallowRef(null)
    mocks.terminalBinary.envelope = shallowRef(null)
    mocks.terminalBinary.telemetry = shallowRef(null)
    mocks.terminalBinary.state = shallowRef({ phase: 'stopped' })
    mocks.terminalBinary.error = shallowRef(null)
    mocks.dash.state = ref('idle')
    mocks.dash.error = ref(null)
    mocks.useBinaryStream.mockImplementation(name => (
      name === 'rtt-terminal' ? mocks.terminalBinary : mocks.binary
    ))
    mocks.downloadTextFile.mockReset()
    mocks.checkConflict.mockResolvedValue([])
    mocks.scheduler.render = null
    mocks.dash.start.mockResolvedValue(true)
    mocks.dash.stop.mockResolvedValue(true)
    mocks.api.findRtt.mockResolvedValue({ found: true, addr: '0x20001A40' })
    mocks.api.writeRtt.mockResolvedValue({ sent_bytes: 1 })
    mocks.api.setRttEncoding.mockImplementation(async encoding => ({ encoding }))
    mocks.status = { running: false, numeric_channels: [], down_buffers: [] }
    vi.stubGlobal('localStorage', new MemoryStorage())
    localStorage.clear()
    saveDesktopSettings(localStorage, desktopSettings({ rttAddress: '0x20000000' }))
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => ({
      ok: true,
      json: async () => mocks.status,
    })))
  })

  function desktopSettings(overrides: Partial<DesktopSettings> = {}): DesktopSettings {
    return {
      version: 1,
      symbolPath: '',
      symbolDisplayPath: '',
      rttAddress: '',
      rttEncoding: 'utf-8',
      transmitMode: 'text',
      lineEnding: '',
      sendHistory: [],
      ...overrides,
    }
  }

  it('constructs separate RTT transports and mounts only the terminal view by default', () => {
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    expect(mocks.useBinaryStream).toHaveBeenCalledWith('rtt', expect.any(Object))
    expect(mocks.useBinaryStream).toHaveBeenCalledWith('rtt-terminal', expect.any(Object))
    expect(wrapper.findComponent({ name: 'VirtualLogPanel' }).exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'RttTerminalPanel' }).exists()).toBe(true)
    expect(wrapper.find('[data-testid="rtt-save-log"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps RTT setup available while disconnected but requires an explicit connection', async () => {
    const wrapper = mount(RttViewTab, { props: { deviceConnected: false } })

    expect(wrapper.get('.setup-device').text()).toContain('连接 MKLink 设备')
    expect(wrapper.get('[data-testid="rtt-address"]').exists()).toBe(true)
    expect(wrapper.get('.control-toolbar .btn-primary').attributes('disabled')).toBeDefined()

    await wrapper.get('.control-toolbar .btn-primary').trigger('click')
    await flushPromises()

    expect(mocks.dash.start).not.toHaveBeenCalled()
    expect(wrapper.findComponent({ name: 'RttTerminalPanel' }).props('inputEnabled')).toBe(false)
    wrapper.unmount()
  })

  it('switches between log and terminal views without restarting RTT', async () => {
    mocks.status = { running: true, numeric_channels: [], down_buffers: [] }
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    await flushPromises()

    expect(wrapper.get('[data-testid="rtt-terminal-mode"]').attributes('aria-pressed')).toBe('true')
    expect(wrapper.findComponent({ name: 'VirtualLogPanel' }).exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'RttTerminalPanel' }).exists()).toBe(true)
    expect(mocks.terminalBinary.start).toHaveBeenCalled()
    expect(mocks.binary.start).not.toHaveBeenCalled()

    mocks.terminalBinary.rttTerminal.value = {
      type: 'rtt-terminal', sequence: 1n, text: '\x1b[31merror\x1b[0m\r',
    }
    await nextTick()

    await wrapper.get('[data-testid="rtt-log-mode"]').trigger('click')
    expect(wrapper.get('[data-testid="rtt-log-mode"]').attributes('aria-pressed')).toBe('true')
    expect(wrapper.findComponent({ name: 'RttTerminalPanel' }).exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'VirtualLogPanel' }).exists()).toBe(true)
    expect(wrapper.find('[data-testid="rtt-chart-toggle"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="rtt-save-log"]').exists()).toBe(true)
    expect(mocks.terminalBinary.stop).toHaveBeenCalled()
    expect(mocks.binary.start).toHaveBeenCalled()
    expect(mocks.dash.start).not.toHaveBeenCalled()
    expect(mocks.dash.stop).not.toHaveBeenCalled()

    await wrapper.get('[data-testid="rtt-clear-logs"]').trigger('click')
    expect(wrapper.get('[data-testid="rtt-clear-logs"]').attributes('aria-label')).toBe('清除日志')
    wrapper.unmount()
  })

  it('batches terminal keyboard input through the existing RTT Down Buffer API', async () => {
    vi.useFakeTimers()
    mocks.status = {
      running: true,
      numeric_channels: [],
      down_buffers: [{ channel: 0, active: true }],
    }
    mocks.dash.state.value = 'running'
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    await flushPromises()
    const terminal = wrapper.findComponent({ name: 'RttTerminalPanel' })
    expect(terminal.props('inputEnabled')).toBe(true)

    terminal.vm.$emit('input', 'he')
    terminal.vm.$emit('input', 'lp\r')
    await vi.advanceTimersByTimeAsync(8)
    await flushPromises()

    expect(mocks.api.writeRtt).toHaveBeenCalledTimes(1)
    expect(Array.from(mocks.api.writeRtt.mock.calls[0][0])).toEqual(
      Array.from(new TextEncoder().encode('help\r')),
    )
    wrapper.unmount()
  })

  it('groups RTT setup and stream controls into two compact rows', async () => {
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    await wrapper.get('[data-testid="rtt-log-mode"]').trigger('click')

    expect(wrapper.find('.rtt-address-row').exists()).toBe(true)
    expect(wrapper.find('.rtt-primary-tools .control-toolbar').exists()).toBe(true)
    expect(wrapper.find('.rtt-primary-tools .encoding-control').exists()).toBe(true)
    expect(wrapper.find('.rtt-primary-tools .stream-metrics').exists()).toBe(true)
    expect(wrapper.find('.rtt-secondary-tools [data-testid="rtt-chart-toggle"]').exists()).toBe(true)
    expect(wrapper.find('.rtt-secondary-tools [data-testid="rtt-clear-logs"]').exists()).toBe(true)
    expect(wrapper.find('.rtt-format-note').exists()).toBe(false)
    expect(wrapper.find('.format-help-popover').exists()).toBe(false)

    await wrapper.get('[data-testid="rtt-format-help"]').trigger('click')
    expect(wrapper.get('.format-help-popover').text()).toContain('temp=25.3,speed=1200')
    wrapper.unmount()
  })

  it('polls RTT status through the browser page origin', async () => {
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    await flushPromises()

    expect(fetch).toHaveBeenCalledWith(
      '/api/dash/rtt/status',
    )
    wrapper.unmount()
  })

  it('keeps the text data panel visible before RTT text arrives and after clear', async () => {
    vi.useFakeTimers()
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    await wrapper.get('[data-testid="rtt-log-mode"]').trigger('click')

    expect(wrapper.get('.rtt-view-log').classes()).not.toContain('is-empty')

    mocks.binary.rttLines.value = {
      type: 'rtt-lines', sequence: 1n,
      lines: [{ timestampNs: 1n, level: 'raw', text: 'first-line' }],
    }
    await nextTick()
    vi.advanceTimersByTime(100)
    await nextTick()

    expect(wrapper.get('.rtt-view-log').classes()).not.toContain('is-empty')
    await wrapper.get('.btn-clear').trigger('click')
    expect(wrapper.get('.rtt-view-log').classes()).not.toContain('is-empty')
    wrapper.unmount()
  })

  it('saves retained RTT rows only in log mode', async () => {
    vi.useFakeTimers()
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    expect(wrapper.find('[data-testid="rtt-save-log"]').exists()).toBe(false)
    await wrapper.get('[data-testid="rtt-log-mode"]').trigger('click')
    mocks.binary.rttLines.value = {
      type: 'rtt-lines', sequence: 1n,
      lines: [{ timestampNs: 1_000_000n, level: 'raw', text: 'ready' }],
    }
    await nextTick()
    vi.advanceTimersByTime(100)
    await nextTick()

    await wrapper.get('[data-testid="rtt-save-log"]').trigger('click')

    expect(mocks.downloadTextFile).toHaveBeenCalledWith(
      'rtt-test.log', expect.stringContaining('\traw\tready'),
    )
    wrapper.unmount()
  })

  it('starts and stops the binary lifecycle with dashboard controls', async () => {
    mocks.status = {
      running: true,
      control_block_addr: '0x20000000',
      numeric_channels: [],
      down_buffers: [{ channel: 0, active: true }],
    }
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    await wrapper.get('.btn-primary').trigger('click')
    await flushPromises()
    expect(mocks.binary.reset).toHaveBeenCalled()
    expect(mocks.terminalBinary.reset).toHaveBeenCalled()
    expect(mocks.dash.start).toHaveBeenCalledWith({
      addr: '0x20000000', mode: 0, search_size: 1024, encoding: 'utf-8',
    })
    expect(mocks.terminalBinary.start).toHaveBeenCalled()
    mocks.dash.state.value = 'running'
    await nextTick()
    await wrapper.get('.btn-danger').trigger('click')
    expect(mocks.terminalBinary.stop).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('persists the selected encoding and sends it when RTT starts', async () => {
    mocks.status = { running: false, numeric_channels: [], down_buffers: [] }
    mocks.dash.start.mockImplementationOnce(async () => {
      mocks.status = {
        running: true,
        control_block_addr: '0x20000000',
        encoding: 'gbk',
        numeric_channels: [],
        down_buffers: [],
      }
      return true
    })
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    await wrapper.get('[data-testid="rtt-encoding"]').setValue('gbk')
    await wrapper.get('.btn-primary').trigger('click')
    await flushPromises()

    expect(mocks.dash.start).toHaveBeenCalledWith({
      addr: '0x20000000', mode: 0, search_size: 1024, encoding: 'gbk',
    })
    expect(JSON.parse(localStorage.getItem('mklink.desktop.settings.v1') ?? '{}').rttEncoding)
      .toBe('gbk')
    wrapper.unmount()
  })

  it('switches decoder encoding while RTT is running', async () => {
    mocks.status = {
      running: true,
      encoding: 'utf-8',
      numeric_channels: [],
      down_buffers: [],
    }
    mocks.dash.state.value = 'running'
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    await flushPromises()

    await wrapper.get('[data-testid="rtt-encoding"]').setValue('gb18030')
    await flushPromises()

    expect(mocks.api.setRttEncoding).toHaveBeenCalledWith('gb18030')
    expect(JSON.parse(localStorage.getItem('mklink.desktop.settings.v1') ?? '{}').rttEncoding)
      .toBe('gb18030')
    wrapper.unmount()
  })

  it('switches from a conflicting SuperWatch session without confirmation', async () => {
    mocks.checkConflict.mockResolvedValue(['superwatch'])
    const confirm = vi.fn(() => false)
    vi.stubGlobal('confirm', confirm)
    mocks.status = {
      running: true,
      control_block_addr: '0x20000000',
      numeric_channels: [],
      down_buffers: [],
    }
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })

    await wrapper.get('.btn-primary').trigger('click')
    await flushPromises()

    expect(confirm).not.toHaveBeenCalled()
    expect(mocks.dash.start).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('shows startup feedback and deduplicates repeated RTT start clicks', async () => {
    let resolveStart!: (value: boolean) => void
    mocks.dash.start.mockReturnValueOnce(new Promise(resolve => { resolveStart = resolve }))
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    const toolbar = wrapper.findComponent({ name: 'ControlToolbar' })

    toolbar.vm.$emit('start')
    toolbar.vm.$emit('start')
    await nextTick()

    expect(mocks.dash.start).toHaveBeenCalledOnce()
    expect(toolbar.text()).toContain('启动中')

    resolveStart(false)
    await flushPromises()
    wrapper.unmount()
  })

  it('stops a timed-out RTT start and ignores a late running status', async () => {
    vi.useFakeTimers()
    mocks.dash.stop
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true)
    mocks.dash.start.mockImplementationOnce(async () => {
      mocks.status = {
        running: true,
        control_block_addr: null,
        numeric_channels: [],
        down_buffers: [],
      }
      return true
    })
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    const toolbar = wrapper.findComponent({ name: 'ControlToolbar' })

    toolbar.vm.$emit('start')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(12_000)
    await flushPromises()

    expect(mocks.dash.stop).toHaveBeenCalledTimes(2)
    expect(mocks.binary.stop).toHaveBeenCalled()
    const startCalls = mocks.binary.start.mock.calls.length

    await vi.advanceTimersByTimeAsync(1_000)
    await flushPromises()
    expect(mocks.binary.start).toHaveBeenCalledTimes(startCalls)
    expect(toolbar.props('state')).toBe('error')
    wrapper.unmount()
  })

  it('searches the selected AXF/ELF and fills the editable RTT address', async () => {
    saveDesktopSettings(localStorage, desktopSettings({
      symbolPath: 'C:\\firmware\\app.elf',
      rttAddress: '0x20000000',
    }))
    mocks.api.findRtt.mockResolvedValueOnce({
      found: true, addr: '0x20001A40', source: 'binary:app.elf',
    })
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })

    await wrapper.get('[data-testid="rtt-search"]').trigger('click')
    await flushPromises()

    expect(mocks.api.findRtt).toHaveBeenCalledWith('C:\\firmware\\app.elf')
    expect((wrapper.get('[data-testid="rtt-address"]').element as HTMLInputElement).value)
      .toBe('0x20001A40')
    expect(JSON.parse(localStorage.getItem('mklink.desktop.settings.v1') ?? '{}').rttAddress)
      .toBe('0x20001A40')
    expect(wrapper.text()).toContain('binary:app.elf')
    wrapper.unmount()
  })

  it('updates its address when RTOS Trace saves a new shared value', async () => {
    saveDesktopSettings(localStorage, desktopSettings({ rttAddress: '0x20000000' }))
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })

    saveDesktopSettings(localStorage, desktopSettings({ rttAddress: '0x0008E488' }))
    await nextTick()

    expect((wrapper.get('[data-testid="rtt-address"]').element as HTMLInputElement).value)
      .toBe('0x0008E488')
    wrapper.unmount()
  })

  it('falls back to project auto-detection when no AXF/ELF is selected', async () => {
    saveDesktopSettings(localStorage, desktopSettings({
      symbolPath: '   ',
    }))
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    await wrapper.get('[data-testid="rtt-search"]').trigger('click')
    await flushPromises()
    expect(mocks.api.findRtt).toHaveBeenLastCalledWith(undefined)
    wrapper.unmount()
  })

  it('does not let an in-flight search overwrite a newer manual edit', async () => {
    let resolveSearch!: (value: unknown) => void
    mocks.api.findRtt.mockReturnValueOnce(new Promise(resolve => { resolveSearch = resolve }))
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })

    await wrapper.get('[data-testid="rtt-search"]').trigger('click')
    await wrapper.get('[data-testid="rtt-address"]').setValue('0x20003333')
    resolveSearch({ found: true, addr: '0x20001111', source: 'binary:old.elf' })
    await flushPromises()

    expect((wrapper.get('[data-testid="rtt-address"]').element as HTMLInputElement).value)
      .toBe('0x20003333')
    expect(JSON.parse(localStorage.getItem('mklink.desktop.settings.v1') ?? '{}').rttAddress)
      .toBe('0x20003333')
    expect(wrapper.text()).not.toContain('binary:old.elf')
    wrapper.unmount()
  })

  it('does not apply a search result after the selected AXF changes', async () => {
    let resolveSearch!: (value: unknown) => void
    mocks.api.findRtt.mockReturnValueOnce(new Promise(resolve => { resolveSearch = resolve }))
    saveDesktopSettings(localStorage, desktopSettings({
      symbolPath: 'C:\\firmware\\old.axf',
      rttAddress: '0x20000000',
    }))
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })

    await wrapper.get('[data-testid="rtt-search"]').trigger('click')
    saveDesktopSettings(localStorage, desktopSettings({
      symbolPath: 'D:\\build\\next.axf',
      rttAddress: '',
    }))
    resolveSearch({ found: true, addr: '0x20001111', source: 'binary:old.axf' })
    await flushPromises()

    expect((wrapper.get('[data-testid="rtt-address"]').element as HTMLInputElement).value)
      .toBe('')
    expect(JSON.parse(localStorage.getItem('mklink.desktop.settings.v1') ?? '{}')).toMatchObject({
      symbolPath: 'D:\\build\\next.axf',
      rttAddress: '',
    })
    expect(wrapper.text()).not.toContain('binary:old.axf')
    wrapper.unmount()
  })

  it('blocks RTT start until an in-flight address search completes', async () => {
    let resolveSearch!: (value: unknown) => void
    mocks.api.findRtt.mockReturnValueOnce(new Promise(resolve => { resolveSearch = resolve }))
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })

    await wrapper.get('[data-testid="rtt-search"]').trigger('click')

    const toolbar = wrapper.findComponent({ name: 'ControlToolbar' })
    expect(toolbar.get('.btn-primary').attributes('disabled')).toBeDefined()
    toolbar.vm.$emit('start')
    await nextTick()
    expect(mocks.dash.start).not.toHaveBeenCalled()

    resolveSearch({ found: true, addr: '0x20001A40', source: 'binary:app.axf' })
    await flushPromises()
    expect(toolbar.get('.btn-primary').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('preserves the current address on search failure and ignores stale results', async () => {
    let resolveFirst!: (value: unknown) => void
    const first = new Promise(resolve => { resolveFirst = resolve })
    mocks.api.findRtt
      .mockReturnValueOnce(first)
      .mockResolvedValueOnce({ found: true, addr: '0x20002222' })
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })

    await wrapper.get('[data-testid="rtt-search"]').trigger('click')
    await wrapper.get('[data-testid="rtt-search"]').trigger('click')
    await flushPromises()
    expect((wrapper.get('[data-testid="rtt-address"]').element as HTMLInputElement).value)
      .toBe('0x20002222')

    resolveFirst({ found: true, addr: '0x20001111' })
    await flushPromises()
    expect((wrapper.get('[data-testid="rtt-address"]').element as HTMLInputElement).value)
      .toBe('0x20002222')

    mocks.api.findRtt.mockRejectedValueOnce(new Error('未找到 RTT'))
    await wrapper.get('[data-testid="rtt-search"]').trigger('click')
    await flushPromises()
    expect((wrapper.get('[data-testid="rtt-address"]').element as HTMLInputElement).value)
      .toBe('0x20002222')
    expect(wrapper.text()).toContain('未找到 RTT')
    wrapper.unmount()
  })

  it('rejects an invalid manual address before starting', async () => {
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    await wrapper.get('[data-testid="rtt-address"]').setValue('20001A40')
    await wrapper.get('.btn-primary').trigger('click')

    expect(mocks.dash.start).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('RTT 地址')
    wrapper.unmount()
  })

  it('enables transmission only while RTT has an active DownBuffer', async () => {
    vi.useFakeTimers()
    mocks.status = {
      running: true,
      numeric_channels: [],
      down_buffers: [{ channel: 0, active: true }],
    }
    mocks.dash.state.value = 'running'
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    await flushPromises()
    await nextTick()

    const bar = wrapper.findComponent({ name: 'RttTransmitBar' })
    expect(bar.props('enabled')).toBe(true)
    await bar.get('[data-testid="rtt-input"]').setValue('OK')
    await bar.get('[data-testid="rtt-send"]').trigger('click')
    await flushPromises()
    expect(mocks.api.writeRtt).toHaveBeenCalledWith(Uint8Array.of(0x4f, 0x4b))

    mocks.status = { running: true, numeric_channels: [], down_buffers: [] }
    vi.advanceTimersByTime(1_000)
    await flushPromises()
    expect(bar.props('enabled')).toBe(false)
    wrapper.unmount()
  })

  it('requires an active DownBuffer on the selected RTT channel', async () => {
    mocks.status = {
      running: true,
      numeric_channels: [],
      down_buffers: [{ channel: 1, active: true }],
    }
    mocks.dash.state.value = 'running'
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    await flushPromises()

    expect(wrapper.findComponent({ name: 'RttTransmitBar' }).props('enabled')).toBe(false)
    wrapper.unmount()
  })

  it('disables transmission immediately when stop begins', async () => {
    vi.useFakeTimers()
    let resolveStop!: () => void
    mocks.dash.stop.mockReturnValueOnce(new Promise<void>(resolve => { resolveStop = resolve }))
    mocks.status = {
      running: true,
      numeric_channels: [],
      down_buffers: [{ channel: 0, active: true }],
    }
    mocks.dash.state.value = 'running'
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    await flushPromises()
    const bar = wrapper.findComponent({ name: 'RttTransmitBar' })
    expect(bar.props('enabled')).toBe(true)

    await wrapper.get('.btn-danger').trigger('click')
    expect(bar.props('enabled')).toBe(false)
    resolveStop()
    await flushPromises()
    wrapper.unmount()
  })

  it('requests the numeric envelope over the actual Worker buffer time range', async () => {
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    await wrapper.get('[data-testid="rtt-log-mode"]').trigger('click')
    mocks.binary.telemetry.value = { bufferedSamples: 256 }
    mocks.binary.waveformBatch.value = {
      type: 'waveform-batch', sequence: 1n, timestampNs: 2_000_000_000n,
      itemCount: 256, channelCount: 4, layout: 'sample-major-float32',
      values: new ArrayBuffer(0), times: new ArrayBuffer(0),
      bufferStartMs: 1_500, bufferEndMs: 2_000,
    }
    await nextTick()

    mocks.scheduler.render?.()

    expect(mocks.binary.requestVisibleRange).toHaveBeenCalledWith(1, 1_500, 2_000, 564)
    wrapper.unmount()
  })

  it('shows chart controls and keeps the last curve visible after pause and stop', async () => {
    mocks.status = {
      running: true,
      numeric_channels: ['temp'],
      down_buffers: [],
    }
    const clearRect = vi.fn()
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      setTransform: vi.fn(), clearRect, beginPath: vi.fn(), moveTo: vi.fn(),
      lineTo: vi.fn(), stroke: vi.fn(), fillText: vi.fn(), save: vi.fn(),
      restore: vi.fn(), rect: vi.fn(), clip: vi.fn(), translate: vi.fn(),
      rotate: vi.fn(), strokeStyle: '', fillStyle: '', font: '', textAlign: '',
      lineWidth: 1,
    } as unknown as CanvasRenderingContext2D)
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    await wrapper.get('[data-testid="rtt-log-mode"]').trigger('click')
    mocks.binary.waveformBatch.value = {
      type: 'waveform-batch', sequence: 1n, timestampNs: 2_000_000_000n,
      itemCount: 2, channelCount: 1, layout: 'sample-major-float32',
      values: Float32Array.of(1, 2).buffer,
      times: Float64Array.of(1_000, 2_000).buffer,
      bufferStartMs: 1_000, bufferEndMs: 2_000,
    }
    await nextTick()
    mocks.binary.envelope.value = {
      type: 'render-envelope', mode: 'min-max-v1', timestampKind: 'sample-milliseconds',
      requestId: 0, pixelWidth: 640, channelCount: 1, pointCount: 2,
      candidateSampleCount: 2, values: Float32Array.of(1, 2).buffer,
      times: Float64Array.of(1_000, 2_000).buffer,
      timeIndices: Uint32Array.of(0, 1).buffer,
      channelOffsets: Uint32Array.of(0, 2).buffer,
    }
    await nextTick()

    expect(wrapper.find('.format-help-popover').exists()).toBe(false)
    await wrapper.get('[data-testid="rtt-format-help"]').trigger('click')
    expect(wrapper.get('.format-help-popover').text()).toContain('数据格式')
    expect(wrapper.get('.format-help-popover').text()).toContain('temp=25.3,speed=1200')
    expect(wrapper.get('[data-testid="rtt-chart-toggle"]').text()).toContain('关闭曲线')
    expect(wrapper.get('.rtt-chart-shell').isVisible()).toBe(true)

    mocks.dash.state.value = 'running'
    await nextTick()
    await wrapper.get('.control-toolbar .btn:not(.btn-danger)').trigger('click')
    await wrapper.get('.btn-danger').trigger('click')

    expect(wrapper.get('.rtt-chart-shell').isVisible()).toBe(true)
    await wrapper.get('[data-testid="rtt-chart-toggle"]').trigger('click')
    expect(wrapper.find('.rtt-chart-shell').exists()).toBe(false)
    expect(wrapper.get('[data-testid="rtt-chart-toggle"]').text()).toContain('打开曲线')
    wrapper.unmount()
  })

  it('zooms around the pointer and pans the retained RTT chart viewport', async () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      setTransform: vi.fn(), clearRect: vi.fn(), beginPath: vi.fn(), moveTo: vi.fn(),
      lineTo: vi.fn(), stroke: vi.fn(), fillText: vi.fn(), save: vi.fn(),
      restore: vi.fn(), rect: vi.fn(), clip: vi.fn(), translate: vi.fn(),
      rotate: vi.fn(), strokeStyle: '', fillStyle: '', font: '', textAlign: '',
      lineWidth: 1,
    } as unknown as CanvasRenderingContext2D)
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    await wrapper.get('[data-testid="rtt-log-mode"]').trigger('click')
    mocks.binary.telemetry.value = { bufferedSamples: 256 }
    mocks.binary.waveformBatch.value = {
      type: 'waveform-batch', sequence: 1n, timestampNs: 10_000_000_000n,
      itemCount: 256, channelCount: 1, layout: 'sample-major-float32',
      values: new ArrayBuffer(0), times: new ArrayBuffer(0),
      bufferStartMs: 0, bufferEndMs: 10_000,
    }
    await nextTick()

    const canvas = wrapper.get('canvas').element as HTMLCanvasElement
    Object.defineProperty(canvas, 'clientWidth', { configurable: true, value: 640 })
    Object.defineProperty(canvas, 'clientHeight', { configurable: true, value: 220 })
    canvas.getBoundingClientRect = () => ({
      x: 0, y: 0, width: 640, height: 220, top: 0, right: 640,
      bottom: 220, left: 0, toJSON: () => ({}),
    })
    mocks.scheduler.render?.()
    const initial = mocks.binary.requestVisibleRange.mock.calls.at(-1)

    await wrapper.get('canvas').trigger('wheel', { deltaY: -1, clientX: 320, clientY: 110 })
    mocks.scheduler.render?.()
    const zoomed = mocks.binary.requestVisibleRange.mock.calls.at(-1)
    expect(zoomed[2] - zoomed[1]).toBeLessThan(initial[2] - initial[1])

    const down = new MouseEvent('mousedown')
    Object.defineProperties(down, {
      button: { value: 0 }, clientX: { value: 320 }, clientY: { value: 110 },
    })
    canvas.dispatchEvent(down)
    const move = new MouseEvent('mousemove')
    Object.defineProperties(move, {
      clientX: { value: 220 }, clientY: { value: 130 },
    })
    window.dispatchEvent(move)
    window.dispatchEvent(new MouseEvent('mouseup', { button: 0 }))
    mocks.scheduler.render?.()
    const panned = mocks.binary.requestVisibleRange.mock.calls.at(-1)
    expect(panned[1]).not.toBe(zoomed[1])
    wrapper.unmount()
  })

  it('does not start the binary transport when the dashboard start fails', async () => {
    mocks.dash.start.mockResolvedValue(false)
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })

    await wrapper.get('.btn-primary').trigger('click')
    await Promise.resolve()

    expect(mocks.binary.reset).toHaveBeenCalled()
    expect(mocks.binary.start).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('stops the binary transport and surfaces a backend runtime error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        running: false,
        error: 'RTT device entered ERROR state',
        numeric_channels: [],
      }),
    }))
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })

    await flushPromises()
    await nextTick()

    expect(mocks.binary.stop).toHaveBeenCalled()
    const toolbar = wrapper.findComponent({ name: 'ControlToolbar' })
    expect(toolbar.props('state')).toBe('error')
    expect(toolbar.props('error')).toBe('RTT device entered ERROR state')
    expect(wrapper.findComponent({ name: 'RttTransmitBar' }).props('enabled')).toBe(false)
    wrapper.unmount()
  })

  it('returns to idle when another dashboard stops RTT in the backend', async () => {
    vi.useFakeTimers()
    mocks.status = {
      running: true,
      numeric_channels: ['temp'],
      down_buffers: [],
    }
    mocks.dash.state.value = 'running'
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    await flushPromises()

    expect(wrapper.findComponent({ name: 'ControlToolbar' }).props('state')).toBe('running')
    mocks.status = {
      running: false,
      numeric_channels: ['temp'],
      down_buffers: [],
    }
    await vi.advanceTimersByTimeAsync(1_000)
    await flushPromises()

    expect(wrapper.findComponent({ name: 'ControlToolbar' }).props('state')).toBe('idle')
    expect(mocks.binary.stop).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('pauses only rendering while binary acquisition remains active', async () => {
    vi.useFakeTimers()
    mocks.status = {
      running: true,
      numeric_channels: ['temp'],
      down_buffers: [],
    }
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    await wrapper.get('[data-testid="rtt-log-mode"]').trigger('click')
    mocks.dash.state.value = 'running'
    await flushPromises()
    mocks.binary.start.mockClear()

    await wrapper.get('.control-toolbar .btn:not(.btn-danger)').trigger('click')
    expect(mocks.scheduler.stop).toHaveBeenCalled()
    expect(mocks.dash.pause).not.toHaveBeenCalled()
    expect(mocks.binary.stop).not.toHaveBeenCalled()
    mocks.binary.rttLines.value = {
      type: 'rtt-lines', sequence: 1n,
      lines: [{ timestampNs: 1n, level: 'raw', text: 'paused-line' }],
    }
    await nextTick()
    vi.advanceTimersByTime(100)
    await nextTick()
    expect((wrapper.findComponent({ name: 'VirtualLogPanel' }).vm as any).retainedCount).toBe(0)

    await wrapper.get('.control-toolbar .btn-primary').trigger('click')
    expect(mocks.scheduler.start).toHaveBeenCalledTimes(3)
    expect(mocks.scheduler.invalidate).toHaveBeenCalledWith('data')
    expect(mocks.dash.resume).not.toHaveBeenCalled()
    expect(mocks.binary.start).not.toHaveBeenCalled()
    mocks.binary.rttLines.value = {
      type: 'rtt-lines', sequence: 2n,
      lines: [{ timestampNs: 2n, level: 'raw', text: 'resumed-line' }],
    }
    await nextTick()
    vi.advanceTimersByTime(100)
    await nextTick()
    expect((wrapper.findComponent({ name: 'VirtualLogPanel' }).vm as any).retainedCount).toBe(1)
    wrapper.unmount()
  })

  it('ignores an in-flight envelope that arrives after render pause', async () => {
    const clearRect = vi.fn()
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      setTransform: vi.fn(), clearRect, beginPath: vi.fn(), moveTo: vi.fn(),
      lineTo: vi.fn(), stroke: vi.fn(), strokeStyle: '',
    } as unknown as CanvasRenderingContext2D)
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    mocks.dash.state.value = 'running'
    await nextTick()

    await wrapper.get('.control-toolbar .btn:not(.btn-danger)').trigger('click')
    mocks.binary.envelope.value = {
      type: 'render-envelope', requestId: 0, channelCount: 1, pointCount: 2,
      values: Float32Array.of(1, 2).buffer,
      channelOffsets: Uint32Array.of(0, 2).buffer,
    }
    await nextTick()

    expect(clearRect).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('bounds an accelerated RTT record to Worker to VirtualLog pipeline at 5000 lines', async () => {
    vi.useFakeTimers()
    const wrapper = mount(RttViewTab, { props: { deviceConnected: true } })
    await wrapper.get('[data-testid="rtt-log-mode"]').trigger('click')
    const lineCount = 6000
    const encoder = new TextEncoder()
    const encoded = Array.from({ length: lineCount }, (_, index) => encoder.encode(`line-${index + 1}`))
    const payload = new Uint8Array(encoded.reduce((size, line) => size + 13 + line.length, 0))
    const payloadView = new DataView(payload.buffer)
    let offset = 0
    encoded.forEach((line, index) => {
      payloadView.setBigUint64(offset, BigInt(index + 1), true)
      payloadView.setUint8(offset + 8, 0)
      payloadView.setUint32(offset + 9, line.length, true)
      payload.set(line, offset + 13)
      offset += 13 + line.length
    })
    const buffer = new ArrayBuffer(36 + payload.byteLength)
    const bytes = new Uint8Array(buffer)
    const view = new DataView(buffer)
    bytes.set([0x4d, 0x4b, 0x53, 0x54])
    view.setUint8(4, 1)
    view.setUint8(5, StreamType.RTT_RAW)
    view.setUint8(6, 1)
    view.setUint8(7, 36)
    view.setUint32(8, StreamType.RTT_RAW, true)
    view.setBigUint64(12, 1n, true)
    view.setBigUint64(20, BigInt(lineCount), true)
    view.setUint32(28, lineCount, true)
    view.setUint32(32, payload.byteLength, true)
    bytes.set(payload, 36)
    const decoder = new StreamDecoder(message => {
      if (message.type === 'rtt-lines') mocks.binary.rttLines.value = message
    })
    decoder.handle({ type: 'configure', capacity: 200_000, channelCount: 1 })
    decoder.handle({
      type: 'frame', buffer, connectionGeneration: 1, frameTicket: 1,
    })
    await nextTick()
    vi.advanceTimersByTime(100)
    await nextTick()

    const panel = wrapper.findComponent({ name: 'VirtualLogPanel' })
    expect((panel.vm as any).retainedCount).toBe(5000)
    expect((panel.vm as any).firstLineNumber).toBe(1001)
    expect(panel.findAll('.virtual-log-row').length).toBeLessThan(40)
    wrapper.unmount()
  })
})
