import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import VirtualLogPanel from './VirtualLogPanel.vue'

describe('VirtualLogPanel', () => {
  afterEach(() => vi.useRealTimers())

  it('trims to 5000 entries and renders only visible rows with spacers', async () => {
    vi.useFakeTimers()
    const wrapper = mount(VirtualLogPanel)
    const panel = wrapper.get('[role="log"]').element as HTMLElement
    Object.defineProperty(panel, 'clientHeight', { configurable: true, value: 240 })
    ;(wrapper.vm as any).append(Array.from({ length: 5100 }, (_, index) => ({
      time: index, level: 'raw', text: `line-${index + 1}`,
    })))
    vi.advanceTimersByTime(100)
    await nextTick()
    expect((wrapper.vm as any).retainedCount).toBe(5000)
    expect((wrapper.vm as any).firstLineNumber).toBe(101)
    expect(wrapper.findAll('.virtual-log-row').length).toBeLessThan(40)
    expect(wrapper.get('.virtual-log-spacer').attributes('style')).toContain('height: 110000px')
    wrapper.unmount()
  })

  it('follows only within 24px, disables follow after user scroll, and resumes at bottom', async () => {
    vi.useFakeTimers()
    const wrapper = mount(VirtualLogPanel)
    const panel = wrapper.get('[role="log"]').element as HTMLElement
    Object.defineProperties(panel, {
      clientHeight: { configurable: true, value: 100 },
      scrollHeight: { configurable: true, get: () => (wrapper.vm as any).retainedCount * 22 },
    })
    ;(wrapper.vm as any).append(Array.from({ length: 20 }, (_, index) => ({
      time: index, level: 'data', text: `line-${index}`,
    })))
    vi.advanceTimersByTime(100)
    await nextTick()
    expect(panel.scrollTop).toBe(440)

    panel.scrollTop = 100
    await wrapper.get('[role="log"]').trigger('scroll')
    ;(wrapper.vm as any).append([{ time: 21, level: 'raw', text: 'held' }])
    vi.advanceTimersByTime(100)
    await nextTick()
    expect(panel.scrollTop).toBe(100)
    expect((wrapper.vm as any).following).toBe(false)

    panel.scrollTop = panel.scrollHeight - panel.clientHeight - 20
    await wrapper.get('[role="log"]').trigger('scroll')
    expect((wrapper.vm as any).following).toBe(true)
    ;(wrapper.vm as any).append([{ time: 22, level: 'warning', text: 'follow' }])
    vi.advanceTimersByTime(100)
    await nextTick()
    expect(panel.scrollTop).toBe(panel.scrollHeight)
    wrapper.unmount()
  })

  it('coalesces appends to about 30Hz and cancels pending work on unmount', async () => {
    vi.useFakeTimers()
    const wrapper = mount(VirtualLogPanel)
    for (let index = 0; index < 20; index++) {
      ;(wrapper.vm as any).append([{ time: index, level: 'raw', text: String(index) }])
    }
    expect((wrapper.vm as any).retainedCount).toBe(0)
    vi.advanceTimersByTime(32)
    expect((wrapper.vm as any).retainedCount).toBe(0)
    vi.advanceTimersByTime(1)
    await nextTick()
    expect((wrapper.vm as any).retainedCount).toBe(20)
    ;(wrapper.vm as any).append([{ time: 21, level: 'raw', text: 'pending' }])
    wrapper.unmount()
    vi.advanceTimersByTime(100)
  })

  it('recomputes visible rows after a resize without rendering the full log', async () => {
    vi.useFakeTimers()
    const wrapper = mount(VirtualLogPanel)
    const panel = wrapper.get('[role="log"]').element as HTMLElement
    let height = 44
    Object.defineProperty(panel, 'clientHeight', { configurable: true, get: () => height })
    ;(wrapper.vm as any).append(Array.from({ length: 1000 }, (_, index) => ({
      time: index, level: 'raw', text: String(index),
    })))
    vi.advanceTimersByTime(100)
    window.dispatchEvent(new Event('resize'))
    await nextTick()
    const compact = wrapper.findAll('.virtual-log-row').length
    height = 440
    window.dispatchEvent(new Event('resize'))
    await nextTick()
    const expanded = wrapper.findAll('.virtual-log-row').length
    expect(expanded).toBeGreaterThan(compact)
    expect(expanded).toBeLessThan(40)
    wrapper.unmount()
  })

  it('exports every retained row including pending data as tab-separated text', () => {
    vi.useFakeTimers()
    const wrapper = mount(VirtualLogPanel)
    ;(wrapper.vm as any).append([
      { time: 1_000_000n, level: 'data', label: 'RX', text: '4F4B  OK' },
      { time: 2_000_000n, level: 'warning', label: 'TX', text: '41  A' },
    ])

    const exported = (wrapper.vm as any).exportText()

    expect(exported).toMatch(/\d{2}:00:00\.001\tRX\t4F4B  OK/)
    expect(exported).toMatch(/\d{2}:00:00\.002\tTX\t41  A/)
    expect((wrapper.vm as any).retainedCount).toBe(2)
    wrapper.unmount()
  })
})
