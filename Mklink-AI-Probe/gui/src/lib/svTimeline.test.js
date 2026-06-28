import { describe, expect, it, vi } from 'vitest'
import { SvTimeline } from './svTimeline'

describe('SvTimeline continuous filtering', () => {
  it('keeps normal periodic RTOS task gaps inside a live window', () => {
    const timeline = Object.create(SvTimeline.prototype)
    timeline.unit = 'us'
    timeline.tickHz = 72_000_000
    timeline.windowSize = 2_000_000

    const intervals = []
    for (let i = 0; i < 40; i++) {
      const start = i * 50_000
      intervals.push({ tid: 1, name: 'svfast', start, end: start + 180 })
      intervals.push({ tid: 2, name: 'afe', start: start + 700, end: start + 920 })
      if (i % 5 === 0) {
        intervals.push({ tid: 3, name: 'svmid', start: start + 1_400, end: start + 1_900 })
      }
    }

    expect(timeline._filterContinuous(intervals)).toHaveLength(intervals.length)
  })

  it('keeps task lane order stable when runtime percentages cross', () => {
    const timeline = Object.create(SvTimeline.prototype)
    timeline.PALETTE = ['#1', '#2', '#3']
    timeline.hidden = new Set()
    timeline.follow = false
    timeline.windowSize = 0
    timeline.viewStart = null
    timeline.viewEnd = null
    timeline._hadIntervals = false
    timeline._filterContinuous = intervals => intervals
    timeline._layout = () => {}
    timeline._draw = () => {}
    timeline._updateStatus = () => {}

    timeline.setData([
      { tid: 1, name: 'afe', start: 0, end: 60 },
      { tid: 2, name: 'svfast', start: 0, end: 40 },
    ])
    expect(timeline.tasks.map(task => task.name)).toEqual(['afe', 'svfast'])

    timeline.setData([
      { tid: 1, name: 'afe', start: 100, end: 130 },
      { tid: 2, name: 'svfast', start: 100, end: 190 },
    ])
    expect(timeline.tasks.map(task => task.name)).toEqual(['afe', 'svfast'])
  })

  it('keeps visible CPU status order stable when percentages cross', () => {
    const timeline = Object.create(SvTimeline.prototype)
    timeline.hidden = new Set()
    timeline.tasks = [
      { tid: 1, name: 'afe', color: '#1' },
      { tid: 2, name: 'svfast', color: '#2' },
    ]
    timeline.intervals = [
      { tid: 1, name: 'afe', start: 0, end: 30 },
      { tid: 2, name: 'svfast', start: 0, end: 70 },
    ]
    timeline.viewStart = 0
    timeline.viewEnd = 100
    timeline.roots = {
      legend: document.createElement('div'),
      vcpu: document.createElement('div'),
    }
    timeline.toggleTask = vi.fn()

    timeline._updateStatus()

    const labels = [...timeline.roots.legend.querySelectorAll('.sv-lg')]
      .map(el => el.textContent.trim().replace(/\s+\d+(\.\d+)?%$/, ''))
    expect(labels).toEqual(['afe', 'svfast'])
  })

  it('lets ordinary wheel events scroll the surrounding dashboard', () => {
    const timeline = Object.create(SvTimeline.prototype)

    expect(timeline._shouldZoomWheel({ ctrlKey: false, shiftKey: false })).toBe(false)
    expect(timeline._shouldZoomWheel({ ctrlKey: true, shiftKey: false })).toBe(true)
    expect(timeline._shouldZoomWheel({ ctrlKey: false, shiftKey: true })).toBe(true)
  })

  it('removes window listeners after destroy', () => {
    const timeline = Object.create(SvTimeline.prototype)
    const canvas = document.createElement('canvas')
    canvas.getBoundingClientRect = () => ({ left: 0, top: 0, width: 240, height: 80, right: 240, bottom: 80, x: 0, y: 0, toJSON: () => ({}) })
    timeline.roots = { canvas }
    timeline.canvas = canvas
    timeline.W = 240
    timeline.H = 80
    timeline.dragging = false
    timeline._resize = vi.fn()
    timeline._draw = vi.fn()
    timeline._updateStatus = vi.fn()
    timeline._hitTest = vi.fn(() => null)
    timeline._showTip = vi.fn()
    timeline._hideTip = vi.fn()
    timeline.setFollowMode = vi.fn()

    timeline._bind()
    timeline.destroy()
    window.dispatchEvent(new MouseEvent('mousemove', { clientX: 20, clientY: 20 }))
    window.dispatchEvent(new MouseEvent('mouseup'))

    expect(timeline._draw).not.toHaveBeenCalled()
  })
})
