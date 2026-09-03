import { flushPromises, shallowMount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import DashboardView from './DashboardView.vue'
import { setLanguage } from '../composables/useLanguage'

const routerMock = vi.hoisted(() => ({
  query: {} as Record<string, string>,
}))
const apiMock = vi.hoisted(() => ({
  deviceStatus: {
    __v_isRef: true,
    value: {
      connected: true,
      state: 'connected',
      mcu: 'STM32F103RC',
      idcode: '0x410',
      port: 'PROBE_PORT',
      axf: { loaded: true },
    },
  },
  connectDevice: vi.fn(),
  disconnectDevice: vi.fn(),
  resetDevice: vi.fn(),
  rebootProbe: vi.fn(),
}))
const confirmMock = vi.hoisted(() => vi.fn())
const toastMock = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() }),
  useRoute: () => ({ query: routerMock.query }),
}))

vi.mock('../composables/useMklinkApi', () => ({
  useMklinkApi: () => ({
    deviceStatus: apiMock.deviceStatus,
    connectDevice: apiMock.connectDevice,
    disconnectDevice: apiMock.disconnectDevice,
    uploadFileSource: vi.fn(),
    parseAxf: vi.fn(),
    flashDevice: vi.fn(),
    resetDevice: apiMock.resetDevice,
    rebootProbe: apiMock.rebootProbe,
    eraseDevice: vi.fn(),
    haltDevice: vi.fn(),
    resumeDevice: vi.fn(),
  }),
}))

vi.mock('../composables/useToast', () => ({
  useToast: () => toastMock,
}))

vi.mock('../composables/useResourceStatus', () => ({
  useResourceStatus: () => ({
    refresh: vi.fn(),
    getBridgeOwner: () => '',
  }),
}))

const dashStub = { template: '<div />', props: ['deviceConnected'] }

describe('DashboardView layout classes', () => {
  beforeEach(() => {
    confirmMock.mockReturnValue(true)
    vi.stubGlobal('confirm', confirmMock)
  })

  afterEach(() => {
    routerMock.query = {}
    apiMock.deviceStatus.value.connected = true
    apiMock.deviceStatus.value.mcu = 'STM32F103RC'
    apiMock.deviceStatus.value.axf = { loaded: true }
    vi.clearAllMocks()
    vi.unstubAllGlobals()
    setLanguage('zh')
  })

  it('keeps an explicit Device connect or disconnect action in the dashboard header', async () => {
    apiMock.disconnectDevice.mockImplementationOnce(async () => {
      apiMock.deviceStatus.value.connected = false
    })
    apiMock.connectDevice.mockImplementationOnce(async () => {
      apiMock.deviceStatus.value.connected = true
      return apiMock.deviceStatus.value
    })
    const wrapper = shallowMount(DashboardView, {
      global: {
        stubs: {
          RttViewTab: dashStub,
          HardFaultTab: dashStub,
          SymbolsTab: dashStub,
          MemoryTab: dashStub,
          SuperWatchTab: dashStub,
          SerialMonitorTab: dashStub,
          ModbusTab: dashStub,
          SystemViewTab: dashStub,
        },
      },
    })

    expect(wrapper.get('[data-testid="device-quick-action"]').text()).toContain('断开')
    await wrapper.get('[data-testid="device-quick-action"]').trigger('click')
    expect(apiMock.disconnectDevice).toHaveBeenCalledOnce()
    expect(wrapper.get('[data-testid="device-quick-action"]').text()).toContain('连接设备')

    await wrapper.get('[data-testid="device-quick-action"]').trigger('click')
    expect(apiMock.connectDevice).toHaveBeenCalledWith(expect.objectContaining({ restore_last: true }))
  })

  it('dismisses the current connection error and shows a later failure again', async () => {
    apiMock.deviceStatus.value.connected = false
    apiMock.connectDevice
      .mockRejectedValueOnce(new Error('first failure'))
      .mockRejectedValueOnce(new Error('second failure'))
    const wrapper = shallowMount(DashboardView, {
      global: {
        stubs: {
          RttViewTab: dashStub,
          HardFaultTab: dashStub,
          SymbolsTab: dashStub,
          MemoryTab: dashStub,
          SuperWatchTab: dashStub,
          SerialMonitorTab: dashStub,
          ModbusTab: dashStub,
          SystemViewTab: dashStub,
        },
      },
    })

    await wrapper.get('[data-testid="device-quick-action"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('.device-quick-error').text()).toContain('first failure')

    await wrapper.get('[data-testid="dismiss-connection-error"]').trigger('click')
    expect(wrapper.find('.device-quick-error').exists()).toBe(false)

    await wrapper.get('[data-testid="device-quick-action"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('.device-quick-error').text()).toContain('second failure')
  })

  it('resets the connected MCU from the dashboard header', async () => {
    apiMock.resetDevice.mockResolvedValueOnce({ status: 'ok' })
    const wrapper = shallowMount(DashboardView, {
      global: {
        stubs: {
          RttViewTab: dashStub,
          HardFaultTab: dashStub,
          SymbolsTab: dashStub,
          MemoryTab: dashStub,
          SuperWatchTab: dashStub,
          SerialMonitorTab: dashStub,
          ModbusTab: dashStub,
          SystemViewTab: dashStub,
        },
      },
    })

    const reset = wrapper.get('[data-testid="mcu-reset-action"]')
    expect(reset.text()).toContain('重启 MCU')
    expect(wrapper.get('[data-testid="reboot-probe"]').text()).toContain('重启 MKLink')
    expect(reset.attributes('disabled')).toBeUndefined()
    await reset.trigger('click')
    await flushPromises()
    expect(apiMock.resetDevice).toHaveBeenCalledOnce()
    expect(toastMock.success).toHaveBeenCalledWith('MCU 已复位')
    wrapper.unmount()

    apiMock.deviceStatus.value.connected = false
    const disconnected = shallowMount(DashboardView, {
      global: {
        stubs: {
          RttViewTab: dashStub,
          HardFaultTab: dashStub,
          SymbolsTab: dashStub,
          MemoryTab: dashStub,
          SuperWatchTab: dashStub,
          SerialMonitorTab: dashStub,
          ModbusTab: dashStub,
          SystemViewTab: dashStub,
        },
      },
    })
    expect(disconnected.get('[data-testid="mcu-reset-action"]').attributes('disabled')).toBeDefined()
    expect(disconnected.get('[data-testid="reboot-probe"]').attributes('disabled')).toBeDefined()
  })

  it('confirms MKLink reboot and reports reconnect guidance', async () => {
    apiMock.rebootProbe.mockResolvedValueOnce({ status: 'rebooted', connected: false })
    const wrapper = shallowMount(DashboardView, {
      global: {
        stubs: {
          RttViewTab: dashStub,
          HardFaultTab: dashStub,
          SymbolsTab: dashStub,
          MemoryTab: dashStub,
          SuperWatchTab: dashStub,
          SerialMonitorTab: dashStub,
          ModbusTab: dashStub,
          SystemViewTab: dashStub,
        },
      },
    })

    confirmMock.mockReturnValueOnce(false)
    await wrapper.get('[data-testid="reboot-probe"]').trigger('click')
    await flushPromises()
    expect(apiMock.rebootProbe).not.toHaveBeenCalled()

    confirmMock.mockReturnValueOnce(true)
    await wrapper.get('[data-testid="reboot-probe"]').trigger('click')
    await flushPromises()
    expect(confirmMock).toHaveBeenCalledTimes(2)
    expect(apiMock.rebootProbe).toHaveBeenCalledOnce()
    expect(toastMock.success).toHaveBeenCalledWith(expect.stringContaining('MKLink 已重启'))
    expect(toastMock.success).toHaveBeenCalledWith(expect.stringContaining('重新枚举后再连接'))
  })

  it('reports MCU reset failures', async () => {
    apiMock.resetDevice.mockRejectedValueOnce(new Error('reset failed'))
    const wrapper = shallowMount(DashboardView, {
      global: {
        stubs: {
          RttViewTab: dashStub,
          HardFaultTab: dashStub,
          SymbolsTab: dashStub,
          MemoryTab: dashStub,
          SuperWatchTab: dashStub,
          SerialMonitorTab: dashStub,
          ModbusTab: dashStub,
          SystemViewTab: dashStub,
        },
      },
    })

    await wrapper.get('[data-testid="mcu-reset-action"]').trigger('click')
    await flushPromises()
    expect(toastMock.error).toHaveBeenCalledWith('MCU 复位失败：reset failed')
  })

  it('places SuperWatch immediately after RTT View', () => {
    const wrapper = shallowMount(DashboardView, {
      global: {
        stubs: {
          RttViewTab: dashStub,
          HardFaultTab: dashStub,
          SymbolsTab: dashStub,
          MemoryTab: dashStub,
          SuperWatchTab: dashStub,
          SerialMonitorTab: dashStub,
          ModbusTab: dashStub,
          SystemViewTab: dashStub,
        },
      },
    })

    expect(wrapper.findAll('.tab-btn').map(button => button.text()).slice(0, 2))
      .toEqual(['RTT View', 'SuperWatch'])
    wrapper.unmount()
  })

  it('uses the required dashboard tab order and keeps Symbols last', () => {
    const wrapper = shallowMount(DashboardView, {
      global: {
        stubs: {
          RttViewTab: dashStub,
          HardFaultTab: dashStub,
          SymbolsTab: dashStub,
          MemoryTab: dashStub,
          SuperWatchTab: dashStub,
          SerialMonitorTab: dashStub,
          ModbusTab: dashStub,
          SystemViewTab: dashStub,
        },
      },
    })

    const labels = wrapper.findAll('.tab-btn').map(button => button.text())
    expect(labels).toEqual([
      'RTT View',
      'SuperWatch',
      'HardFault',
      'Memory',
      '串口助手',
      'Modbus',
      'RTOS Trace',
      '符号表',
    ])
    wrapper.unmount()
  })

  it('keeps technical tab names and translates the remaining tabs in English mode', async () => {
    setLanguage('en')
    const wrapper = shallowMount(DashboardView, {
      global: {
        stubs: {
          RttViewTab: dashStub,
          HardFaultTab: dashStub,
          SymbolsTab: dashStub,
          MemoryTab: dashStub,
          SuperWatchTab: dashStub,
          SerialMonitorTab: dashStub,
          ModbusTab: dashStub,
          SystemViewTab: dashStub,
        },
      },
    })

    expect(wrapper.findAll('.tab-btn').map(button => button.text())).toEqual([
      'RTT View',
      'SuperWatch',
      'HardFault',
      'Memory',
      'Serial Assistant',
      'Modbus',
      'RTOS Trace',
      'Symbols',
    ])
    wrapper.unmount()
  })

  it('does not expose the MCU family label or Debug Control', () => {
    const wrapper = shallowMount(DashboardView, {
      global: {
        stubs: {
          RttViewTab: dashStub,
          HardFaultTab: dashStub,
          SymbolsTab: dashStub,
          MemoryTab: dashStub,
          SuperWatchTab: dashStub,
          SerialMonitorTab: dashStub,
          ModbusTab: dashStub,
          SystemViewTab: dashStub,
        },
      },
    })

    expect(wrapper.text()).not.toContain('STM32F103RC')
    expect(wrapper.findAll('.tab-btn').map(button => button.text())).not.toContain('调试控制')
    wrapper.unmount()
  })

  it('uses one compact navigation row without a duplicate dashboard title', () => {
    const wrapper = shallowMount(DashboardView, {
      global: {
        stubs: {
          RttViewTab: dashStub,
          HardFaultTab: dashStub,
          SymbolsTab: dashStub,
          MemoryTab: dashStub,
          SuperWatchTab: dashStub,
          SerialMonitorTab: dashStub,
          ModbusTab: dashStub,
          SystemViewTab: dashStub,
        },
      },
    })

    expect(wrapper.find('.dashboard-nav-row').exists()).toBe(true)
    expect(wrapper.find('.dashboard-nav-row > .tabs-bar').exists()).toBe(true)
    expect(wrapper.find('.card-title-row').exists()).toBe(false)
    expect(wrapper.find('.card-title').exists()).toBe(false)
    wrapper.unmount()
  })

  it('does not use the full-screen clipped card layout for RTOS Trace', async () => {
    const wrapper = shallowMount(DashboardView, {
      global: {
        stubs: {
          RttViewTab: dashStub,
          HardFaultTab: dashStub,
          SymbolsTab: dashStub,
          MemoryTab: dashStub,
          SuperWatchTab: dashStub,
          SerialMonitorTab: dashStub,
          ModbusTab: dashStub,
          VofaTab: dashStub,
          SystemViewTab: { template: '<div class="sv-tab" />', props: ['deviceConnected'] },
        },
      },
    })

    const systemViewTab = wrapper.findAll('button').find(button => button.text() === 'RTOS Trace')
    expect(systemViewTab).toBeTruthy()
    await systemViewTab!.trigger('click')

    const cardClasses = wrapper.get('.card').classes()
    expect(cardClasses).toContain('card-systemview')
    expect(cardClasses).not.toContain('card-full')
  })

  it('keeps the RTOS Trace card scrollable when content is taller than the viewport', () => {
    const source = readFileSync('src/views/DashboardView.vue', 'utf8')

    expect(source).toMatch(/\.dash-root\s*\{[^}]*min-height:\s*0/s)
    expect(source).toMatch(/\.card-systemview\s*\{[^}]*flex:\s*1\s+1\s+auto/s)
    expect(source).toMatch(/\.card-systemview\s*\{[^}]*min-height:\s*0/s)
    expect(source).toMatch(/\.card-systemview\s*\{[^}]*max-height:\s*100%/s)
    expect(source).toMatch(/\.card-systemview\s*\{[^}]*overflow-y:\s*auto/s)
    expect(source).toMatch(/\.card-systemview\s*\{[^}]*scrollbar-gutter:\s*stable/s)
    expect(source).not.toMatch(/\.card-systemview\s*\{[^}]*calc\(100vh/s)
  })

  it('does not trap ordinary wheel scrolling inside the SystemView timeline', () => {
    const source = readFileSync('src/components/dash/SystemViewTab.vue', 'utf8')

    expect(source).toMatch(/\.sv-canvas-wrap\s*\{[^}]*overflow:\s*visible/s)
    expect(source).not.toMatch(/\.sv-canvas-wrap\s*\{[^}]*overflow:\s*auto/s)
  })

  it('lets the SystemView timeline size itself from its context lanes', () => {
    const source = readFileSync('src/components/dash/SystemViewTab.vue', 'utf8')

    expect(source).toMatch(/\.sv-gantt-section\s*\{[^}]*flex:\s*0\s+0\s+auto/s)
  })

  it('keeps the live SystemView legend bounded without a duplicate CPU panel', () => {
    const source = readFileSync('src/components/dash/SystemViewTab.vue', 'utf8')

    expect(source).toMatch(/\.sv-legend\s*\{[^}]*height:\s*26px/s)
    expect(source).toMatch(/\.sv-legend\s*\{[^}]*overflow-y:\s*auto/s)
    expect(source).not.toContain('sv-vcpu')
    expect(source).not.toContain('CPU Usage in Visible Window')
  })

  it('uses the binary SystemView stream with bounded render and table cadences', () => {
    const source = readFileSync('src/components/dash/SystemViewTab.vue', 'utf8')

    expect(source).toMatch(/useBinaryStream\('systemview'/)
    expect(source).toMatch(/new RenderScheduler/)
    expect(source).toMatch(/TABLE_UPDATE_INTERVAL_MS\s*=\s*200/)
    expect(source).not.toMatch(/passthroughEvents:\s*\['status',\s*'batch'\]/)
    expect(source).not.toMatch(/pendingLiveEvents/)
    expect(source).toContain('dp.isr_names')
    expect(source).toContain('isr_name:')
    expect(source).toContain('visible.contexts')
    expect(source).toContain('exactRuntimeRows')
  })

  it('can open directly on the RTOS Trace tab from the route query', () => {
    routerMock.query = { tab: 'systemview' }

    const wrapper = shallowMount(DashboardView, {
      global: {
        stubs: {
          RttViewTab: dashStub,
          HardFaultTab: dashStub,
          SymbolsTab: dashStub,
          MemoryTab: dashStub,
          SuperWatchTab: dashStub,
          SerialMonitorTab: dashStub,
          ModbusTab: dashStub,
          VofaTab: dashStub,
          SystemViewTab: { template: '<div class="sv-tab" />', props: ['deviceConnected'] },
        },
      },
    })

    expect(wrapper.get('.card').classes()).toContain('card-systemview')
  })

  it('can open directly on the SuperWatch tab from the route query', () => {
    routerMock.query = { tab: 'superwatch' }

    const wrapper = shallowMount(DashboardView, {
      global: {
        stubs: {
          RttViewTab: dashStub,
          HardFaultTab: dashStub,
          SymbolsTab: dashStub,
          MemoryTab: dashStub,
          SuperWatchTab: { template: '<div class="superwatch-route-probe" />', props: ['deviceConnected'] },
          SerialMonitorTab: dashStub,
          ModbusTab: dashStub,
          VofaTab: dashStub,
          SystemViewTab: dashStub,
        },
      },
    })

    expect(wrapper.find('.superwatch-route-probe').exists()).toBe(true)
  })

  it('removes the VOFA+ desktop entry and degrades legacy links to RTT View', () => {
    routerMock.query = { tab: 'vofa' }

    const wrapper = shallowMount(DashboardView, {
      global: {
        stubs: {
          RttViewTab: { template: '<div class="rtt-route-probe" />', props: ['deviceConnected'] },
          HardFaultTab: dashStub,
          SymbolsTab: dashStub,
          MemoryTab: dashStub,
          SuperWatchTab: dashStub,
          SerialMonitorTab: dashStub,
          ModbusTab: dashStub,
          SystemViewTab: dashStub,
        },
      },
    })

    expect(wrapper.text()).not.toContain('VOFA+')
    expect(wrapper.find('.rtt-route-probe').exists()).toBe(true)
    const rttButton = wrapper.findAll('button').find(button => button.text() === 'RTT View')
    expect(rttButton?.classes()).toContain('active')
  })

  it('reconnects SystemView control and binary streams when opening a running trace', () => {
    const source = readFileSync('src/components/dash/SystemViewTab.vue', 'utf8')

    expect(source).toContain('async function reconnectRunningTrace')
    expect(source).toContain('dash.getStatus()')
    expect(source).toMatch(/if\s*\(status\?\.running\)/)
    expect(source).toContain('connectStatus()')
    expect(source).toContain('binaryStream.start()')
  })

  it('stops both SystemView transports when the tab unmounts', () => {
    const source = readFileSync('src/components/dash/SystemViewTab.vue', 'utf8')
    const cleanup = source.slice(source.indexOf('onUnmounted(() => {'), source.indexOf('\n})', source.indexOf('onUnmounted(() => {')))

    expect(cleanup).toContain('disconnectStatus()')
    expect(cleanup).toContain('binaryStream.stop()')
    expect(cleanup).toContain('cancelPendingConnect()')
    expect(cleanup).toContain('renderScheduler?.dispose()')
    expect(cleanup).toContain('tlInstance?.destroy()')
  })
})
