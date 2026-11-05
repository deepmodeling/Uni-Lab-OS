/** 事件流查看器的缓冲、过滤与主体提取。 */
import type { SseMessage } from '../api/sse'

export const LOG_NOTICE_EVENT = 'runtime.logs.changed'
export const STREAM_RESET_EVENT = 'stream.reset'

export interface EventEntry {
  /** 本地递增序号（瞬时通知没有服务端 id，React key 用它）。 */
  seq: number
  /** 服务端持久序号；瞬时通知为 undefined。 */
  id?: string
  event: string
  receivedAt: number
  data: unknown
  raw: string
  /** 关联的任务 / 工作流等 uuid。 */
  subject?: { kind: 'task' | 'workflow' | 'job' | 'other'; uuid: string }
}

function parseData(raw: string): unknown {
  try {
    return JSON.parse(raw)
  } catch {
    return raw
  }
}

/** 从通知正文里找主体：通知只表示「某聚合变了」，正文要回 REST 读。 */
export function eventSubject(data: unknown): EventEntry['subject'] {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return undefined
  const record = data as Record<string, unknown>
  const pick = (key: string) => (typeof record[key] === 'string' && record[key] ? (record[key] as string) : undefined)
  const task = pick('workflow_task_uuid') ?? pick('task_uuid')
  if (task) return { kind: 'task', uuid: task }
  const job = pick('workflow_node_job_uuid') ?? pick('job_uuid')
  if (job) return { kind: 'job', uuid: job }
  const workflow = pick('workflow_uuid')
  if (workflow) return { kind: 'workflow', uuid: workflow }
  const uuid = pick('uuid')
  return uuid ? { kind: 'other', uuid } : undefined
}

export function toEntry(message: SseMessage, seq: number, receivedAt: number): EventEntry {
  const data = parseData(message.data)
  // 瞬时日志通知不写 id；解析器会沿用上一条的 last-event-id，这里不把它当成本条的序号
  const persistent = message.event !== LOG_NOTICE_EVENT
  return {
    seq,
    id: persistent ? message.id : undefined,
    event: message.event,
    receivedAt,
    data,
    raw: message.data,
    subject: eventSubject(data),
  }
}

/** 新的在前，最多保留 `limit` 条。 */
export function prependBounded(buffer: EventEntry[], incoming: EventEntry[], limit: number): EventEntry[] {
  if (!incoming.length) return buffer
  const next = [...[...incoming].reverse(), ...buffer]
  return next.length > limit ? next.slice(0, limit) : next
}

export interface EventFilter {
  /** 空 = 全部类型。 */
  types: Set<string>
  /** 在事件类型、主体 uuid 与正文里做不区分大小写的子串匹配。 */
  text: string
  hideLogNotices: boolean
}

export function matchesFilter(entry: EventEntry, filter: EventFilter): boolean {
  if (filter.hideLogNotices && entry.event === LOG_NOTICE_EVENT) return false
  if (filter.types.size && !filter.types.has(entry.event)) return false
  const needle = filter.text.trim().toLowerCase()
  if (!needle) return true
  return [entry.event, entry.id ?? '', entry.subject?.uuid ?? '', entry.raw].some((value) =>
    value.toLowerCase().includes(needle),
  )
}

/** 各事件类型出现次数（过滤器下拉用）。 */
export function countByType(entries: EventEntry[]): [string, number][] {
  const counts = new Map<string, number>()
  for (const entry of entries) counts.set(entry.event, (counts.get(entry.event) ?? 0) + 1)
  return [...counts.entries()].sort((left, right) => left[0].localeCompare(right[0]))
}
