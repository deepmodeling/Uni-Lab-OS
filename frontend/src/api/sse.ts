/**
 * `GET /api/v1/events` 的 SSE 读取：用 fetch 流而不是 EventSource，这样首连就能带
 * `Last-Event-ID`（填 0 回放保留历史），断线后按服务端 `retry:` 自动续传。
 *
 * 服务端语义（unilabos/server/api/runtime/events.py）：
 * - 不带游标：先发一个只有 `id:` 的水位帧，之后只推新通知；
 * - 持久工作流通知带 `id`（frontend_event 序号）；`runtime.logs.changed` 是瞬时通知，不带 `id`；
 * - 游标超前 / 过期时发 `stream.reset`（data.reason），`id` 为新水位，客户端应重读 REST。
 */

export interface SseMessage {
  /** 本条消息分发时的 last-event-id（规范：id 跨消息保持）。 */
  id?: string
  event: string
  data: string
}

/** 增量 SSE 解析器（WHATWG event-stream 规则的子集，足够覆盖 uvicorn 的输出）。 */
export class SseParser {
  lastEventId: string | undefined
  retry: number | undefined
  private buffer = ''
  private dataLines: string[] = []
  private eventType = ''

  constructor(lastEventId?: string) {
    this.lastEventId = lastEventId
  }

  feed(chunk: string): SseMessage[] {
    let text = this.buffer + chunk
    // 行尾可以是 \r\n、\n 或 \r；块尾的 \r 可能是 \r\n 的前半，留到下一块再判断
    const hold = text.endsWith('\r') ? '\r' : ''
    if (hold) text = text.slice(0, -1)
    const lines = text.split(/\r\n|\n|\r/)
    // 最后一段没有行尾，是不完整的一行
    this.buffer = (lines.pop() ?? '') + hold
    const messages: SseMessage[] = []
    for (const line of lines) {
      const message = this.line(line)
      if (message) messages.push(message)
    }
    return messages
  }

  private line(line: string): SseMessage | undefined {
    if (line === '') return this.dispatch()
    if (line.startsWith(':')) return undefined
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'data') this.dataLines.push(value)
    else if (field === 'event') this.eventType = value
    else if (field === 'id') {
      if (!value.includes('\0')) this.lastEventId = value
    } else if (field === 'retry') {
      if (/^\d+$/.test(value)) this.retry = Number(value)
    }
    return undefined
  }

  private dispatch(): SseMessage | undefined {
    const eventType = this.eventType || 'message'
    const hasData = this.dataLines.length > 0
    const data = this.dataLines.join('\n')
    this.dataLines = []
    this.eventType = ''
    if (!hasData) return undefined
    return { id: this.lastEventId, event: eventType, data }
  }
}

export type StreamStatus = 'connecting' | 'open' | 'reconnecting' | 'closed' | 'error'

export interface EventStreamOptions {
  url: string
  /** 首连游标；undefined = 从当前水位开始（不回放）。 */
  lastEventId?: string
  onMessage: (message: SseMessage) => void
  onStatus?: (status: StreamStatus, detail?: string) => void
  /** 游标推进（含只有 id 的水位帧）。 */
  onCursor?: (lastEventId: string) => void
  fetchImpl?: typeof fetch
  /** 服务端未给 retry 时的重连间隔。 */
  retryMs?: number
}

/** 打开并保持一个事件流；返回 close()。 */
export function openEventStream(options: EventStreamOptions): () => void {
  const controller = new AbortController()
  const doFetch: typeof fetch = options.fetchImpl ?? ((input, init) => globalThis.fetch(input, init))
  let lastEventId = options.lastEventId
  let retryMs = options.retryMs ?? 3000

  const sleep = (ms: number) =>
    new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, ms)
      controller.signal.addEventListener('abort', () => {
        clearTimeout(timer)
        resolve()
      })
    })

  async function connectOnce(): Promise<void> {
    const headers: Record<string, string> = { Accept: 'text/event-stream' }
    if (lastEventId !== undefined) headers['Last-Event-ID'] = lastEventId
    const response = await doFetch(options.url, { headers, signal: controller.signal, cache: 'no-store' })
    const contentType = response.headers.get('content-type') ?? ''
    if (!response.ok || !contentType.includes('text/event-stream') || !response.body) {
      const text = await response.text().catch(() => '')
      throw new Error(`事件流不可用：HTTP ${response.status} ${text.slice(0, 200)}`.trim())
    }
    options.onStatus?.('open')
    const parser = new SseParser(lastEventId)
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      const messages = parser.feed(decoder.decode(value, { stream: true }))
      if (parser.retry !== undefined) retryMs = parser.retry
      if (parser.lastEventId !== undefined && parser.lastEventId !== lastEventId) {
        lastEventId = parser.lastEventId
        options.onCursor?.(lastEventId)
      }
      messages.forEach(options.onMessage)
    }
  }

  void (async () => {
    options.onStatus?.('connecting')
    while (!controller.signal.aborted) {
      try {
        await connectOnce()
        if (controller.signal.aborted) break
        options.onStatus?.('reconnecting', '服务端关闭了连接')
      } catch (error) {
        if (controller.signal.aborted) break
        options.onStatus?.('error', error instanceof Error ? error.message : String(error))
      }
      await sleep(retryMs)
      if (!controller.signal.aborted) options.onStatus?.('reconnecting')
    }
    options.onStatus?.('closed')
  })()

  return () => controller.abort()
}
