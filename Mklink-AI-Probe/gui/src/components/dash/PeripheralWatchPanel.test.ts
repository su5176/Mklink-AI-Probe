import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import PeripheralWatchPanel from './PeripheralWatchPanel.vue'

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

it('loads a selected Pack chip, samples a GPIO bit without AXF, and keeps failed changes unchecked', async () => {
  const target = { id: 'chip', target: 'STM32F103RE', pack: 'Keil.DFP@1', svd: 'SVD/F103.svd' }
  const item = { name: 'GPIOB.12', register: 'GPIOB.IDR', address: '0x40010C08' }
  let fail = false
  const fetchMock = vi.fn(async (url: string, options?: RequestInit) => {
    let data: object = {}
    if (url.includes('/targets')) data = { targets: [target] }
    else if (url.endsWith('/select')) {
      expect(JSON.parse(String(options?.body))).toEqual({ target_id: 'chip' })
      data = { selection: target, items: [item] }
    } else if (url.endsWith('/add')) {
      expect(JSON.parse(String(options?.body))).toEqual({ name: 'GPIOB.12' })
      data = fail ? { item: { error: 'Cannot sample' } } : { item }
    } else if (url.endsWith('/items')) data = { items: [] }
    else data = { items: [] }
    return { ok: true, json: async () => data }
  })
  vi.stubGlobal('fetch', fetchMock)
  const wrapper = mount(PeripheralWatchPanel, { props: { deviceConnected: true, latestValues: { 'GPIOB.12': 1 } } })
  await flushPromises()
  await wrapper.get('[data-testid="peripheral-chip-search"]').trigger('focus')
  await wrapper.get('[data-testid="peripheral-chip-search"]').trigger('keydown', { key: 'Enter' })
  expect((wrapper.get('[data-testid="peripheral-svd"]').element as HTMLSelectElement).value).toBe('chip')
  expect(wrapper.get('[data-testid="peripheral-svd"]').text()).toContain('F103.svd')
  await wrapper.get('[data-testid="peripheral-load"]').trigger('click')
  await flushPromises()
  expect(wrapper.get('[data-testid="peripheral-source"]').text()).toContain('STM32F103RE')
  expect(wrapper.get('output').text()).toBe('1')
  await wrapper.get('[data-testid="peripheral-GPIOB.12"]').setValue(true)
  await flushPromises()
  expect((wrapper.get('[data-testid="peripheral-GPIOB.12"]').element as HTMLInputElement).checked).toBe(true)
  await wrapper.get('[data-testid="peripheral-GPIOB.12"]').setValue(false)
  await flushPromises()
  fail = true
  await wrapper.get('[data-testid="peripheral-GPIOB.12"]').setValue(true)
  await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toContain('Cannot sample')
  expect((wrapper.get('[data-testid="peripheral-GPIOB.12"]').element as HTMLInputElement).checked).toBe(false)
  await wrapper.setProps({ deviceConnected: false })
  expect(wrapper.find('[data-testid="peripheral-GPIOB.12"]').exists()).toBe(false)
  expect(fetchMock.mock.calls.every(([url]) => !url.includes('/symbols/'))).toBe(true)
  wrapper.unmount()
})
