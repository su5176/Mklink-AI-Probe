import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { nextTick } from 'vue'
import SuperWatchTab from './SuperWatchTab.vue'
import SymbolVariablePanel from './SymbolVariablePanel.vue'
import WaveformViewer from './WaveformViewer.vue'

describe('SuperWatchTab', () => {
  it('shares waveform values and channel visibility between both workspace panes', async () => {
    const wrapper = mount(SuperWatchTab, {
      props: { deviceConnected: true },
      global: {
        stubs: {
          SymbolVariablePanel: {
            name: 'SymbolVariablePanel',
            emits: ['visibility-change', 'selection-removed', 'snapshot-change'],
            props: ['deviceConnected', 'latestValues', 'hiddenChannels'],
            template: '<aside class="variable-panel-stub" />',
          },
          WaveformViewer: {
            name: 'WaveformViewer',
            emits: ['latest-values'],
            props: ['mode', 'deviceConnected', 'hiddenChannels', 'arraySnapshotPath'],
            template: '<main class="waveform-stub" />',
          },
        },
      },
    })

    expect(wrapper.get('.superwatch-workspace').exists()).toBe(true)
    wrapper.findComponent(WaveformViewer).vm.$emit('latest-values', { gain: 1.25 })
    await nextTick()

    expect(wrapper.findComponent(SymbolVariablePanel).props('latestValues')).toEqual({ gain: 1.25 })

    wrapper.findComponent(SymbolVariablePanel).vm.$emit('visibility-change', 'gain', false)
    await nextTick()
    expect(wrapper.findComponent(SymbolVariablePanel).props('hiddenChannels')).toEqual(new Set(['gain']))
    expect(wrapper.findComponent(WaveformViewer).props('hiddenChannels')).toEqual(new Set(['gain']))
    expect(wrapper.findComponent(WaveformViewer).props('arraySnapshotPath')).toBe(null)
    wrapper.findComponent(SymbolVariablePanel).vm.$emit('snapshot-change', 'samples')
    await nextTick()
    expect(wrapper.findComponent(WaveformViewer).props('arraySnapshotPath')).toBe('samples')

    wrapper.findComponent(SymbolVariablePanel).vm.$emit('selection-removed', 'gain')
    await nextTick()
    expect(wrapper.findComponent(SymbolVariablePanel).props('hiddenChannels')).toEqual(new Set())
    expect(wrapper.findComponent(WaveformViewer).props('hiddenChannels')).toEqual(new Set())
  })
})
