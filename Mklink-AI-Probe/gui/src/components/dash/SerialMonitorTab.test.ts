import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { shallowRef } from 'vue'
import SerialMonitorTab from './SerialMonitorTab.vue'

const mocks = vi.hoisted(() => ({
  listPorts: vi.fn(),
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
  toastInfo: vi.fn(),
  useBinaryStream: vi.fn(),
  downloadTextFile: vi.fn(),
  terminalWrites: [] as string[],
  terminalClears: 0,
  terminalInput: null as null | ((data: string) => void),
  logBinary: null as any,
  terminalBinary: null as any,
}))

vi.mock('../../composables/useMklinkApi', () => ({
  useMklinkApi: () => ({ listPorts: mocks.listPorts }),
}))

vi.mock('../../composables/useToast', () => ({
  useToast: () => ({
    success: mocks.toastSuccess, error: mocks.toastError, info: mocks.toastInfo,
  }),
}))

vi.mock('../../composables/useBinaryStream', () => ({
  useBinaryStream: mocks.useBinaryStream,
}))

vi.mock('../../lib/downloadTextFile', () => ({
  downloadTextFile: mocks.downloadTextFile,
  timestampedLogName: (prefix: string) => `${prefix}-test.log`,
}))

vi.mock('@xterm/xterm', () => ({
  Terminal: class {
    options: Record<string, unknown>

    constructor(options: Record<string, unknown>) {
      this.options = { disableStdin: options.disableStdin }
    }

    loadAddon() {}
    open() {}
    attachCustomKeyEventHandler() {}
    onData(callback: (data: string) => void) {
      mocks.terminalInput = callback
      return { dispose() {} }
    }
    clear() { mocks.terminalClears += 1 }
    getSelection() { return '' }
    paste(text: string) { mocks.terminalInput?.(text) }
    selectAll() {}
    write(text: string) { mocks.terminalWrites.push(text) }
    focus() {}
    dispose() {}
  },
}))

vi.mock('@xterm/addon-fit', () => ({ FitAddon: class { fit() {} } }))

class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>()
  get length(): number { return this.values.size }
  clear(): void { this.values.clear() }
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  key(index: number): string | null { return [...this.values.keys()][index] ?? null }
  removeItem(key: string): void { this.values.delete(key) }
  setItem(key: string, value: string): void { this.values.set(key, value) }
}

function binaryClient(kind: 'log' | 'terminal') {
  return {
    serialLines: shallowRef(kind === 'log' ? null : undefined),
    serialTerminal: shallowRef(kind === 'terminal' ? null : undefined),
    telemetry: shallowRef(null),
    error: shallowRef(null),
    start: vi.fn(), stop: vi.fn(), reset: vi.fn(), configure: vi.fn(),
    requestVisibleRange: vi.fn(),
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function runningStatus(baudrate = 230400) {
  return {
    running: true,
    ports: { TEST_UART: 'open' },
    config: [{ port: 'TEST_UART', baudrate, databits: 8, stopbits: 1, parity: 'N' }],
    stats: { rx_count: 0, tx_count: 0, rx_bytes: 0, tx_bytes: 0, bytes_per_sec: 0 },
  }
}

function ymodemStatus(overrides: Record<string, unknown> = {}) {
  return {
    transfer_id: 1,
    state: 'running',
    active: true,
    phase: 'waiting',
    port: 'TEST_UART',
    filename: 'rtthread.bin',
    sent_bytes: 0,
    total_bytes: 8,
    percent: 0,
    block: 0,
    retries: 0,
    error: '',
    ...overrides,
  }
}

describe('SerialMonitorTab', () => {
  beforeEach(() => {
    vi.stubGlobal('localStorage', new MemoryStorage())
    mocks.logBinary = binaryClient('log')
    mocks.terminalBinary = binaryClient('terminal')
    mocks.useBinaryStream.mockReset().mockImplementation((_name, options) => (
      options.decoderMode === 'serial-log' ? mocks.logBinary : mocks.terminalBinary
    ))
    mocks.listPorts.mockReset().mockResolvedValue([
      { device: 'TEST_UART', description: 'USB UART', is_mklink: false },
    ])
    mocks.toastError.mockReset()
    mocks.toastSuccess.mockReset()
    mocks.toastInfo.mockReset()
    mocks.downloadTextFile.mockReset()
    mocks.terminalWrites.length = 0
    mocks.terminalClears = 0
    mocks.terminalInput = null
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('isolates terminal and log streams without stopping the backend on unmount', async () => {
    let status = { ...runningStatus(), running: false, ports: {}, config: [] }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/dash/serial/status')) return jsonResponse(status)
      if (url.endsWith('/api/dash/serial/start')) {
        status = runningStatus()
        return jsonResponse({ status: 'started' })
      }
      if (url.endsWith('/api/dash/serial/send')) return jsonResponse({ ok: true })
      throw new Error(`Unexpected request: ${url} ${init?.method || 'GET'}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(SerialMonitorTab)
    await vi.waitFor(() => expect(wrapper.text()).toContain('USB UART'))

    expect(wrapper.text()).not.toContain('请先连接设备')
    expect(wrapper.findComponent({ name: 'VirtualLogPanel' }).exists()).toBe(false)
    expect(wrapper.find('[data-testid="serial-save-log"]').exists()).toBe(false)
    await wrapper.get('.btn-primary').trigger('click')
    await vi.waitFor(() => expect(mocks.terminalBinary.start).toHaveBeenCalledTimes(1))
    expect(mocks.logBinary.start).not.toHaveBeenCalled()

    mocks.terminalBinary.serialTerminal.value = {
      type: 'serial-terminal', sequence: 1n, text: '\u001b[31mprompt> ',
    }
    await wrapper.vm.$nextTick()
    expect(mocks.terminalWrites.join('')).toContain('\u001b[31mprompt> ')

    mocks.terminalInput?.('help\r')
    await vi.waitFor(() => {
      const send = fetchMock.mock.calls.find(call => String(call[0]).endsWith('/api/dash/serial/send'))
      expect(JSON.parse(String(send?.[1]?.body))).toEqual({
        port: 'TEST_UART', data: '68656c700d', hex: true,
      })
    })

    await wrapper.get('[data-testid="serial-log-mode"]').trigger('click')
    expect(mocks.terminalBinary.stop).toHaveBeenCalled()
    expect(mocks.logBinary.start).toHaveBeenCalledTimes(1)
    expect(wrapper.findComponent({ name: 'RttTerminalPanel' }).exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'VirtualLogPanel' }).exists()).toBe(true)
    expect(wrapper.find('[data-testid="serial-save-log"]').exists()).toBe(true)

    mocks.logBinary.serialLines.value = {
      type: 'serial-lines', sequence: 2n,
      lines: [{ timestampNs: 1_000_000n, direction: 'RX', rawHex: '4F4B0A', ascii: 'OK\n' }],
    }
    await vi.waitFor(() => expect(wrapper.find('[data-testid="serial-save-log"]').attributes('disabled')).toBeUndefined())
    await wrapper.get('[data-testid="serial-save-log"]').trigger('click')
    expect(mocks.downloadTextFile).toHaveBeenCalledWith(
      'serial-test.log', expect.stringContaining('\tRX\t4F4B0A  OK\\n'),
    )

    wrapper.unmount()
    expect(fetchMock.mock.calls.some(call => String(call[0]).endsWith('/api/dash/serial/stop'))).toBe(false)
    expect(mocks.logBinary.stop).toHaveBeenCalled()
    expect(mocks.terminalBinary.stop).toHaveBeenCalled()
  })

  it('accepts a custom positive integer baud rate', async () => {
    let status = { ...runningStatus(), running: false, ports: {}, config: [] }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/dash/serial/status')) return jsonResponse(status)
      if (url.endsWith('/api/dash/serial/start')) {
        status = runningStatus(JSON.parse(String(init?.body)).ports[0].baudrate)
        return jsonResponse({ status: 'started' })
      }
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(SerialMonitorTab)
    await vi.waitFor(() => expect(wrapper.text()).toContain('USB UART'))

    const baudrateInput = wrapper.get('[data-testid="serial-baudrate"]')
    expect((baudrateInput.element as HTMLInputElement).value).toBe('115200')
    expect(wrapper.findAll('#serial-baudrates option').map(option => option.attributes('value')))
      .toContain('115200')
    for (const invalid of ['', '0', '-1', '1.5', '9007199254740992']) {
      await baudrateInput.setValue(invalid)
      expect(baudrateInput.attributes('aria-invalid')).toBe('true')
      expect(wrapper.get('.btn-primary').attributes('disabled')).toBeDefined()
      await wrapper.get('.btn-primary').trigger('click')
      expect(fetchMock.mock.calls.some(call => String(call[0]).endsWith('/api/dash/serial/start')))
        .toBe(false)
    }
    await baudrateInput.setValue('250000')
    expect(baudrateInput.attributes('aria-invalid')).toBe('false')
    expect(wrapper.get('.btn-primary').attributes('disabled')).toBeUndefined()
    await wrapper.get('.btn-primary').trigger('click')

    await vi.waitFor(() => {
      const start = fetchMock.mock.calls.find(call => String(call[0]).endsWith('/api/dash/serial/start'))
      expect(JSON.parse(String(start?.[1]?.body)).ports[0].baudrate).toBe(250000)
    })
    await vi.waitFor(() => expect(baudrateInput.attributes('disabled')).toBeDefined())
    expect((baudrateInput.element as HTMLInputElement).value).toBe('250000')
    wrapper.unmount()
  })

  it('reconnects to an existing session and clears only the mounted mode', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/dash/serial/status')) return jsonResponse(runningStatus(250000))
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(SerialMonitorTab)
    await vi.waitFor(() => expect(mocks.terminalBinary.start).toHaveBeenCalledTimes(1))

    const baudrateInput = wrapper.get('[data-testid="serial-baudrate"]')
    expect((baudrateInput.element as HTMLInputElement).value).toBe('250000')
    expect(baudrateInput.attributes('disabled')).toBeDefined()
    expect(wrapper.findComponent({ name: 'VirtualLogPanel' }).exists()).toBe(false)
    await wrapper.get('.clear-action').trigger('click')
    expect(mocks.terminalClears).toBeGreaterThan(0)

    await wrapper.get('[data-testid="serial-log-mode"]').trigger('click')
    mocks.logBinary.serialLines.value = {
      type: 'serial-lines', sequence: 1n,
      lines: [{ timestampNs: 1_000_000n, direction: 'RX', rawHex: '4F4B0A', ascii: 'OK\n' }],
    }
    await vi.waitFor(() => expect(
      (wrapper.findComponent({ name: 'VirtualLogPanel' }).vm as any).retainedCount,
    ).toBe(1))
    await wrapper.get('.clear-action').trigger('click')
    expect((wrapper.findComponent({ name: 'VirtualLogPanel' }).vm as any).retainedCount).toBe(0)
    expect((wrapper.get('select').element as HTMLSelectElement).value).toBe('TEST_UART')

    wrapper.unmount()
  })

  it('uploads a selected YMODEM file, reports progress in the terminal, and locks ordinary input', async () => {
    let transferPolls = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/dash/serial/status')) return jsonResponse(runningStatus())
      if (url.includes('/api/dash/serial/ymodem/start?port=TEST_UART')) {
        return jsonResponse(ymodemStatus())
      }
      if (url.endsWith('/api/dash/serial/ymodem/status')) {
        transferPolls += 1
        if (transferPolls === 1) {
          return jsonResponse(ymodemStatus({
            phase: 'transferring', sent_bytes: 4, percent: 50, block: 1,
          }))
        }
        if (transferPolls === 2) {
          return jsonResponse(ymodemStatus({
            phase: 'transferring', sent_bytes: 5, percent: 55, block: 2,
          }))
        }
        if (transferPolls === 3) {
          return jsonResponse(ymodemStatus({
            phase: 'transferring', sent_bytes: 6, percent: 60, block: 3,
          }))
        }
        return jsonResponse(ymodemStatus({
          state: 'completed', active: false, phase: 'completed', sent_bytes: 8, percent: 100,
        }))
      }
      if (url.includes('/api/dash/serial/ymodem/trace?')) {
        return jsonResponse({
          transfer_id: 1,
          entries: [
            { seq: 1, transfer_id: 1, timestamp: 1, port: 'TEST_UART', direction: 'RX', size: 1, hex: '43' },
            { seq: 2, transfer_id: 1, timestamp: 1.1, port: 'TEST_UART', direction: 'TX', size: 4, hex: '01 02 A0 FF' },
          ],
          next_seq: 2,
          dropped: 0,
        })
      }
      if (url.endsWith('/api/dash/serial/send')) return jsonResponse({ ok: true })
      throw new Error(`Unexpected request: ${url} ${init?.method || 'GET'}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(SerialMonitorTab)
    await vi.waitFor(() => expect(mocks.terminalBinary.start).toHaveBeenCalledTimes(1))

    const file = new File([new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8])], 'rtthread.bin')
    const input = wrapper.get('[data-testid="serial-ymodem-file"]')
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    expect(wrapper.text()).toContain('rtthread.bin · 8 B')
    expect(wrapper.get('[data-testid="serial-ymodem-start"]').attributes('disabled')).toBeUndefined()

    await wrapper.get('[data-testid="serial-ymodem-start"]').trigger('click')
    await vi.waitFor(() => expect(wrapper.find('[data-testid="serial-ymodem-cancel"]').exists()).toBe(true))
    expect(wrapper.findComponent({ name: 'RttTerminalPanel' }).props('inputEnabled')).toBe(false)
    mocks.terminalInput?.('must not send\r')
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(fetchMock.mock.calls.some(call => String(call[0]).endsWith('/api/dash/serial/send'))).toBe(false)

    const startCall = fetchMock.mock.calls.find(call => String(call[0]).includes('/ymodem/start'))
    expect(startCall?.[1]?.method).toBe('POST')
    const form = startCall?.[1]?.body as FormData
    expect((form.get('file') as File).name).toBe('rtthread.bin')
    expect((form.get('file') as File).size).toBe(8)

    await vi.waitFor(() => expect(wrapper.text()).toContain('100% · 8/8 B'))
    expect(mocks.terminalWrites.join('')).toContain('[YMODEM] \u51c6\u5907\u53d1\u9001 rtthread.bin\uff088 B\uff09')
    expect(mocks.terminalWrites.join('')).toContain('[YMODEM] \u6b63\u5728\u4f20\u8f93 50%\uff084/8 B\uff09')
    expect(mocks.terminalWrites.join('')).not.toContain('[YMODEM] \u6b63\u5728\u4f20\u8f93 55%')
    expect(mocks.terminalWrites.join('')).toContain('[YMODEM] \u6b63\u5728\u4f20\u8f93 60%\uff086/8 B\uff09')
    expect(mocks.terminalWrites.join('')).toContain('[YMODEM] \u4f20\u8f93\u5b8c\u6210')
    expect(mocks.terminalWrites.join('')).toContain('YMODEM RX 1 B +0x0000')
    expect(mocks.terminalWrites.join('')).toContain('YMODEM TX 4 B +0x0000')
    expect(mocks.terminalWrites.join('')).toContain('01 02 A0 FF')
    expect(mocks.toastSuccess).toHaveBeenCalledTimes(1)
    expect(wrapper.findComponent({ name: 'RttTerminalPanel' }).props('inputEnabled')).toBe(true)
    wrapper.unmount()
  })

  it('reconnects to an active YMODEM transfer and can cancel it', async () => {
    let current = ymodemStatus({ sent_bytes: 1024, total_bytes: 4096, percent: 25, block: 1 })
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/dash/serial/status')) {
        return jsonResponse({ ...runningStatus(), ymodem: current })
      }
      if (url.endsWith('/api/dash/serial/ymodem/cancel')) {
        expect(init?.method).toBe('POST')
        current = ymodemStatus({
          phase: 'cancelling', sent_bytes: 1024, total_bytes: 4096, percent: 25, block: 1,
        })
        return jsonResponse(current)
      }
      if (url.endsWith('/api/dash/serial/ymodem/status')) {
        if (current.phase !== 'cancelling') return jsonResponse(current)
        current = ymodemStatus({
          state: 'cancelled', active: false, phase: 'cancelled',
          sent_bytes: 1024, total_bytes: 4096, percent: 25, block: 1,
          error: 'cancelled by user',
        })
        return jsonResponse(current)
      }
      if (url.includes('/api/dash/serial/ymodem/trace?')) {
        return jsonResponse({ transfer_id: 1, entries: [], next_seq: 0, dropped: 0 })
      }
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(SerialMonitorTab)
    await vi.waitFor(() => expect(wrapper.text()).toContain('25% · 1024/4096 B'))
    expect(wrapper.get('[data-testid="serial-ymodem-file"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="serial-log-mode"]').attributes('disabled')).toBeDefined()
    expect(wrapper.findComponent({ name: 'RttTerminalPanel' }).props('inputEnabled')).toBe(false)

    await wrapper.get('[data-testid="serial-ymodem-cancel"]').trigger('click')
    await vi.waitFor(() => expect(mocks.toastInfo).toHaveBeenCalledTimes(1))
    expect(mocks.terminalWrites.join('')).toContain('[YMODEM] \u5df2\u53d6\u6d88\uff1acancelled by user')
    expect(wrapper.find('[data-testid="serial-ymodem-cancel"]').exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'RttTerminalPanel' }).props('inputEnabled')).toBe(true)
    wrapper.unmount()
  })
})
