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

  it('places explicit ISR, Scheduler, Task, and Idle contexts in the requested order', () => {
    const timeline = Object.create(SvTimeline.prototype)
    Object.assign(timeline, {
      PALETTE: ['#1', '#2', '#3'],
      hidden: new Set(),
      follow: false,
      windowSize: 0,
      viewStart: null,
      viewEnd: null,
      _hadIntervals: false,
      _explicitContexts: [],
      _filterContinuous: intervals => intervals,
      _layout: () => {},
      _draw: () => {},
      _updateStatus: () => {},
    })
    timeline.setData([
      { tid: 4, name: 'Idle', type: 'Idle', start: 0, end: 20 },
      { tid: 3, name: 'main', type: 'Task', start: 20, end: 30 },
      { tid: 1, name: 'mchtmr', type: 'ISR', start: 30, end: 31 },
      { tid: 2, name: 'Scheduler', type: 'Scheduler', start: 31, end: 32 },
    ])
    timeline.setContexts([
      { tid: 1, name: 'mchtmr', type: 'ISR' },
      { tid: 2, name: 'Scheduler', type: 'Scheduler' },
      { tid: 3, name: 'main', type: 'Task' },
      { tid: 4, name: 'Idle', type: 'Idle' },
    ])

    expect(timeline.tasks.map(task => task.name)).toEqual(['mchtmr', 'Scheduler', 'main', 'Idle'])
  })

  it('keeps context legend order stable without repeating CPU percentages', () => {
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
    }
    timeline.toggleTask = vi.fn()

    timeline._updateStatus()

    const labels = [...timeline.roots.legend.querySelectorAll('.sv-lg')]
      .map(el => el.textContent.trim())
    expect(labels).toEqual(['afe', 'svfast'])
    expect(timeline.roots.legend.textContent).not.toContain('%')
  })

  it('keeps the axis and interval labels in microseconds', () => {
    const timeline = Object.create(SvTimeline.prototype)
    timeline.unit = 'us'

    expect(timeline._fmtAxisValue(32.768, 0.1)).toBe('32.8 us')
    expect(timeline._fmtIntervalLabel({ start: 0, end: 0.6 })).toBe('0.6 us')
    expect(timeline._fmtIntervalLabel({ start: 0, end: 443.5 })).toBe('443.5 us')
    expect(timeline._fmtTime(1_250_000)).toBe('1,250,000 us')
  })

  it('uses conventional context colors for ISR, Scheduler, and Idle', () => {
    const timeline = Object.create(SvTimeline.prototype)
    timeline.PALETTE = ['#task']

    expect(timeline._contextColor('ISR', 0)).toBe('#c52832')
    expect(timeline._contextColor('Scheduler', 0)).toBe('#727983')
    expect(timeline._contextColor('Idle', 0)).toBe('#8b929a')
    expect(timeline._contextColor('Task', 0)).toBe('#task')
  })

  it('draws subpixel task intervals instead of leaving a busy timeline blank', () => {
    const timeline = Object.create(SvTimeline.prototype)
    const fillRect = vi.fn()
    const strokeRect = vi.fn()
    const task = { tid: 1, name: 'main', type: 'Task', color: '#123456' }
    Object.assign(timeline, {
      _renderPaused: false,
      ctx: {
        clearRect: vi.fn(), fillRect, strokeRect, fillText: vi.fn(),
        setLineDash: vi.fn(), beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(),
        stroke: vi.fn(), closePath: vi.fn(),
      },
      W: 240, H: 80, plotX0: 40, plotX1: 240, plotW: 200,
      rulerH: 42, laneH: 28, viewStart: 0, viewEnd: 1_000,
      hidden: new Set(), hover: null, markerTime: null, unit: 'us',
      intervals: [{ tid: 1, name: 'main', start: 500, end: 500.1 }],
      tasks: [task], lanes: [task], taskOf: new Map([[1, task]]),
      _drawLaneBackgrounds: vi.fn(), _drawRuler: vi.fn(), _drawMarker: vi.fn(),
      _fmtIntervalLabel: vi.fn(() => '0.1 us'), _labelWidth: vi.fn(() => 40),
    })

    timeline._draw()

    expect(fillRect).toHaveBeenCalledWith(140, 45, 0.8, 22)
    expect(strokeRect).not.toHaveBeenCalled()
    expect(timeline._fmtIntervalLabel).not.toHaveBeenCalled()
    expect(timeline._labelWidth).not.toHaveBeenCalled()
  })

  it('zooms with a modified wheel event over the plot and then pans by dragging', () => {
    const timeline = Object.create(SvTimeline.prototype)
    const canvas = document.createElement('canvas')
    canvas.getBoundingClientRect = () => ({ left: 0, top: 0, width: 240, height: 80, right: 240, bottom: 80, x: 0, y: 0, toJSON: () => ({}) })
    Object.assign(timeline, {
      roots: { canvas }, canvas, unit: 'us', W: 240, H: 80,
      plotX0: 40, plotX1: 240, plotW: 200,
      tMin: 0, tMax: 1_000, viewStart: 0, viewEnd: 1_000,
      dragging: false, follow: true,
      _resize: vi.fn(), _draw: vi.fn(), _updateStatus: vi.fn(),
      _hitTest: vi.fn(() => null), _showTip: vi.fn(), _hideTip: vi.fn(),
    })

    timeline._bind()
    const wheel = new WheelEvent('wheel', { deltaY: -100, ctrlKey: true, bubbles: true, cancelable: true })
    Object.defineProperties(wheel, {
      clientX: { value: 140 },
      clientY: { value: 20 },
      ctrlKey: { value: true },
    })
    canvas.dispatchEvent(wheel)

    expect(wheel.defaultPrevented).toBe(true)
    expect(timeline.follow).toBe(true)
    expect(timeline.followSpan).toBe(800)
    expect(timeline.viewEnd - timeline.viewStart).toBeCloseTo(800)
    const zoomedStart = timeline.viewStart

    canvas.dispatchEvent(new MouseEvent('mousedown', { clientX: 140, clientY: 30, button: 0, bubbles: true, cancelable: true }))
    window.dispatchEvent(new MouseEvent('mousemove', { clientX: 120, clientY: 30 }))
    window.dispatchEvent(new MouseEvent('mouseup', { clientX: 120, clientY: 30 }))

    expect(timeline.viewStart).toBeGreaterThan(zoomedStart)
    expect(timeline.viewEnd - timeline.viewStart).toBeCloseTo(800)
    timeline.destroy()
  })

  it('leaves wheel scrolling available over the task-name column', () => {
    const timeline = Object.create(SvTimeline.prototype)
    timeline.plotX0 = 40
    timeline.plotX1 = 240

    expect(timeline._shouldZoomWheel(20, { ctrlKey: true })).toBe(false)
    expect(timeline._shouldZoomWheel(140, { ctrlKey: false })).toBe(false)
    expect(timeline._shouldZoomWheel(140, { ctrlKey: true })).toBe(true)
  })

  it('leaves an ordinary wheel event available for page scrolling', () => {
    const timeline = Object.create(SvTimeline.prototype)
    const canvas = document.createElement('canvas')
    canvas.getBoundingClientRect = () => ({ left: 0, top: 0, width: 240, height: 80, right: 240, bottom: 80, x: 0, y: 0, toJSON: () => ({}) })
    Object.assign(timeline, {
      roots: { canvas }, canvas, W: 240, H: 80,
      plotX0: 40, plotX1: 240, plotW: 200,
      tMin: 0, tMax: 1_000, viewStart: 0, viewEnd: 1_000,
      dragging: false, follow: true,
      _resize: vi.fn(), _draw: vi.fn(), _updateStatus: vi.fn(),
      _hitTest: vi.fn(() => null), _showTip: vi.fn(), _hideTip: vi.fn(),
    })

    timeline._bind()
    const wheel = new WheelEvent('wheel', { deltaY: 100, bubbles: true, cancelable: true })
    Object.defineProperties(wheel, { clientX: { value: 140 }, clientY: { value: 20 } })
    canvas.dispatchEvent(wheel)

    expect(wheel.defaultPrevented).toBe(false)
    expect(timeline.follow).toBe(true)
    expect(timeline.viewStart).toBe(0)
    expect(timeline.viewEnd).toBe(1_000)
    timeline.destroy()
  })

  it('keeps live follow for a click and disables it only after dragging', () => {
    const timeline = Object.create(SvTimeline.prototype)
    const canvas = document.createElement('canvas')
    canvas.getBoundingClientRect = () => ({ left: 0, top: 0, width: 240, height: 80, right: 240, bottom: 80, x: 0, y: 0, toJSON: () => ({}) })
    Object.assign(timeline, {
      roots: { canvas }, canvas, W: 240, H: 80,
      plotX0: 40, plotX1: 240, plotW: 200,
      tMin: 0, tMax: 1_000, viewStart: 0, viewEnd: 1_000,
      dragging: false, follow: true,
      _resize: vi.fn(), _draw: vi.fn(), _updateStatus: vi.fn(),
      _hitTest: vi.fn(() => null), _showTip: vi.fn(), _hideTip: vi.fn(),
      setFollowMode: vi.fn(function (enabled) { this.follow = enabled }),
    })

    timeline._bind()
    canvas.dispatchEvent(new MouseEvent('mousedown', { clientX: 140, clientY: 30, button: 0, bubbles: true, cancelable: true }))
    window.dispatchEvent(new MouseEvent('mouseup', { clientX: 140, clientY: 30 }))
    expect(timeline.follow).toBe(true)
    expect(timeline.setFollowMode).not.toHaveBeenCalled()

    canvas.dispatchEvent(new MouseEvent('mousedown', { clientX: 140, clientY: 30, button: 0, bubbles: true, cancelable: true }))
    window.dispatchEvent(new MouseEvent('mousemove', { clientX: 120, clientY: 30 }))
    expect(timeline.follow).toBe(false)
    expect(timeline.setFollowMode).toHaveBeenCalledWith(false)
    window.dispatchEvent(new MouseEvent('mouseup', { clientX: 120, clientY: 30 }))
    timeline.destroy()
  })

  it('keeps the inspected live frame stable until follow mode resumes', () => {
    const timeline = Object.create(SvTimeline.prototype)
    timeline.follow = false
    timeline._hadIntervals = true
    timeline.viewStart = 100
    timeline.viewEnd = 200
    timeline.intervals = [{ tid: 1, name: 'main', start: 100, end: 200 }]
    timeline._acceptData = vi.fn()

    timeline.setPrefilteredIntervals([{ tid: 2, name: 'Idle', start: 300, end: 400 }])
    expect(timeline._acceptData).toHaveBeenCalledOnce()
    expect(timeline._acceptData).toHaveBeenLastCalledWith(
      [{ tid: 2, name: 'Idle', start: 300, end: 400 }],
      { render: false, preserveDataRange: true },
    )

    timeline.follow = true
    timeline.setPrefilteredIntervals([{ tid: 2, name: 'Idle', start: 300, end: 400 }])
    expect(timeline._acceptData).toHaveBeenCalledTimes(2)
  })

  it('preserves a manual view range while accepting new intervals', () => {
    const timeline = Object.create(SvTimeline.prototype)
    Object.assign(timeline, {
      follow: false,
      viewStart: 100,
      viewEnd: 200,
      _hadIntervals: true,
      _filterContinuous: intervals => intervals,
      _layout: vi.fn(() => false),
      _draw: vi.fn(),
      _updateStatus: vi.fn(),
      hidden: new Set(),
      PALETTE: ['#1'],
      _taskOrder: [],
      _taskMeta: new Map(),
      _explicitContexts: [],
    })

    timeline.setPrefilteredIntervals([{ tid: 1, name: 'main', start: 150, end: 180 }])

    expect(timeline.getViewRange()).toEqual({ start: 100, end: 200 })
    expect(timeline._draw).not.toHaveBeenCalled()
  })

  it('keeps accumulated data bounds when the live viewport advances', () => {
    const timeline = Object.create(SvTimeline.prototype)
    Object.assign(timeline, {
      PALETTE: ['#1'],
      hidden: new Set(),
      follow: true,
      windowSize: 100,
      viewStart: null,
      viewEnd: null,
      _hadIntervals: false,
      _explicitContexts: [],
      _filterContinuous: intervals => intervals,
      _layout: vi.fn(() => false),
      _draw: vi.fn(),
      _updateStatus: vi.fn(),
      _taskOrder: [],
      _taskMeta: new Map(),
    })

    timeline.setPrefilteredIntervals([{ tid: 1, name: 'main', start: 900, end: 950 }])
    expect([timeline.tMin, timeline.tMax]).toEqual([900, 950])
    timeline.setPrefilteredIntervals([])
    expect(timeline._hadIntervals).toBe(true)
    timeline.setPrefilteredIntervals([{ tid: 1, name: 'main', start: 950, end: 1_000 }])

    expect([timeline.tMin, timeline.tMax]).toEqual([900, 1_000])
    expect(timeline._targetFollowRange()).toEqual({ start: 900, end: 1_000 })
  })

  it('positions an interval tooltip without throwing at the viewport edge', () => {
    const timeline = Object.create(SvTimeline.prototype)
    const tooltip = document.createElement('div')
    Object.defineProperties(tooltip, {
      offsetWidth: { value: 120 },
      offsetHeight: { value: 60 },
    })
    timeline.roots = { tooltip }
    timeline.unit = 'us'
    timeline.tickOrigin = 0n
    timeline.tickHz = 0

    expect(() => timeline._showTip(window.innerWidth, window.innerHeight, {
      tid: 1, name: '<main>', start: 10, end: 20,
    })).not.toThrow()
    expect(tooltip.style.left).toBe(`${window.innerWidth - 128}px`)
    expect(tooltip.style.top).toBe(`${window.innerHeight - 68}px`)
    expect(tooltip.innerHTML).toContain('&lt;main&gt;')
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

  it('keeps the live window aligned to the newest event', () => {
    const timeline = Object.create(SvTimeline.prototype)
    Object.assign(timeline, {
      follow: true,
      windowSize: 100,
      tMin: 0,
      tMax: 131,
    })

    expect(timeline._targetFollowRange()).toEqual({ start: 31, end: 131 })
    timeline.tMax = 199.999
    expect(timeline._targetFollowRange()).toEqual({ start: 99.999, end: 199.999 })
    timeline.tMax = 200.001
    expect(timeline._targetFollowRange()).toEqual({ start: 100.001, end: 200.001 })
  })

  it('moves the live ruler continuously as new data arrives', () => {
    const now = vi.spyOn(performance, 'now').mockReturnValue(0)
    const timeline = Object.create(SvTimeline.prototype)
    Object.assign(timeline, {
      PALETTE: ['#1'],
      hidden: new Set(),
      follow: true,
      windowSize: 100,
      _hadIntervals: false,
      _filterContinuous: intervals => intervals,
      _layout: vi.fn(),
      viewStart: 0,
      viewEnd: 100,
      _draw: vi.fn(),
      _updateStatus: vi.fn(),
      _drawLive: vi.fn(() => true),
    })

    timeline.setData([{ tid: 1, name: 'main', start: 10, end: 30 }])
    expect([timeline.viewStart, timeline.viewEnd]).toEqual([-70, 30])
    now.mockReturnValue(10)
    timeline.setData([{ tid: 1, name: 'main', start: 10, end: 90 }])
    expect([timeline.viewStart, timeline.viewEnd]).toEqual([-70, 30])
    expect(timeline._followTarget).toEqual({ start: -10, end: 90 })
    timeline.renderFrame(110)
    expect([timeline.viewStart, timeline.viewEnd]).toEqual([-10, 90])
    now.mockReturnValue(120)
    timeline.setData([{ tid: 1, name: 'main', start: 10, end: 101 }])
    expect([timeline.viewStart, timeline.viewEnd]).toEqual([-10, 90])
    expect(timeline._followTarget).toEqual({ start: 1, end: 101 })
    timeline.renderFrame(220)
    expect([timeline.viewStart, timeline.viewEnd]).toEqual([1, 101])

    expect(timeline._drawLive).toHaveBeenCalledTimes(3)
    now.mockRestore()
  })

  it('draws each live update in a continuously advancing window', () => {
    const timeline = Object.create(SvTimeline.prototype)
    Object.assign(timeline, {
      PALETTE: ['#1'],
      hidden: new Set(),
      follow: true,
      windowSize: 100,
      viewStart: 0,
      viewEnd: 100,
      _hadIntervals: true,
      _filterContinuous: intervals => intervals,
      _layout: vi.fn(() => false),
      _drawLive: vi.fn(() => true),
      _draw: vi.fn(),
      _updateStatus: vi.fn(),
    })

    timeline.setData([{ tid: 1, name: 'main', start: 40, end: 60 }])
    expect([timeline.viewStart, timeline.viewEnd]).toEqual([0, 100])
    expect(timeline._followTarget).toEqual({ start: -40, end: 60 })
    timeline.renderFrame((timeline._followTransitionAt || 0) + 100)
    expect([timeline.viewStart, timeline.viewEnd]).toEqual([-40, 60])
    timeline.setData([{ tid: 1, name: 'main', start: 101, end: 120 }])
    expect([timeline.viewStart, timeline.viewEnd]).toEqual([-40, 60])
    expect(timeline._followTarget).toEqual({ start: 20, end: 120 })
    timeline.renderFrame((timeline._followTransitionAt || 0) + 100)
    expect([timeline.viewStart, timeline.viewEnd]).toEqual([20, 120])
    expect(timeline._drawLive).toHaveBeenCalledTimes(2)
  })

  it('interpolates the live view on animation frames', () => {
    const timeline = Object.create(SvTimeline.prototype)
    Object.assign(timeline, {
      follow: true,
      windowSize: 100,
      viewStart: 0,
      viewEnd: 100,
      _followTarget: { start: 20, end: 120 },
      _followTransitionAt: 0,
      _renderPaused: false,
      _draw: vi.fn(),
    })

    timeline.renderFrame(50)
    expect(timeline.viewStart).toBeGreaterThan(0)
    expect(timeline.viewStart).toBeLessThan(20)
    expect(timeline.viewEnd).toBeGreaterThan(100)
    expect(timeline._draw).toHaveBeenCalledOnce()

    timeline.renderFrame(100)
    expect([timeline.viewStart, timeline.viewEnd]).toEqual([20, 120])
    expect(timeline._followTarget).toBeNull()
  })

  it('restarts follow interpolation from the displayed frame for each data batch', () => {
    const now = vi.spyOn(performance, 'now').mockReturnValue(0)
    const timeline = Object.create(SvTimeline.prototype)
    Object.assign(timeline, {
      PALETTE: ['#1'],
      hidden: new Set(),
      follow: true,
      windowSize: 100,
      _hadIntervals: false,
      _filterContinuous: intervals => intervals,
      _layout: vi.fn(),
      _drawLive: vi.fn(() => true),
      _draw: vi.fn(),
      _updateStatus: vi.fn(),
      viewStart: 0,
      viewEnd: 100,
      _taskOrder: [],
      _taskMeta: new Map(),
      _explicitContexts: [],
    })

    timeline.setData([{ tid: 1, name: 'main', start: 0, end: 100 }])
    now.mockReturnValue(10)
    timeline.setData([{ tid: 1, name: 'main', start: 0, end: 120 }])
    timeline.renderFrame(60)
    now.mockReturnValue(70)
    timeline.renderFrame(70)
    const displayed = { start: timeline.viewStart, end: timeline.viewEnd }

    timeline.setData([{ tid: 1, name: 'main', start: 0, end: 140 }])

    expect(timeline._followFrom).toEqual(displayed)
    expect(timeline._followTransitionAt).toBe(70)
    expect([timeline.viewStart, timeline.viewEnd]).toEqual([displayed.start, displayed.end])

    timeline.renderFrame(120)
    expect(timeline.viewStart).toBeGreaterThan(displayed.start)
    expect(timeline.viewStart).toBeLessThan(40)
    expect(timeline.viewEnd).toBeGreaterThan(displayed.end)
    expect(timeline.viewEnd).toBeLessThan(140)

    timeline.renderFrame(170)
    expect([timeline.viewStart, timeline.viewEnd]).toEqual([40, 140])
    expect(timeline._followFrom).toBeNull()
    now.mockRestore()
  })

  it('pauses live rendering and resumes without changing follow mode', () => {
    const timeline = Object.create(SvTimeline.prototype)
    Object.assign(timeline, {
      follow: true,
      windowSize: 100,
      _lastLiveRender: 0,
      _draw: vi.fn(),
      _updateStatus: vi.fn(),
      _layout: vi.fn(),
    })

    timeline.pauseRendering()
    expect(timeline._drawLive(100)).toBe(false)
    expect(timeline.follow).toBe(true)

    timeline.resumeRendering()
    expect(timeline._layout).toHaveBeenCalledOnce()
    expect(timeline._draw).toHaveBeenCalledOnce()
    expect(timeline.follow).toBe(true)
  })

  it('does not draw while an initially paused timeline is constructed or resized', () => {
    const canvas = document.createElement('canvas')
    canvas.width = 321
    canvas.height = 123
    const context = new Proxy({
      setTransform: vi.fn(),
      clearRect: vi.fn(),
      measureText: vi.fn(() => ({ width: 0 })),
    }, {
      get(target, property) {
        if (!(property in target)) target[property] = vi.fn()
        return target[property]
      },
    })
    canvas.getContext = vi.fn(() => context)
    const timeline = new SvTimeline(
      { canvas },
      {
        intervals: [],
        follow: true,
        windowSize: 100,
        renderPaused: true,
      },
    )

    window.dispatchEvent(new Event('resize'))
    timeline.setData([{ tid: 1, name: 'main', start: 0, end: 10 }])

    expect(context.clearRect).not.toHaveBeenCalled()
    expect(canvas.width).toBe(321)
    expect(canvas.height).toBe(123)
    timeline.destroy()
  })
})

describe('SvTimeline lane layout', () => {
  it('does not clear the canvas repeatedly for a fractional device pixel ratio', () => {
    const canvas = document.createElement('canvas')
    Object.defineProperty(canvas, 'clientWidth', { configurable: true, value: 320 })
    let backingWidth = 320
    let widthWrites = 0
    Object.defineProperty(canvas, 'width', {
      configurable: true,
      get: () => backingWidth,
      set: value => { backingWidth = Math.trunc(value); widthWrites++ },
    })
    const dpr = vi.spyOn(window, 'devicePixelRatio', 'get').mockReturnValue(1.00000003)
    const timeline = Object.create(SvTimeline.prototype)
    Object.assign(timeline, {
      canvas,
      ctx: { setTransform: vi.fn() },
      hidden: new Set(),
      laneCapacity: 2,
      tasks: [{ tid: 1 }, { tid: 2 }],
      rulerH: 42,
      laneH: 28,
      padR: 6,
      renderPaused: false,
    })

    timeline._layout()
    timeline._layout()

    expect(widthWrites).toBe(0)
    expect(backingWidth).toBe(320)
    dpr.mockRestore()
  })

  it('forces the current live frame to redraw after the canvas is resized', () => {
    const timeline = Object.create(SvTimeline.prototype)
    Object.assign(timeline, {
      PALETTE: ['#1'],
      hidden: new Set(),
      follow: true,
      windowSize: 100,
      viewStart: 0,
      viewEnd: 100,
      _hadIntervals: true,
      _filterContinuous: intervals => intervals,
      _layout: vi.fn(() => true),
      _drawLive: vi.fn(() => true),
      _updateStatus: vi.fn(),
    })

    timeline.setData([{ tid: 1, name: 'main', start: 10, end: 30 }])

    expect(timeline._drawLive).toHaveBeenCalledWith(undefined, true)
  })

  it('starts compact and grows only when more task lanes appear', () => {
    const canvas = document.createElement('canvas')
    Object.defineProperty(canvas, 'clientWidth', { configurable: true, value: 320 })
    const timeline = Object.create(SvTimeline.prototype)
    Object.assign(timeline, {
      canvas,
      ctx: { setTransform: vi.fn() },
      hidden: new Set(),
      laneCapacity: 2,
      tasks: [{ tid: 1 }, { tid: 2 }],
      rulerH: 42,
      laneH: 28,
      padR: 6,
      renderPaused: false,
    })

    timeline._layout()
    expect(timeline.H).toBe(102)
    timeline.tasks = [{ tid: 1 }, { tid: 2 }, { tid: 3 }]
    timeline._layout()
    expect(timeline.H).toBe(130)
    timeline.tasks = [{ tid: 1 }]
    timeline._layout()
    expect(timeline.H).toBe(130)
  })
})
