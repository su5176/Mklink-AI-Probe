import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import type { DesktopSettings } from '../../lib/desktopSettings'
import RttTransmitBar from './RttTransmitBar.vue'

function settings(overrides: Partial<DesktopSettings> = {}): DesktopSettings {
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

describe('RttTransmitBar', () => {
  it('renders the compact controls in the reference order and toggles Abc/Hex', async () => {
    const wrapper = mount(RttTransmitBar, {
      props: { enabled: true, settings: settings(), send: vi.fn() },
    })

    expect(wrapper.get('[data-testid="rtt-transmit-bar"]').findAll(':scope > *').map(node =>
      node.attributes('data-testid') ?? node.classes()[0],
    )).toEqual([
      'rtt-format', 'rtt-direction', 'rtt-input', 'rtt-clear', 'history-control', 'rtt-ending', 'rtt-send',
    ])
    expect(wrapper.get('[data-testid="rtt-history"]').element.tagName).toBe('BUTTON')
    expect(wrapper.get('[data-testid="rtt-format"]').text()).toBe('Abc')
    await wrapper.get('[data-testid="rtt-format"]').trigger('click')
    expect(wrapper.get('[data-testid="rtt-format"]').text()).toBe('Hex')
    expect(wrapper.emitted('settings-change')?.at(-1)?.[0]).toMatchObject({ transmitMode: 'hex' })
  })

  it('offers literal line endings and sends exact bytes by click or Enter', async () => {
    const send = vi.fn().mockResolvedValue(undefined)
    const wrapper = mount(RttTransmitBar, {
      props: { enabled: true, settings: settings(), send },
    })
    const ending = wrapper.get('[data-testid="rtt-ending"]')
    expect(ending.findAll('option').map(option => option.text())).toEqual(['无', '\\r', '\\n', '\\r\\n'])

    await wrapper.get('[data-testid="rtt-input"]').setValue('ping')
    await ending.setValue('\r\n')
    await wrapper.get('[data-testid="rtt-send"]').trigger('click')
    expect(send).toHaveBeenLastCalledWith(Uint8Array.of(0x70, 0x69, 0x6e, 0x67, 0x0d, 0x0a))
    expect((wrapper.get('[data-testid="rtt-input"]').element as HTMLInputElement).value).toBe('ping')

    await wrapper.get('[data-testid="rtt-input"]').setValue('ok')
    await wrapper.get('[data-testid="rtt-input"]').trigger('keydown', { key: 'Enter' })
    expect(send).toHaveBeenCalledTimes(2)
    expect(send).toHaveBeenLastCalledWith(Uint8Array.of(0x6f, 0x6b, 0x0d, 0x0a))
    expect((wrapper.get('[data-testid="rtt-input"]').element as HTMLInputElement).value).toBe('ok')
  })

  it('does not send while Enter is part of IME composition', async () => {
    const send = vi.fn().mockResolvedValue(undefined)
    const wrapper = mount(RttTransmitBar, {
      props: { enabled: true, settings: settings(), send },
    })
    await wrapper.get('[data-testid="rtt-input"]').setValue('中文')
    await wrapper.get('[data-testid="rtt-input"]').trigger('keydown', {
      key: 'Enter', isComposing: true,
    })
    expect(send).not.toHaveBeenCalled()
  })

  it('sends a selected line ending without a main payload and blocks a fully empty payload', async () => {
    const send = vi.fn().mockResolvedValue(undefined)
    const wrapper = mount(RttTransmitBar, {
      props: { enabled: true, settings: settings({ lineEnding: '\r\n' }), send },
    })

    expect(wrapper.get('[data-testid="rtt-send"]').attributes('disabled')).toBeUndefined()
    await wrapper.get('[data-testid="rtt-input"]').trigger('keydown', { key: 'Enter' })
    expect(send).toHaveBeenLastCalledWith(Uint8Array.of(0x0d, 0x0a))

    await wrapper.get('[data-testid="rtt-format"]').trigger('click')
    await wrapper.get('[data-testid="rtt-input"]').setValue('   ')
    expect(wrapper.get('[data-testid="rtt-send"]').attributes('disabled')).toBeUndefined()
    await wrapper.get('[data-testid="rtt-send"]').trigger('click')
    expect(send).toHaveBeenLastCalledWith(Uint8Array.of(0x0d, 0x0a))

    await wrapper.get('[data-testid="rtt-clear"]').trigger('click')
    await wrapper.get('[data-testid="rtt-ending"]').setValue('')
    expect(wrapper.get('[data-testid="rtt-send"]').attributes('disabled')).toBeDefined()
  })

  it('is gated by the enabled prop and supports clearing input', async () => {
    const send = vi.fn().mockResolvedValue(undefined)
    const wrapper = mount(RttTransmitBar, {
      props: { enabled: false, settings: settings(), send },
    })
    await wrapper.get('[data-testid="rtt-input"]').setValue('blocked')
    expect(wrapper.get('[data-testid="rtt-send"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[data-testid="rtt-send"]').trigger('click')
    expect(send).not.toHaveBeenCalled()

    await wrapper.get('[data-testid="rtt-clear"]').trigger('click')
    expect((wrapper.get('[data-testid="rtt-input"]').element as HTMLInputElement).value).toBe('')
  })

  it('locks mutable controls while a send is in flight', async () => {
    let resolveSend!: () => void
    const send = vi.fn().mockReturnValue(new Promise<void>(resolve => { resolveSend = resolve }))
    const wrapper = mount(RttTransmitBar, {
      props: { enabled: true, settings: settings(), send },
    })

    await wrapper.get('[data-testid="rtt-input"]').setValue('status?')
    await wrapper.get('[data-testid="rtt-send"]').trigger('click')

    for (const testId of ['rtt-format', 'rtt-input', 'rtt-clear', 'rtt-history', 'rtt-ending', 'rtt-send']) {
      expect(wrapper.get(`[data-testid="${testId}"]`).attributes('disabled')).toBeDefined()
    }

    resolveSend()
    await Promise.resolve()
  })

  it('records the submitted snapshot when state changes during a pending send', async () => {
    let resolveSend!: () => void
    const send = vi.fn().mockReturnValue(new Promise<void>(resolve => { resolveSend = resolve }))
    const wrapper = mount(RttTransmitBar, {
      props: { enabled: true, settings: settings(), send },
    })

    await wrapper.get('[data-testid="rtt-input"]').setValue('status?')
    await wrapper.get('[data-testid="rtt-send"]').trigger('click')
    await wrapper.setProps({ settings: settings({ transmitMode: 'hex', lineEnding: '\n' }) })
    ;(wrapper.vm as unknown as { input: string }).input = 'AA 55'

    resolveSend()
    await Promise.resolve()
    await Promise.resolve()

    const changed = wrapper.emitted('settings-change')?.at(-1)?.[0] as DesktopSettings
    expect(changed.sendHistory[0]).toMatchObject({
      text: 'status?', mode: 'text', lineEnding: '',
    })
    expect((wrapper.get('[data-testid="rtt-input"]').element as HTMLInputElement).value).toBe('AA 55')
  })

  it('records successful history, retains failures, and restores without sending', async () => {
    const send = vi.fn()
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error('写入失败'))
    const wrapper = mount(RttTransmitBar, {
      props: { enabled: true, settings: settings(), send },
    })

    await wrapper.get('[data-testid="rtt-input"]').setValue('status?')
    await wrapper.get('[data-testid="rtt-send"]').trigger('click')
    const changed = wrapper.emitted('settings-change')?.at(-1)?.[0] as DesktopSettings
    expect(changed.sendHistory[0]).toMatchObject({
      text: 'status?', mode: 'text', lineEnding: '',
    })
    expect((wrapper.get('[data-testid="rtt-input"]').element as HTMLInputElement).value).toBe('status?')

    await wrapper.setProps({ settings: changed })
    await wrapper.get('[data-testid="rtt-format"]').trigger('click')
    await wrapper.get('[data-testid="rtt-input"]').setValue('AA 55')
    await wrapper.get('[data-testid="rtt-send"]').trigger('click')
    expect((wrapper.get('[data-testid="rtt-input"]').element as HTMLInputElement).value).toBe('AA 55')
    expect(wrapper.text()).toContain('写入失败')

    await wrapper.get('[data-testid="rtt-history"]').trigger('click')
    await wrapper.get('[data-testid="rtt-history-item-0"]').trigger('click')
    expect((wrapper.get('[data-testid="rtt-input"]').element as HTMLInputElement).value).toBe('status?')
    expect(wrapper.get('[data-testid="rtt-format"]').text()).toBe('Abc')
    expect(send).toHaveBeenCalledTimes(2)
  })
})
