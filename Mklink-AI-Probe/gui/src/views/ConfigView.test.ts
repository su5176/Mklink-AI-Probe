import { flushPromises, mount } from '@vue/test-utils'
import { readonly, ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => {
  const deviceStatus = {
    connected: false,
    state: 'disconnected',
    mcu: null,
    idcode: null,
    port: null,
    axf: { loaded: false },
  }

  return {
    deviceStatus,
    api: {
      listPorts: vi.fn(),
      discoverPort: vi.fn(),
      getConfig: vi.fn(),
      updateConfig: vi.fn(),
      connectDevice: vi.fn(),
      disconnectDevice: vi.fn(),
      setPowerOn: vi.fn(),
      rebootProbe: vi.fn(),
      parseAxf: vi.fn(),
      uploadFileSource: vi.fn(),
      probeFirmwareCheck: vi.fn(),
    },
    wsConnect: vi.fn(),
    wsDisconnect: vi.fn(),
    toastError: vi.fn(),
    toastSuccess: vi.fn(),
    loadDesktopSettings: vi.fn(),
    saveDesktopSettings: vi.fn(),
    pickSymbolFile: vi.fn(),
    pickMapFile: vi.fn(),
    refreshSymbolCatalog: vi.fn(),
    confirm: vi.fn(),
  }
})

vi.mock('../composables/useMklinkApi', () => ({
  useMklinkApi: () => ({ deviceStatus: readonly(ref(mocks.deviceStatus)), ...mocks.api }),
}))

vi.mock('../composables/useMklinkWs', () => ({
  useMklinkWs: () => ({
    wsConnected: ref(false),
    connect: mocks.wsConnect,
    disconnect: mocks.wsDisconnect,
  }),
}))

vi.mock('../composables/useToast', () => ({
  useToast: () => ({ error: mocks.toastError, success: mocks.toastSuccess }),
}))

vi.mock('../lib/desktopSettings', async importOriginal => ({
  ...await importOriginal<typeof import('../lib/desktopSettings')>(),
  loadDesktopSettings: mocks.loadDesktopSettings,
  saveDesktopSettings: mocks.saveDesktopSettings,
}))

vi.mock('../lib/filePicker', () => ({
  pickSymbolFile: mocks.pickSymbolFile,
  pickMapFile: mocks.pickMapFile,
}))

vi.mock('../composables/useSymbolCatalog', () => ({
  useSymbolCatalog: () => ({ ensureLoaded: mocks.refreshSymbolCatalog }),
}))

async function mountView() {
  const { default: ConfigView } = await import('./ConfigView.vue')
  const wrapper = mount(ConfigView, {
    global: {
      stubs: {
        FirmwareUpdateModal: true,
      },
    },
  })
  await flushPromises()
  return wrapper
}

describe('ConfigView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.api.parseAxf.mockReset()
    Object.assign(mocks.deviceStatus, {
      connected: false,
      state: 'disconnected',
      mcu: null,
      idcode: null,
      port: null,
      axf: { loaded: false },
    })
    mocks.api.listPorts.mockResolvedValue([
      { device: 'TEST_PORT_A', description: 'MKLink A', manufacturer: 'MicroLink', vid: 1, pid: 2 },
      { device: 'TEST_PORT_B', description: 'MKLink', manufacturer: 'MicroLink', vid: 1, pid: 2 },
    ])
    mocks.api.discoverPort.mockResolvedValue({ port: 'TEST_PORT_B' })
    mocks.api.getConfig.mockResolvedValue({ com_port: 'TEST_PORT_A', swd_clock: '2000000' })
    mocks.api.updateConfig.mockResolvedValue({})
    mocks.api.connectDevice.mockResolvedValue({})
    mocks.api.disconnectDevice.mockResolvedValue(undefined)
    mocks.api.setPowerOn.mockResolvedValue({ status: 'ok' })
    mocks.api.rebootProbe.mockResolvedValue({ status: 'rebooted', connected: false })
    mocks.api.parseAxf.mockResolvedValue({
      loaded: true,
      axf_path: 'C:\\saved\\app.axf',
      variable_count: 3,
    })
    mocks.api.uploadFileSource.mockResolvedValue({ path: '' })
    mocks.refreshSymbolCatalog.mockResolvedValue(undefined)
    mocks.api.probeFirmwareCheck.mockResolvedValue({ status: 'ok' })
    mocks.loadDesktopSettings.mockReturnValue({
      version: 1,
      symbolPath: 'C:\\saved\\app.axf',
      mapPath: 'C:\\saved\\app.map',
      rttAddress: '',
      transmitMode: 'text',
      lineEnding: '',
      sendHistory: [],
    })
    mocks.pickSymbolFile.mockResolvedValue(null)
    mocks.pickMapFile.mockResolvedValue(null)
    vi.spyOn(window, 'open').mockImplementation(() => null)
    mocks.confirm.mockReturnValue(true)
    vi.stubGlobal('confirm', mocks.confirm)
  })

  it('renders one four-section workspace with Local Device selected by default', async () => {
    const wrapper = await mountView()

    expect(wrapper.findAll('[data-testid="config-section"]')).toHaveLength(4)
    expect(wrapper.get('[data-testid="config-section-local"]').attributes('aria-current')).toBe('page')
    expect(wrapper.get('[data-testid="local-device-panel"]').exists()).toBe(true)

    const text = wrapper.text()
    expect(text).not.toContain('项目概览')
    expect(text).not.toContain('最近项目')
    expect(text).not.toContain('MCU 类型')
    expect(text).not.toContain('MCU 提示')
    expect(text).not.toContain('高级配置 (RTT)')
    expect(wrapper.find('[data-testid="device-status"]').exists()).toBe(false)
  })

  it('distinguishes readable variables from DWARF type definitions', async () => {
    mocks.deviceStatus.axf = {
      loaded: true,
      axf_path: 'C:\\saved\\app.axf',
      variable_count: 801,
      struct_count: 150,
      enum_count: 12,
    }

    const wrapper = await mountView()
    await wrapper.get('[data-testid="config-section-files"]').trigger('click')

    expect(wrapper.text()).toContain('801 个固定可读变量')
    expect(wrapper.text()).toContain('150 种结构体类型')
    expect(wrapper.text()).toContain('12 种枚举类型')
  })

  it('shows the active symbol source when the edited path is not loaded', async () => {
    mocks.deviceStatus.axf = {
      loaded: true,
      axf_path: 'C:\\old\\firmware.axf',
      variable_count: 801,
      struct_count: 150,
      enum_count: 12,
    }

    const wrapper = await mountView()
    await wrapper.get('[data-testid="config-section-files"]').trigger('click')

    expect(wrapper.get('[data-testid="symbol-source-state"]').text()).toContain('待解析')
    expect(wrapper.get('[data-testid="active-symbol-path"]').text())
      .toContain('C:\\old\\firmware.axf')
  })

  it('uses the restored port as a soft preference when connecting', async () => {
    const wrapper = await mountView()

    await wrapper.get('[data-testid="connect-local"]').trigger('click')
    await flushPromises()

    expect(mocks.api.connectDevice).toHaveBeenCalledWith({
      restore_last: true,
      axf: 'C:\\saved\\app.axf',
    })
    expect(mocks.api.connectDevice.mock.calls[0][0]).not.toHaveProperty('mcu')
  })

  it('shows Auto Search and discovers on connect when no port has been saved', async () => {
    mocks.api.getConfig.mockResolvedValue({})
    const wrapper = await mountView()

    const portSelect = wrapper.get<HTMLSelectElement>('[data-testid="local-port"]')
    expect(portSelect.element.value).toBe('')
    expect(portSelect.text()).toContain('自动搜索')

    await wrapper.get('[data-testid="connect-local"]').trigger('click')
    await flushPromises()

    expect(mocks.api.connectDevice).toHaveBeenCalledWith({
      restore_last: true,
      axf: 'C:\\saved\\app.axf',
    })
  })

  it('falls back to Auto Search after a restored connection fails', async () => {
    mocks.api.connectDevice
      .mockRejectedValueOnce(new Error('saved port unavailable'))
      .mockResolvedValueOnce({ port: 'TEST_PORT_B' })
    const wrapper = await mountView()

    await wrapper.get('[data-testid="connect-local"]').trigger('click')
    await flushPromises()

    expect(wrapper.get<HTMLSelectElement>('[data-testid="local-port"]').element.value).toBe('')

    await wrapper.get('[data-testid="connect-local"]').trigger('click')
    await flushPromises()

    expect(mocks.api.connectDevice).toHaveBeenNthCalledWith(2, {
      restore_last: true,
      axf: 'C:\\saved\\app.axf',
    })
    expect(wrapper.get<HTMLSelectElement>('[data-testid="local-port"]').element.value)
      .toBe('TEST_PORT_B')
  })

  it('shows Auto Search when the restored port is no longer available', async () => {
    mocks.api.listPorts.mockResolvedValue([
      { device: 'TEST_PORT_B', description: 'MKLink', manufacturer: 'MicroLink', vid: 1, pid: 2 },
    ])
    const wrapper = await mountView()

    expect(wrapper.get<HTMLSelectElement>('[data-testid="local-port"]').element.value).toBe('')
    expect(wrapper.get('[data-testid="local-port"]').text()).toContain('自动搜索')
  })

  it('keeps a port selected in the current session as a strict connection target', async () => {
    const wrapper = await mountView()

    await wrapper.get('[data-testid="local-port"]').setValue('TEST_PORT_B')
    await flushPromises()
    await wrapper.get('[data-testid="connect-local"]').trigger('click')
    await flushPromises()

    expect(mocks.api.connectDevice).toHaveBeenCalledWith({
      port: 'TEST_PORT_B',
      axf: 'C:\\saved\\app.axf',
    })
  })

  it('switches a failed strict connection to Auto Search for the next attempt', async () => {
    mocks.api.connectDevice
      .mockRejectedValueOnce(new Error('selected port unavailable'))
      .mockResolvedValueOnce({ port: 'TEST_PORT_A' })
    const wrapper = await mountView()

    await wrapper.get('[data-testid="local-port"]').setValue('TEST_PORT_B')
    await flushPromises()
    await wrapper.get('[data-testid="connect-local"]').trigger('click')
    await flushPromises()

    expect(mocks.api.connectDevice).toHaveBeenNthCalledWith(1, {
      port: 'TEST_PORT_B',
      axf: 'C:\\saved\\app.axf',
    })
    expect(wrapper.get<HTMLSelectElement>('[data-testid="local-port"]').element.value).toBe('')

    await wrapper.get('[data-testid="connect-local"]').trigger('click')
    await flushPromises()

    expect(mocks.api.connectDevice).toHaveBeenNthCalledWith(2, {
      restore_last: true,
      axf: 'C:\\saved\\app.axf',
    })
  })

  it('persists Auto Search by clearing the configured port', async () => {
    const wrapper = await mountView()

    await wrapper.get('[data-testid="local-port"]').setValue('')
    await flushPromises()

    expect(mocks.api.updateConfig).toHaveBeenCalledWith(expect.objectContaining({
      com_port: '',
    }))
  })

  it('fills the detected serial port after an automatic connection succeeds', async () => {
    mocks.api.getConfig.mockResolvedValue({})
    mocks.api.connectDevice.mockResolvedValue({ port: 'TEST_PORT_B' })
    const wrapper = await mountView()

    await wrapper.get('[data-testid="connect-local"]').trigger('click')
    await flushPromises()

    expect((wrapper.get('[data-testid="local-port"]').element as HTMLSelectElement).value)
      .toBe('TEST_PORT_B')
  })

  it('automatically saves serial discovery and SWD changes without a save button', async () => {
    const wrapper = await mountView()

    await wrapper.get('[data-testid="auto-port"]').trigger('click')
    await wrapper.get('[data-testid="swd-clock"]').setValue('4000000')
    await flushPromises()

    expect(mocks.api.discoverPort).toHaveBeenCalledOnce()
    expect(mocks.api.updateConfig).toHaveBeenCalledWith(expect.objectContaining({
      com_port: 'TEST_PORT_B',
      swd_clock: '4000000',
    }))
    expect(wrapper.find('[data-testid="save-local"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="local-auto-save"]').text()).toContain('自动保存')
    expect(wrapper.get('[data-testid="disconnect-local"]').attributes('disabled')).toBeDefined()
  })

  it('sets supported probe voltages and requires a second action for 5V', async () => {
    mocks.deviceStatus.connected = true
    const wrapper = await mountView()

    await wrapper.get('[data-testid="probe-power-3300"]').trigger('click')
    await flushPromises()

    expect(mocks.api.setPowerOn).toHaveBeenCalledWith(3300, false)

    mocks.api.setPowerOn.mockClear()
    mocks.confirm.mockReturnValueOnce(false)
    await wrapper.get('[data-testid="probe-power-5000"]').trigger('click')
    await flushPromises()
    expect(mocks.api.setPowerOn).not.toHaveBeenCalled()

    mocks.confirm.mockReturnValueOnce(true)
    await wrapper.get('[data-testid="probe-power-5000"]').trigger('click')
    await flushPromises()
    expect(mocks.api.setPowerOn).toHaveBeenCalledWith(5000, true)
    expect(mocks.toastSuccess).toHaveBeenCalledWith(expect.stringContaining('5.0V'))
  })

  it('confirms probe reboot and reports that the connection is dropped', async () => {
    mocks.deviceStatus.connected = true
    const wrapper = await mountView()

    await wrapper.get('[data-testid="reboot-probe"]').trigger('click')
    await flushPromises()

    expect(mocks.confirm).toHaveBeenCalledOnce()
    expect(mocks.api.rebootProbe).toHaveBeenCalledOnce()
    expect(mocks.toastSuccess).toHaveBeenCalledWith(expect.stringContaining('MKLink 已重启'))
  })

  it('rejects local SWD clock settings above 10 MHz', async () => {
    const wrapper = await mountView()
    const input = wrapper.get('[data-testid="swd-clock"]')
    expect(input.attributes('max')).toBe('10000000')

    await input.setValue('10000001')
    await flushPromises()

    expect(mocks.api.updateConfig).not.toHaveBeenCalled()
    expect(mocks.toastError).toHaveBeenCalledWith(expect.stringContaining('10 MHz'))
  })

  it('restores and automatically saves independently editable AXF/ELF and MAP paths', async () => {
    const wrapper = await mountView()
    await wrapper.get('[data-testid="config-section-files"]').trigger('click')

    expect(wrapper.get<HTMLInputElement>('[data-testid="symbol-path"]').element.value)
      .toBe('C:\\saved\\app.axf')
    expect(wrapper.get<HTMLInputElement>('[data-testid="map-path"]').element.value)
      .toBe('C:\\saved\\app.map')

    mocks.pickSymbolFile.mockResolvedValueOnce('D:\\build\\next.elf')
    await wrapper.get('[data-testid="browse-symbol"]').trigger('click')
    await flushPromises()
    expect(wrapper.get<HTMLInputElement>('[data-testid="symbol-path"]').element.value)
      .toBe('D:\\build\\next.elf')

    await wrapper.get('[data-testid="map-path"]').setValue('D:\\build\\next.map')
    await wrapper.get('[data-testid="browse-map"]').trigger('click')

    expect(mocks.saveDesktopSettings).toHaveBeenCalledWith(
      window.localStorage,
      expect.objectContaining({
        symbolPath: 'D:\\build\\next.elf',
        mapPath: 'D:\\build\\next.map',
      }),
    )
    expect(wrapper.find('[data-testid="save-files"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="files-auto-save"]').text()).toContain('自动保存')
  })

  it('parses the saved AXF path when a device is connected', async () => {
    Object.assign(mocks.deviceStatus, { connected: true, state: 'halted' })
    const wrapper = await mountView()
    await wrapper.get('[data-testid="config-section-files"]').trigger('click')

    expect(wrapper.get('[data-testid="parse-symbols"]').attributes('disabled')).toBeUndefined()
    await wrapper.get('[data-testid="parse-symbols"]').trigger('click')
    await flushPromises()

    expect(mocks.api.parseAxf).toHaveBeenCalledWith('C:\\saved\\app.axf')
    expect(mocks.refreshSymbolCatalog).toHaveBeenCalledWith(true)
    expect(mocks.toastSuccess).toHaveBeenCalledWith(expect.stringContaining('3'))
  })

  it('shows the browser-selected AXF name while parsing the backend cache path', async () => {
    Object.assign(mocks.deviceStatus, { connected: true, state: 'halted' })
    const selected = new File(['ELF'], 'browser.axf', { type: 'application/octet-stream' })
    mocks.pickSymbolFile.mockResolvedValueOnce(selected)
    mocks.api.uploadFileSource.mockResolvedValueOnce({
      path: 'C:\\Users\\test\\.mklink\\uploads\\file-sources\\uploaded.axf',
    })
    const wrapper = await mountView()
    await wrapper.get('[data-testid="config-section-files"]').trigger('click')

    await wrapper.get('[data-testid="browse-symbol"]').trigger('click')
    await flushPromises()

    expect(mocks.api.uploadFileSource).toHaveBeenCalledWith('symbol', selected)
    expect(wrapper.get<HTMLInputElement>('[data-testid="symbol-path"]').element.value)
      .toBe('browser.axf')
    expect(wrapper.get('[data-testid="symbol-path-validation"]').text()).toContain('浏览器上传')

    mocks.api.parseAxf.mockResolvedValueOnce({
      loaded: true,
      axf_path: 'C:\\Users\\test\\.mklink\\uploads\\file-sources\\uploaded.axf',
      variable_count: 3,
      elf_backend: 'builtin',
      builtin_elf_version: '0.32',
    })
    await wrapper.get('[data-testid="parse-symbols"]').trigger('click')
    await flushPromises()
    expect(mocks.api.parseAxf).toHaveBeenCalledWith(
      'C:\\Users\\test\\.mklink\\uploads\\file-sources\\uploaded.axf',
    )
  })

  it('shows the active builtin symbol parser', async () => {
    mocks.deviceStatus.axf = {
      loaded: true,
      axf_path: 'C:\\saved\\app.axf',
      variable_count: 3,
      elf_backend: 'builtin',
      builtin_elf_version: '0.32',
    }
    const wrapper = await mountView()
    await wrapper.get('[data-testid="config-section-files"]').trigger('click')
    expect(wrapper.get('[data-testid="symbol-parser-backend"]').text())
      .toBe('内置 pyelftools 0.32')
  })

  it('reports a catalog refresh failure separately from successful AXF parsing', async () => {
    Object.assign(mocks.deviceStatus, { connected: true, state: 'halted' })
    mocks.refreshSymbolCatalog.mockRejectedValueOnce(new Error('catalog unavailable'))
    const wrapper = await mountView()
    await wrapper.get('[data-testid="config-section-files"]').trigger('click')

    await wrapper.get('[data-testid="parse-symbols"]').trigger('click')
    await flushPromises()

    expect(mocks.api.parseAxf).toHaveBeenCalledWith('C:\\saved\\app.axf')
    expect(mocks.toastError).toHaveBeenCalledWith('符号目录刷新失败: catalog unavailable')
    expect(mocks.toastError).not.toHaveBeenCalledWith(expect.stringContaining('AXF 解析失败'))
  })

  it('rejects a parse response that still reports another active AXF', async () => {
    Object.assign(mocks.deviceStatus, { connected: true, state: 'halted' })
    mocks.api.parseAxf.mockResolvedValueOnce({
      loaded: true,
      axf_path: 'C:\\old\\firmware.axf',
      variable_count: 801,
    })
    const wrapper = await mountView()
    await wrapper.get('[data-testid="config-section-files"]').trigger('click')

    await wrapper.get('[data-testid="parse-symbols"]').trigger('click')
    await flushPromises()

    expect(mocks.refreshSymbolCatalog).not.toHaveBeenCalled()
    expect(mocks.toastSuccess).not.toHaveBeenCalled()
    expect(mocks.toastError).toHaveBeenCalledWith(expect.stringContaining('C:\\old\\firmware.axf'))
  })

  it('shows inline path validation and does not let an invalid symbol path block connection', async () => {
    mocks.loadDesktopSettings.mockReturnValueOnce({
      version: 1,
      symbolPath: 'C:\\saved\\app.txt',
      mapPath: 'C:\\saved\\app.axf',
      rttAddress: '',
      transmitMode: 'text',
      lineEnding: '',
      sendHistory: [],
    })
    Object.assign(mocks.deviceStatus, { connected: false, state: 'disconnected' })
    const wrapper = await mountView()

    await wrapper.get('[data-testid="connect-local"]').trigger('click')
    expect(mocks.api.connectDevice).toHaveBeenCalledWith({
      restore_last: true,
      axf: undefined,
    })

    await wrapper.get('[data-testid="config-section-files"]').trigger('click')
    expect(wrapper.get('[data-testid="symbol-path-validation"]').text()).toContain('.axf')
    expect(wrapper.get('[data-testid="map-path-validation"]').text()).toContain('.map')
    expect(wrapper.get('[data-testid="parse-symbols"]').attributes('disabled')).toBeDefined()
  })

  it('keeps remote connection and service launch controls reachable', async () => {
    const wrapper = await mountView()

    await wrapper.get('[data-testid="config-section-remote"]').trigger('click')
    await wrapper.get('[data-testid="remote-url"]').setValue('ws://10.0.0.5:8765')
    await wrapper.get('[data-testid="remote-token"]').setValue('secret')
    await wrapper.get('[data-testid="connect-remote"]').trigger('click')
    expect(mocks.wsConnect).toHaveBeenCalledWith('secret', 'ws://10.0.0.5:8765')

    await wrapper.get('[data-testid="config-section-serve"]').trigger('click')
    await wrapper.get('[data-testid="serve-host"]').setValue('0.0.0.0')
    await wrapper.get('[data-testid="serve-port"]').setValue('9000')
    await wrapper.get('[data-testid="launch-server"]').trigger('click')
    expect(window.open).toHaveBeenCalledWith('http://0.0.0.0:9000/docs', '_blank')
  })

  it('preserves the probe firmware upgrade warning', async () => {
    mocks.api.probeFirmwareCheck.mockResolvedValue({
      status: 'upgrade_required',
      instructions: 'upgrade',
      firmware_dir: 'C:\\firmware',
      recommended_uf2: null,
      all_uf2s: [],
    })

    const wrapper = await mountView()

    expect(wrapper.get('[data-testid="firmware-warning"]').text()).toContain('探针固件需要升级')
  })
})
