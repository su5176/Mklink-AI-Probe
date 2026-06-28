import { describe, expect, it } from 'vitest'
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

  it('lets ordinary wheel events scroll the surrounding dashboard', () => {
    const timeline = Object.create(SvTimeline.prototype)

    expect(timeline._shouldZoomWheel({ ctrlKey: false, shiftKey: false })).toBe(false)
    expect(timeline._shouldZoomWheel({ ctrlKey: true, shiftKey: false })).toBe(true)
    expect(timeline._shouldZoomWheel({ ctrlKey: false, shiftKey: true })).toBe(true)
  })
})
