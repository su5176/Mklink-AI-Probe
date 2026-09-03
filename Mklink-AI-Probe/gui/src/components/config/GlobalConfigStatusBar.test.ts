import { shallowMount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import GlobalConfigStatusBar from './GlobalConfigStatusBar.vue'

describe('GlobalConfigStatusBar', () => {
  it('does not append the detected MCU family to the connection label', () => {
    const wrapper = shallowMount(GlobalConfigStatusBar, {
      props: {
        deviceStatus: {
          connected: true,
          state: 'connected',
          mcu: 'STM32F10x',
          idcode: '0x1ba01477',
          port: 'PROBE_PORT',
          axf: { loaded: false },
        } as never,
        configStatus: null,
        microkeen: null,
      },
    })

    expect(wrapper.text()).toContain('已连接')
    expect(wrapper.text()).not.toContain('STM32F10x')
    wrapper.unmount()
  })
})
