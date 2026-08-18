import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import SuperWatchTab from './SuperWatchTab.vue'
import SymbolVariablePanel from './SymbolVariablePanel.vue'
import WaveformViewer from './WaveformViewer.vue'
import ArraySnapshotViewer from './ArraySnapshotViewer.vue'

describe('SuperWatchTab', () => {
  it('shares waveform values and channel visibility between both workspace panes', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ snapshot: null }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )))
    const wrapper = mount(SuperWatchTab, {
      props: { deviceConnected: true },
      global: {
        stubs: {
          SymbolVariablePanel: {
            name: 'SymbolVariablePanel',
            emits: ['visibility-change', 'selection-removed', 'snapshot-change'],
            props: ['deviceConnected', 'latestValues', 'hiddenChannels', 'snapshotPath'],
            template: '<aside class="variable-panel-stub" />',
          },
          WaveformViewer: {
            name: 'WaveformViewer',
            emits: ['latest-values'],
            props: ['mode', 'deviceConnected', 'hiddenChannels'],
            template: '<main class="waveform-stub" />',
          },
          ArraySnapshotViewer: {
            name: 'ArraySnapshotViewer',
            emits: ['close'],
            props: ['path', 'deviceConnected'],
            template: '<section class="array-snapshot-stub" />',
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

    wrapper.findComponent(SymbolVariablePanel).vm.$emit('selection-removed', 'gain')
    await nextTick()
    expect(wrapper.findComponent(SymbolVariablePanel).props('hiddenChannels')).toEqual(new Set())
    expect(wrapper.findComponent(WaveformViewer).props('hiddenChannels')).toEqual(new Set())

    wrapper.findComponent(SymbolVariablePanel).vm.$emit('snapshot-change', 'samples')
    await nextTick()
    expect(wrapper.findComponent(ArraySnapshotViewer).props('path')).toBe('samples')
    expect(wrapper.findComponent(SymbolVariablePanel).props('snapshotPath')).toBe('samples')

    wrapper.findComponent(ArraySnapshotViewer).vm.$emit('close')
    await nextTick()
    expect(wrapper.findComponent(ArraySnapshotViewer).exists()).toBe(false)
    await flushPromises()
  })
})
