import { shallowMount } from '@vue/test-utils'
import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import StatusBar from './StatusBar.vue'

const deviceStatus = ref({
  connected: true,
  state: 'connected',
  mcu: 'STM32F10x',
  idcode: '0x1ba01477',
})

vi.mock('../composables/useMklinkApi', () => ({
  useMklinkApi: () => ({ deviceStatus }),
}))

vi.mock('../composables/useMklinkWs', () => ({
  useMklinkWs: () => ({ wsConnected: ref(false) }),
}))

vi.mock('../composables/useBackendHealth', () => ({
  useBackendHealth: () => ({
    backendState: ref('alive'),
    backendPort: ref(8766),
    isTauri: false,
    restart: vi.fn(),
  }),
}))

describe('StatusBar', () => {
  it('shows connection state and IDCODE without the detected MCU family label', () => {
    const wrapper = shallowMount(StatusBar)

    expect(wrapper.text()).toContain('已连接')
    expect(wrapper.text()).toContain('0x1ba01477')
    expect(wrapper.text()).toContain('后端正常 · 8766')
    expect(wrapper.text()).not.toContain('STM32F10x')
    wrapper.unmount()
  })
})
