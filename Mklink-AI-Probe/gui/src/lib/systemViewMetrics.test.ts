import { describe, expect, it } from 'vitest'
import {
  buildSystemViewEventRows,
  computeContextRows,
  computeRuntimeRows,
  computeTaskRows,
} from './systemViewMetrics'

describe('systemViewMetrics', () => {
  it('computes runtime distribution rows from task intervals', () => {
    const rows = computeRuntimeRows(
      [
        { id: 1, name: 'svfast', color: '#1', runUs: 150, switches: 4 },
        { id: 2, name: 'svslow', color: '#2', runUs: 90, switches: 1 },
      ],
      [
        { taskId: 1, start: 0, end: 10 },
        { taskId: 1, start: 20, end: 40 },
        { taskId: 1, start: 50, end: 90 },
        { taskId: 1, start: 100, end: 180 },
        { taskId: 2, start: 0, end: 90 },
      ],
    )

    expect(rows[0]).toMatchObject({
      id: 1,
      name: 'svfast',
      count: 4,
      minUs: 10,
      p25Us: 17.5,
      p50Us: 30,
      p75Us: 50,
      maxUs: 80,
      totalUs: 150,
      pct: 62.5,
    })
    expect(rows[1]).toMatchObject({
      id: 2,
      name: 'svslow',
      count: 1,
      minUs: 90,
      maxUs: 90,
      totalUs: 90,
      pct: 37.5,
    })
  })

  it('computes context rows sorted by CPU load', () => {
    const rows = computeContextRows([
      { id: 1, name: 'svfast', color: '#1', runUs: 150, switches: 4, prio: 12 },
      { id: 2, name: 'svslow', color: '#2', runUs: 50, switches: 1 },
    ])

    expect(rows).toEqual([
      {
        id: 1,
        name: 'svfast',
        color: '#1',
        type: 'Task',
        priority: 12,
        activations: 4,
        totalRunUs: 150,
        cpuLoad: 75,
      },
      {
        id: 2,
        name: 'svslow',
        color: '#2',
        type: 'Task',
        priority: undefined,
        activations: 1,
        totalRunUs: 50,
        cpuLoad: 25,
      },
    ])
  })

  it('keeps legacy CPU rows sorted by total runtime', () => {
    expect(computeTaskRows([
      { id: 1, name: 'a', color: '#1', runUs: 10, switches: 1 },
      { id: 2, name: 'b', color: '#2', runUs: 30, switches: 1 },
    ]).map(row => row.name)).toEqual(['b', 'a'])
  })

  it('builds compact event list rows for task and ISR events', () => {
    const rows = buildSystemViewEventRows([
      { kind: 'task_start_exec', t: 10, task_id: 0x20000001, task_name: 'svfast' },
      { kind: 'isr_enter', t: 20, isr_id: 15, isr_name: 'SysTick', cpu_delta_us: 11 },
      { kind: 'isr_to_scheduler', t: 25, cpu_delta_us: 5 },
      { kind: 'idle', t: 30, cpu_delta_us: 995.5 },
    ], {
      firstIndex: 42,
      formatTime: value => `${value} us`,
    })

    expect(rows).toEqual([
      {
        index: 42,
        time: '10 us',
        context: 'svfast',
        event: 'Task Start Exec',
        resource: 'Task',
        detail: '0x20000001',
        kind: 'task_start_exec',
      },
      {
        index: 43,
        time: '20 us',
        context: 'SysTick',
        event: 'ISR Enter',
        resource: 'ISR #15',
        detail: '',
        kind: 'isr_enter',
      },
      {
        index: 44,
        time: '25 us',
        context: '',
        event: 'ISR To Scheduler',
        resource: 'ISR',
        detail: '',
        kind: 'isr_to_scheduler',
      },
      {
        index: 45,
        time: '30 us',
        context: 'Idle',
        event: 'System Idle',
        resource: '',
        detail: 'idle 996 us',
        kind: 'idle',
      },
    ])
  })
})
