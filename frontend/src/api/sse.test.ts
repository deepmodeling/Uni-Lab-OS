import { describe, expect, it, vi } from 'vitest'
import { SseParser, openEventStream, type SseMessage, type StreamStatus } from './sse'

describe('SseParser', () => {
  it('parses the uvicorn output of /api/v1/events across chunk boundaries', () => {
    const parser = new SseParser()
    const stream = [
      'retry: 3000\n: connected\n\n',
      'id: 41\n\n',
      'id: 42\nevent: workflow.task.changed\ndata: {"workflow_task_uuid":"t1"}\n\n',
      'event: runtime.logs.changed\ndata: {"source_ids":["host"],"sources_changed":false,"all_sources":false}\n\n',
    ].join('')
    const messages: SseMessage[] = []
    // 每 7 个字符一块，模拟任意切分
    for (let index = 0; index < stream.length; index += 7) messages.push(...parser.feed(stream.slice(index, index + 7)))
    expect(parser.retry).toBe(3000)
    expect(parser.lastEventId).toBe('42')
    expect(messages).toEqual([
      { id: '42', event: 'workflow.task.changed', data: '{"workflow_task_uuid":"t1"}' },
      // 瞬时通知不写 id，按规范沿用上一条的 last-event-id
      { id: '42', event: 'runtime.logs.changed', data: '{"source_ids":["host"],"sources_changed":false,"all_sources":false}' },
    ])
  })

  it('treats an id-only frame as a watermark without dispatching', () => {
    const parser = new SseParser()
    expect(parser.feed('id: 7\n\n')).toEqual([])
    expect(parser.lastEventId).toBe('7')
  })

  it('handles CRLF and CR line endings, multi-line data and default event type', () => {
    const parser = new SseParser('3')
    const messages = [
      ...parser.feed('data: line1\r\ndata: line2\r'),
      ...parser.feed('\n\r\n'),
      ...parser.feed('data:no-space\r\r\n'),
    ]
    expect(messages).toEqual([
      { id: '3', event: 'message', data: 'line1\nline2' },
      { id: '3', event: 'message', data: 'no-space' },
    ])
  })

  it('ignores ids containing NUL and non-numeric retry', () => {
    const parser = new SseParser('1')
    parser.feed('id: a\0b\nretry: soon\ndata: x\n\n')
    expect(parser.lastEventId).toBe('1')
    expect(parser.retry).toBeUndefined()
  })
})

function sseResponse(chunks: string[], { keepOpen = false } = {}) {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk)))
      if (!keepOpen) controller.close()
    },
  })
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

describe('openEventStream', () => {
  it('sends Last-Event-ID, reconnects after the server closes and resumes from the cursor', async () => {
    const seenHeaders: (string | undefined)[] = []
    let calls = 0
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      seenHeaders.push((init?.headers as Record<string, string>)['Last-Event-ID'])
      calls += 1
      if (calls === 1) return sseResponse(['retry: 5\n\n', 'id: 10\nevent: workflow.task.changed\ndata: {"a":1}\n\n'])
      return sseResponse(['id: 11\nevent: workflow.node_run.changed\ndata: {"b":2}\n\n'], { keepOpen: true })
    })
    const messages: SseMessage[] = []
    const statuses: StreamStatus[] = []
    const cursors: string[] = []
    const close = openEventStream({
      url: '/api/v1/events',
      lastEventId: '0',
      fetchImpl: fetchImpl as typeof fetch,
      onMessage: (message) => messages.push(message),
      onStatus: (status) => statuses.push(status),
      onCursor: (cursor) => cursors.push(cursor),
    })
    await vi.waitFor(() => expect(messages).toHaveLength(2))
    close()
    expect(seenHeaders.slice(0, 2)).toEqual(['0', '10'])
    expect(cursors.slice(0, 2)).toEqual(['10', '11'])
    expect(messages.map((message) => message.event)).toEqual(['workflow.task.changed', 'workflow.node_run.changed'])
    expect(statuses[0]).toBe('connecting')
    expect(statuses).toContain('open')
    expect(statuses).toContain('reconnecting')
  })

  it('omits Last-Event-ID on a fresh connect and reports non-SSE responses as errors', async () => {
    const headers: Record<string, string>[] = []
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      headers.push(init?.headers as Record<string, string>)
      return new Response(JSON.stringify({ code: 1000, error: { msg: 'bad cursor' } }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    })
    const details: string[] = []
    const close = openEventStream({
      url: '/api/v1/events',
      fetchImpl: fetchImpl as typeof fetch,
      retryMs: 5,
      onMessage: () => undefined,
      onStatus: (status, detail) => {
        if (status === 'error' && detail) details.push(detail)
      },
    })
    await vi.waitFor(() => expect(details.length).toBeGreaterThan(0))
    close()
    expect(headers[0]['Last-Event-ID']).toBeUndefined()
    expect(details[0]).toContain('bad cursor')
  })
})
