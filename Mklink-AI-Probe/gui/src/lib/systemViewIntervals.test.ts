import { describe, expect, it } from 'vitest'
import { ingestSystemViewIntervals, type SystemViewIntervalState } from './systemViewIntervals'

describe('ingestSystemViewIntervals', () => {
  it('closes RT-Thread task runs on task_stop_ready events', () => {
    const state: SystemViewIntervalState = { currentTaskId: null, currentStart: null }
    const runTime = new Map<number, number>()
    const switches = new Map<number, number>()
    const taskNames = new Map<number, string>()

    const intervals = ingestSystemViewIntervals([
      { kind: 'task_stop_ready', task_id: 536898380, task_name: 'tidle0', t: 11130124925, tk: 11130124925 },
      { kind: 'task_start_ready', task_id: 536896344, task_name: 'afe', t: 11130125350, tk: 11130125350 },
      { kind: 'task_start_exec', task_id: 536896344, task_name: 'afe', t: 11130126754, tk: 11130126754 },
      { kind: 'task_stop_ready', task_id: 536896344, task_name: 'afe', t: 11130129975, tk: 11130129975 },
      { kind: 'idle', t: 11130131413, tk: 11130131413 },
    ], state, {
      ensureTask: (id, name) => {
        if (name) taskNames.set(id, name)
      },
      addRunTime: (id, duration) => runTime.set(id, (runTime.get(id) || 0) + duration),
      addSwitch: id => switches.set(id, (switches.get(id) || 0) + 1),
    })

    expect(intervals).toEqual([
      {
        taskId: 536896344,
        start: 11130126754,
        end: 11130129975,
        startTk: 11130126754,
        endTk: 11130129975,
      },
    ])
    expect(runTime.get(536896344)).toBe(3221)
    expect(switches.get(536896344)).toBe(1)
    expect(taskNames.get(536896344)).toBe('afe')
    expect(state.currentTaskId).toBeNull()
  })

  it('closes the previous running task when a new task starts', () => {
    const state: SystemViewIntervalState = { currentTaskId: null, currentStart: null }
    const runTime = new Map<number, number>()

    const intervals = ingestSystemViewIntervals([
      { kind: 'task_start_exec', task_id: 1, t: 100, tk: 100 },
      { kind: 'task_start_exec', task_id: 2, t: 160, tk: 160 },
    ], state, {
      ensureTask: () => {},
      addRunTime: (id, duration) => runTime.set(id, (runTime.get(id) || 0) + duration),
      addSwitch: () => {},
    })

    expect(intervals).toEqual([{ taskId: 1, start: 100, end: 160, startTk: 100, endTk: 160 }])
    expect(runTime.get(1)).toBe(60)
    expect(state.currentTaskId).toBe(2)
  })
})
