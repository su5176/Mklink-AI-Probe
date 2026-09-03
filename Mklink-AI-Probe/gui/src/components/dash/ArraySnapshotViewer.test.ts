import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ArraySnapshotViewer from './ArraySnapshotViewer.vue'

function okJson(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('ArraySnapshotViewer', () => {
  const context = {
    setTransform: vi.fn(), clearRect: vi.fn(), beginPath: vi.fn(), moveTo: vi.fn(),
    lineTo: vi.fn(), stroke: vi.fn(), fillText: vi.fn(), strokeStyle: '',
    fillStyle: '', lineWidth: 1, font: '', textAlign: 'left',
  }

  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(context as any)
    vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockReturnValue({
      width: 640, height: 220, x: 0, y: 0, top: 0, right: 640, bottom: 220,
      left: 0, toJSON: () => ({}),
    })
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('renders the selected index range and updates the indexed curve', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okJson({
      snapshot: {
        name: 'samples', type_name: 'int16_t', address: 0x20001080,
        element_size: 2, start_index: 64, count: 3, sequence: 7,
        timestamp_us: 25, values: [-2, -1, 3],
      },
    })))
    const wrapper = mount(ArraySnapshotViewer, {
      props: { path: 'samples', deviceConnected: true },
    })

    await vi.advanceTimersByTimeAsync(0)
    await flushPromises()

    expect(wrapper.text()).toContain('64..66')
    expect(wrapper.text()).toContain('#7')
    expect(context.moveTo).toHaveBeenCalled()
    expect(context.lineTo).toHaveBeenCalled()
    wrapper.unmount()
    vi.unstubAllGlobals()
  })
})
