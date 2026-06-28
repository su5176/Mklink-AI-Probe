export interface SystemViewEventLike {
  kind?: string
  task_id?: number
  task_name?: string
  name?: string
  prio?: number
  t: number
  tk?: number
}

export interface SystemViewPendingStart {
  time: number
  tick?: number
}

export interface SystemViewTaskInterval {
  taskId: number
  start: number
  end: number
  startTk?: number
  endTk?: number
}

export interface SystemViewIntervalState {
  currentTaskId: number | null
  currentStart: SystemViewPendingStart | null
}

export interface SystemViewIntervalCallbacks {
  ensureTask: (id: number, name?: string) => void
  addRunTime: (id: number, duration: number) => void
  addSwitch: (id: number) => void
  applyTaskInfo?: (id: number, event: SystemViewEventLike) => void
}

function closeCurrentTask(
  state: SystemViewIntervalState,
  endTime: number,
  endTick: number | undefined,
  callbacks: SystemViewIntervalCallbacks,
): SystemViewTaskInterval | null {
  if (state.currentTaskId === null || !state.currentStart) return null
  if (endTime < state.currentStart.time) return null

  const taskId = state.currentTaskId
  const interval = {
    taskId,
    start: state.currentStart.time,
    end: endTime,
    startTk: state.currentStart.tick,
    endTk: endTick,
  }
  callbacks.addRunTime(taskId, endTime - state.currentStart.time)
  state.currentTaskId = null
  state.currentStart = null
  return interval
}

export function ingestSystemViewIntervals(
  events: readonly SystemViewEventLike[],
  state: SystemViewIntervalState,
  callbacks: SystemViewIntervalCallbacks,
): SystemViewTaskInterval[] {
  const intervals: SystemViewTaskInterval[] = []

  for (const event of events) {
    const t = event.t
    const tk = event.tk
    const kind = event.kind

    if (!Number.isFinite(t)) continue

    if (kind === 'task_start_exec' && typeof event.task_id === 'number') {
      const closed = closeCurrentTask(state, t, tk, callbacks)
      if (closed && closed.end > closed.start) intervals.push(closed)

      callbacks.ensureTask(event.task_id, event.task_name)
      callbacks.addSwitch(event.task_id)
      state.currentTaskId = event.task_id
      state.currentStart = { time: t, tick: tk }
    } else if (
      (kind === 'task_stop_exec' || kind === 'task_stop_ready') &&
      typeof event.task_id === 'number'
    ) {
      if (state.currentTaskId === event.task_id) {
        const closed = closeCurrentTask(state, t, tk, callbacks)
        if (closed && closed.end > closed.start) intervals.push(closed)
      }
    } else if (kind === 'idle') {
      const closed = closeCurrentTask(state, t, tk, callbacks)
      if (closed && closed.end > closed.start) intervals.push(closed)
    } else if (kind === 'task_info' && typeof event.task_id === 'number') {
      callbacks.ensureTask(event.task_id, event.task_name || event.name)
      callbacks.applyTaskInfo?.(event.task_id, event)
    }
  }

  return intervals
}
