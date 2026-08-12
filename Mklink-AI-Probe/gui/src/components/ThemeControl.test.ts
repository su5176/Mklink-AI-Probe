import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it } from 'vitest'
import { nextTick } from 'vue'
import ThemeControl from './ThemeControl.vue'
import { setTheme } from '../composables/useTheme'

describe('ThemeControl', () => {
  beforeEach(() => {
    localStorage.clear()
    setTheme('porcelain')
  })

  it('applies a selected theme and persists its paired dark theme', async () => {
    const wrapper = mount(ThemeControl, { attachTo: document.body })
    await wrapper.get('[data-testid="theme-trigger"]').trigger('click')
    await wrapper.get('[data-testid="theme-option-mica"]').trigger('click')

    expect(document.documentElement.dataset.theme).toBe('mica')
    expect(document.documentElement.dataset.mode).toBe('light')
    expect(localStorage.getItem('mklink-theme')).toBe('mica')
    expect(wrapper.find('[data-testid="theme-panel"]').exists()).toBe(false)

    await wrapper.get('[data-testid="theme-trigger"]').trigger('click')
    await wrapper.get('[data-testid="theme-pair-toggle"]').trigger('click')
    expect(document.documentElement.dataset.theme).toBe('graphite')
    expect(document.documentElement.dataset.mode).toBe('dark')
    expect(wrapper.find('[data-testid="theme-panel"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('dismisses the theme panel on outside pointer input', async () => {
    const wrapper = mount(ThemeControl, { attachTo: document.body })
    await wrapper.get('[data-testid="theme-trigger"]').trigger('click')
    expect(wrapper.find('[data-testid="theme-panel"]').exists()).toBe(true)

    document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    await nextTick()

    expect(wrapper.find('[data-testid="theme-panel"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('dismisses on Escape and returns focus to the trigger', async () => {
    const wrapper = mount(ThemeControl, { attachTo: document.body })
    const trigger = wrapper.get<HTMLButtonElement>('[data-testid="theme-trigger"]')
    await trigger.trigger('click')

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()

    expect(wrapper.find('[data-testid="theme-panel"]').exists()).toBe(false)
    expect(document.activeElement).toBe(trigger.element)
    wrapper.unmount()
  })
})
