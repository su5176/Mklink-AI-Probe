import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import AboutDialog from './AboutDialog.vue'

describe('AboutDialog', () => {
  it('identifies ETERNAL CHIP software branding and MicroKeen hardware branding', async () => {
    const wrapper = mount(AboutDialog, {
      props: { version: '0.1.6', buildCommit: 'abcdef123456' },
      attachTo: document.body,
    })

    await wrapper.get('[data-testid="about-trigger"]').trigger('click')
    const dialog = document.querySelector('[data-testid="about-dialog"]')
    expect(dialog?.textContent).toContain('MKLink AI Probe')
    expect(dialog?.textContent).toContain('MicroKeen')
    expect(dialog?.textContent).toContain('v0.1.6')
    expect(dialog?.querySelector('img[alt="ETERNAL CHIP"]')).not.toBeNull()
    wrapper.unmount()
  })
})
