/** 展示用的小工具：短 id、时间、时长、JSON 预览、幂等键。 */

export function shortId(value: string | null | undefined, length = 8): string {
  if (!value) return '—'
  return value.length > length ? value.slice(0, length) : value
}

export function formatTime(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  const date = typeof value === 'number' ? new Date(value) : new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleString('zh-CN', { hour12: false })
}

/** 两个 ISO 时间之间的时长；缺结束时间时用 now（进行中）。 */
export function formatDuration(start?: string | null, end?: string | null, now: number = Date.now()): string {
  if (!start) return '—'
  const begin = new Date(start).getTime()
  const finish = end ? new Date(end).getTime() : now
  if (Number.isNaN(begin) || Number.isNaN(finish)) return '—'
  const ms = Math.max(0, finish - begin)
  if (ms < 1000) return `${ms} ms`
  const seconds = ms / 1000
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} min ${Math.round(seconds % 60)} s`
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`
}

/** 秒级 unix 时间戳距今多久。 */
export function secondsAgo(epochSeconds: number | null | undefined, now: number = Date.now()): string {
  if (!epochSeconds) return '—'
  const delta = Math.max(0, now / 1000 - epochSeconds)
  if (delta < 60) return `${Math.round(delta)} s 前`
  if (delta < 3600) return `${Math.round(delta / 60)} min 前`
  return `${Math.round(delta / 3600)} h 前`
}

/** 单行 JSON 预览（截断）。 */
export function jsonPreview(value: unknown, max = 120): string {
  if (value === undefined) return ''
  let text: string
  try {
    text = typeof value === 'string' ? value : JSON.stringify(value)
  } catch {
    text = String(value)
  }
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

export function isEmptyJson(value: unknown): boolean {
  if (value === null || value === undefined) return true
  if (Array.isArray(value)) return value.length === 0
  if (typeof value === 'object') return Object.keys(value as object).length === 0
  return false
}

/** 每次点击一个新键；同一次点击的重试复用同一个键由调用方保证。 */
export function newIdempotencyKey(prefix = 'console'): string {
  const random = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
  return `${prefix}-${random}`
}
