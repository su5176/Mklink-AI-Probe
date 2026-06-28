import { shallowMount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { reactive } from 'vue'
import { readFileSync } from 'node:fs'
import DashboardView from './DashboardView.vue'

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() }),
}))

vi.mock('../composables/useMklinkApi', () => ({
  useMklinkApi: () => ({
    deviceStatus: reactive({ connected: true }),
    flashDevice: vi.fn(),
    resetDevice: vi.fn(),
    eraseDevice: vi.fn(),
    haltDevice: vi.fn(),
    resumeDevice: vi.fn(),
  }),
}))

vi.mock('../composables/useToast', () => ({
  useToast: () => ({
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  }),
}))

vi.mock('../composables/useResourceStatus', () => ({
  useResourceStatus: () => ({
    refresh: vi.fn(),
    getBridgeOwner: () => '',
  }),
}))

const dashStub = { template: '<div />', props: ['deviceConnected'] }

describe('DashboardView layout classes', () => {
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

  it('lets the SystemView timeline reserve enough height for CPU bars', () => {
    const source = readFileSync('src/components/dash/SystemViewTab.vue', 'utf8')

    expect(source).toMatch(/\.sv-gantt-section\s*\{[^}]*flex:\s*0\s+0\s+auto/s)
  })

  it('keeps live SystemView legend and CPU rows from changing the page height', () => {
    const source = readFileSync('src/components/dash/SystemViewTab.vue', 'utf8')

    expect(source).toMatch(/\.sv-legend\s*\{[^}]*height:\s*28px/s)
    expect(source).toMatch(/\.sv-legend\s*\{[^}]*overflow-y:\s*auto/s)
    expect(source).toMatch(/\.sv-vcpu\s*\{[^}]*height:\s*96px/s)
    expect(source).toMatch(/\.sv-vcpu\s*\{[^}]*overflow-y:\s*auto/s)
  })
})
