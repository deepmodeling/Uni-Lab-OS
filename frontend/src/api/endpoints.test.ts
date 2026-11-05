import { describe, expect, it, vi } from 'vitest'
import snapshot from '../../openapi/unilabos-openapi.json'
import { ENDPOINTS, createApi } from './endpoints'
import { createTransport } from './http'

const documentPaths = (snapshot as { paths: Record<string, Record<string, unknown>> }).paths

describe('ENDPOINTS', () => {
  it('only uses routes and methods present in the committed OpenAPI snapshot', () => {
    for (const [name, [method, path]] of Object.entries(ENDPOINTS)) {
      expect(documentPaths[path], `${name}: ${path}`).toBeDefined()
      expect(documentPaths[path][method.toLowerCase()], `${name}: ${method} ${path}`).toBeDefined()
    }
  })
})

describe('createApi', () => {
  it('fills path params, query and body for the task control routes', async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ code: 0, data: { uuid: 't1' } })))
    const api = createApi(createTransport({ fetchImpl: fetchImpl as typeof fetch }))
    await api.tasks.command('t1', { type: 'step', expected_revision: 4, idempotency_key: 'k1' })
    await api.tasks.editInput('t1', { expected_revision: 5, input: { volume: 2 } })
    await api.tasks.list({ status: 'running', page: 2, page_size: 20 })
    const calls = fetchImpl.mock.calls as unknown as [string, RequestInit][]
    expect(calls.map(([url, init]) => `${init.method} ${url}`)).toEqual([
      'POST /api/v1/workflow-tasks/t1/commands',
      'PATCH /api/v1/workflow-tasks/t1/input',
      'GET /api/v1/workflow-tasks?status=running&page=2&page_size=20',
    ])
    expect(JSON.parse(String(calls[1][1].body))).toEqual({ expected_revision: 5, input: { volume: 2 } })
  })

  it('treats readiness 503 as data and scheduler 503 as an error', async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) =>
      String(input).endsWith('/readiness')
        ? new Response(JSON.stringify({ status: 'not_ready', scheduler: 'local', execution: 'starting', reasons: ['x'] }), { status: 503 })
        : new Response(JSON.stringify({ detail: 'scheduler authority is remote' }), { status: 503 }),
    )
    const api = createApi(createTransport({ fetchImpl: fetchImpl as typeof fetch }))
    await expect(api.system.readiness()).resolves.toMatchObject({ status: 'not_ready', reasons: ['x'] })
    await expect(api.scheduler.resources()).rejects.toMatchObject({ status: 503 })
  })
})
