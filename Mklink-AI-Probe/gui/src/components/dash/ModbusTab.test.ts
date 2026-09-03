import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  listPorts: vi.fn(),
  toastError: vi.fn(),
}))

vi.mock('../../composables/useMklinkApi', () => ({
  useMklinkApi: () => ({ listPorts: mocks.listPorts }),
}))

vi.mock('../../composables/useToast', () => ({
  useToast: () => ({ error: mocks.toastError, success: vi.fn(), info: vi.fn() }),
}))

import ModbusTab from './ModbusTab.vue'

describe('ModbusTab prerequisites', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      running: false,
      loop: { running: false, completed: 0, errors: 0 },
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })))
    mocks.listPorts.mockResolvedValue([{
      device: 'SERIAL_PORT', description: 'Virtual serial', manufacturer: 'test', vid: null, pid: null,
    }])
  })

  it('shows serial controls without an MKLink Device prerequisite', async () => {
    const wrapper = mount(ModbusTab)
    await flushPromises()

    expect(wrapper.text()).not.toContain('请先连接设备')
    expect(wrapper.find('select').element.value).toBe('SERIAL_PORT')
    expect(wrapper.findAll('button').some(button => button.text() === '连接')).toBe(true)
  })

  it('surfaces the backend serial-port conflict instead of entering running state', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({
      detail: { conflict: 'user:dashboard:serial', resource: 'serial_port' },
    }), {
      status: 409,
      headers: { 'Content-Type': 'application/json' },
    }))))
    const wrapper = mount(ModbusTab)
    await flushPromises()

    await wrapper.findAll('button').find(button => button.text() === '连接')!.trigger('click')
    await flushPromises()

    expect(mocks.toastError).toHaveBeenCalledWith(expect.stringContaining('user:dashboard:serial'))
    expect(wrapper.findAll('button').some(button => button.text() === '连接')).toBe(true)
  })
})
