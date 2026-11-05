import { describe, expect, it, vi } from 'vitest'
import {
  ApiError,
  buildUrl,
  businessCategory,
  createTransport,
  errorMessage,
  fillPath,
  unwrapEnvelope,
  usesEnvelope,
} from './http'

const context = { method: 'GET', path: '/api/v1/workflows' }

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

describe('business codes', () => {
  it('maps the envelope.py table and falls back to internal', () => {
    expect(businessCategory(0)).toBe('ok')
    expect(businessCategory(1000)).toBe('invalid')
    expect(businessCategory(3002)).toBe('not_found')
    expect(businessCategory(3003)).toBe('conflict')
    expect(businessCategory(5001)).toBe('unavailable')
    expect(businessCategory(1)).toBe('internal')
    expect(businessCategory(4242)).toBe('internal')
  })
})

describe('unwrapEnvelope', () => {
  it('returns data on code 0, including an absent data field', () => {
    expect(unwrapEnvelope({ code: 0, data: { uuid: 'w1' } }, context)).toEqual({ uuid: 'w1' })
    expect(unwrapEnvelope({ code: 0 }, context)).toBeUndefined()
  })

  it('throws a business ApiError carrying error.msg and the category', () => {
    const run = () => unwrapEnvelope({ code: 3003, error: { msg: '只能发布工作流的当前修订' } }, context)
    expect(run).toThrow(ApiError)
    try {
      run()
    } catch (error) {
      const apiError = error as ApiError
      expect(apiError.kind).toBe('business')
      expect(apiError.code).toBe(3003)
      expect(apiError.category).toBe('conflict')
      expect(apiError.message).toBe('只能发布工作流的当前修订')
    }
  })

  it('rejects bodies that are not an envelope', () => {
    expect(() => unwrapEnvelope([1, 2], context)).toThrow(/信封/)
    expect(() => unwrapEnvelope({ data: 1 }, context)).toThrow(/信封/)
  })
})

describe('errorMessage', () => {
  it('reads FastAPI detail strings and validation lists', () => {
    expect(errorMessage({ detail: 'scheduler authority is remote' }, 'x')).toBe('scheduler authority is remote')
    expect(
      errorMessage({ detail: [{ loc: ['body', 'type'], msg: 'Input should be step' }, { loc: ['query'], msg: 'bad' }] }, 'x'),
    ).toBe('body.type: Input should be step; query: bad')
    expect(errorMessage({ code: 1000, error: { msg: '提交内容格式不正确' } }, 'x')).toBe('提交内容格式不正确')
    expect(errorMessage(null, 'fallback')).toBe('fallback')
  })
})

describe('paths', () => {
  it('knows which prefixes use the envelope', () => {
    expect(usesEnvelope('/api/v1/workflow-tasks')).toBe(true)
    expect(usesEnvelope('/api/v1/workflow-tasks/abc/node-runs')).toBe(true)
    expect(usesEnvelope('/api/v1/workflow-node-runs/abc')).toBe(true)
    expect(usesEnvelope('/api/v1/workflows?page=2')).toBe(true)
    expect(usesEnvelope('/api/v1/workflow-publications')).toBe(true)
    expect(usesEnvelope('/api/v1/materials/instances')).toBe(false)
    expect(usesEnvelope('/api/v1/scheduler/resources')).toBe(false)
    // 前缀要按段匹配，不能把 /workflowsx 当成 /workflows
    expect(usesEnvelope('/api/v1/workflowsx')).toBe(false)
  })

  it('builds query strings without empty values and fills path templates', () => {
    expect(buildUrl('', '/api/v1/workflow-tasks', { status: '', page: 2, workflow_uuid: undefined, x: null })).toBe(
      '/api/v1/workflow-tasks?page=2',
    )
    expect(buildUrl('http://h:1/', '/api/v1/health')).toBe('http://h:1/api/v1/health')
    expect(fillPath('/api/v1/workflow-tasks/{task_uuid}/commands', { task_uuid: 'a b' })).toBe(
      '/api/v1/workflow-tasks/a%20b/commands',
    )
    expect(() => fillPath('/api/v1/workflows/{workflow_uuid}', {})).toThrow(/workflow_uuid/)
  })
})

describe('createTransport', () => {
  it('unwraps envelope routes and returns DTO routes as-is', async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.startsWith('/api/v1/workflows')) return jsonResponse({ code: 0, data: { items: [], total: 0 } })
      return jsonResponse({ status: 'ok' })
    })
    const transport = createTransport({ fetchImpl: fetchImpl as typeof fetch })
    await expect(transport.request('/api/v1/workflows')).resolves.toEqual({ items: [], total: 0 })
    await expect(transport.request('/api/v1/health')).resolves.toEqual({ status: 'ok' })
  })

  it('sends JSON bodies with the method', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ code: 0, data: { uuid: 't' } }, 201))
    const transport = createTransport({ fetchImpl: fetchImpl as typeof fetch })
    await transport.request('/api/v1/workflow-tasks/t/commands', {
      method: 'POST',
      body: { type: 'pause', expected_revision: 3, idempotency_key: 'k' },
    })
    const [, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit]
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
    expect(JSON.parse(String(init.body))).toEqual({ type: 'pause', expected_revision: 3, idempotency_key: 'k' })
  })

  it('turns business failures on HTTP 200 into business errors', async () => {
    const transport = createTransport({
      fetchImpl: (async () => jsonResponse({ code: 3002, error: { msg: '资源不存在' } })) as typeof fetch,
    })
    await expect(transport.request('/api/v1/workflow-tasks/missing')).rejects.toMatchObject({
      kind: 'business',
      code: 3002,
      category: 'not_found',
      // 业务失败固定 HTTP 200
      status: 200,
    })
  })

  it('turns non-2xx DTO responses into http errors with detail', async () => {
    const transport = createTransport({
      fetchImpl: (async () => jsonResponse({ detail: 'scheduler authority is remote' }, 503)) as typeof fetch,
    })
    await expect(transport.request('/api/v1/scheduler/resources')).rejects.toMatchObject({
      kind: 'http',
      status: 503,
      message: 'scheduler authority is remote',
    })
  })

  it('accepts listed non-2xx statuses (readiness 503 carries the summary)', async () => {
    const summary = { status: 'not_ready', scheduler: 'local', execution: 'restarting', reasons: ['Host 子进程未接入控制面'] }
    const transport = createTransport({ fetchImpl: (async () => jsonResponse(summary, 503)) as typeof fetch })
    await expect(transport.request('/api/v1/readiness', { acceptStatus: [503] })).resolves.toEqual(summary)
  })

  it('reports network failures and non-JSON bodies', async () => {
    const offline = createTransport({
      fetchImpl: (async () => {
        throw new TypeError('Failed to fetch')
      }) as typeof fetch,
    })
    await expect(offline.request('/api/v1/health')).rejects.toMatchObject({ kind: 'network' })
    const html = createTransport({ fetchImpl: (async () => new Response('<html>', { status: 200 })) as typeof fetch })
    await expect(html.request('/api/v1/health')).rejects.toMatchObject({ kind: 'invalid_response' })
  })
})
