export interface SystemViewTaskRuntime {
  id: number
  name: string
  color: string
  runUs: number
  switches: number
  prio?: number
}

export interface SystemViewTaskRow extends SystemViewTaskRuntime {
  pct: number
}

export interface SystemViewRuntimeInterval {
  taskId: number
  start: number
  end: number
}

export interface SystemViewRuntimeRow extends SystemViewTaskRuntime {
  count: number
  minUs: number
  p25Us: number
  p50Us: number
  p75Us: number
  maxUs: number
  totalUs: number
  pct: number
}

export interface SystemViewContextRow {
  id: number
  name: string
  color: string
  type: string
  priority?: number
  activations: number
  totalRunUs: number
  cpuLoad: number
}

export interface SystemViewEventRow {
  index: number
  time: string
  context: string
  event: string
  resource: string
  detail: string
  kind: string
}

export interface SystemViewEventRowOptions {
  firstIndex?: number
  formatTime?: (value: number) => string
}

export function computeTaskRows(tasks: SystemViewTaskRuntime[]): SystemViewTaskRow[] {
  const totalRun = tasks.reduce((sum, task) => {
    const run = Number.isFinite(task.runUs) ? Math.max(task.runUs, 0) : 0
    return sum + run
  }, 0)

  return tasks
    .map(task => ({
      ...task,
      pct: totalRun > 0 ? Math.max(task.runUs, 0) / totalRun * 100 : 0,
    }))
    .sort((a, b) => b.pct - a.pct)
}

export function computeRuntimeRows(
  tasks: SystemViewTaskRuntime[],
  intervals: SystemViewRuntimeInterval[],
): SystemViewRuntimeRow[] {
  const durationsByTask = new Map<number, number[]>()

  for (const interval of intervals) {
    const duration = interval.end - interval.start
    if (!Number.isFinite(duration) || duration <= 0) continue
    const durations = durationsByTask.get(interval.taskId) || []
    durations.push(duration)
    durationsByTask.set(interval.taskId, durations)
  }

  const totalRun = [...durationsByTask.values()]
    .flat()
    .reduce((sum, duration) => sum + duration, 0)

  return tasks
    .map(task => {
      const durations = (durationsByTask.get(task.id) || []).sort((a, b) => a - b)
      const totalUs = durations.reduce((sum, duration) => sum + duration, 0)
      return {
        ...task,
        count: durations.length,
        minUs: durations[0] || 0,
        p25Us: quantile(durations, 0.25),
        p50Us: quantile(durations, 0.5),
        p75Us: quantile(durations, 0.75),
        maxUs: durations[durations.length - 1] || 0,
        totalUs,
        pct: totalRun > 0 ? totalUs / totalRun * 100 : 0,
      }
    })
    .filter(row => row.count > 0)
    .sort((a, b) => b.totalUs - a.totalUs)
}

export function computeContextRows(tasks: SystemViewTaskRuntime[]): SystemViewContextRow[] {
  const totalRun = tasks.reduce((sum, task) => sum + Math.max(task.runUs, 0), 0)

  return tasks
    .map(task => ({
      id: task.id,
      name: task.name,
      color: task.color,
      type: 'Task',
      priority: task.prio,
      activations: task.switches,
      totalRunUs: Math.max(task.runUs, 0),
      cpuLoad: totalRun > 0 ? Math.max(task.runUs, 0) / totalRun * 100 : 0,
    }))
    .sort((a, b) => b.cpuLoad - a.cpuLoad)
}

export function buildSystemViewEventRows(
  events: any[],
  options: SystemViewEventRowOptions = {},
): SystemViewEventRow[] {
  const firstIndex = options.firstIndex ?? 1
  const formatTime = options.formatTime || ((value: number) => String(value))

  return events.map((event, offset) => {
    const kind = String(event.kind || 'event')
    const taskId = typeof event.task_id === 'number' ? hexId(event.task_id) : ''
    const isrName = event.isr_name || (event.isr_id !== undefined ? `ISR #${event.isr_id}` : '')
    const taskName = event.task_name || taskId
    const context = taskName || isrName || (kind === 'idle' ? 'Idle' : '')

    return {
      index: firstIndex + offset,
      time: typeof event.t === 'number' ? formatTime(event.t) : '',
      context,
      event: labelEvent(kind),
      resource: resourceForEvent(event, kind),
      detail: detailForEvent(event, kind, taskId),
      kind,
    }
  })
}

function quantile(sortedValues: number[], q: number): number {
  if (!sortedValues.length) return 0
  if (sortedValues.length === 1) return sortedValues[0]
  const pos = (sortedValues.length - 1) * q
  const base = Math.floor(pos)
  const rest = pos - base
  const next = sortedValues[base + 1]
  return next === undefined ? sortedValues[base] : sortedValues[base] + rest * (next - sortedValues[base])
}

function labelEvent(kind: string): string {
  if (kind === 'idle') return 'System Idle'
  if (kind.startsWith('isr_')) return 'ISR ' + titleWords(kind.replace(/^isr_/, ''))
  return kind
    .split('_')
    .filter(Boolean)
    .map(titleWord)
    .join(' ')
}

function resourceForEvent(event: any, kind: string): string {
  if (kind.startsWith('task_')) return 'Task'
  if (kind.startsWith('isr_')) return event.isr_id !== undefined ? `ISR #${event.isr_id}` : 'ISR'
  if (kind.startsWith('timer_')) return 'Timer'
  return ''
}

function detailForEvent(event: any, kind: string, taskId: string): string {
  const parts: string[] = []
  if (taskId && kind.startsWith('task_')) parts.push(taskId)
  if (kind === 'idle' && event.cpu_delta_us !== undefined) parts.push(`idle ${Math.round(event.cpu_delta_us).toLocaleString()} us`)
  if (event.cause !== undefined) parts.push(`cause=${event.cause}`)
  return parts.join(' ')
}

function hexId(id: number): string {
  return '0x' + (id >>> 0).toString(16).toUpperCase()
}

function titleWords(value: string): string {
  return value.split('_').filter(Boolean).map(titleWord).join(' ')
}

function titleWord(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1)
}
