import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import App from './App'
import complexGraph from './test/fixtures/graph-complex-demo.json'
import schedulerSnapshot from './test/fixtures/scheduler-lock-demo.json'

const envelope = (data: unknown) => ({ code: 0, data })

/** 按路径返回真实后端录下的形状；未登记的路径 404。 */
function backend(url: string): [number, unknown] {
  const path = url.split('?', 1)[0]
  if (path === '/api/v1/health') return [200, { status: 'ok', scheduler: 'local', execution: 'ready' }]
  if (path === '/api/v1/readiness') return [503, { status: 'not_ready', scheduler: 'local', execution: 'restarting', reasons: ['Host 子进程未接入控制面（启动或重启中）'] }]
  if (path === '/api/v1/hostlink/peers') return [200, { role: 'host', peers: [], client: null }]
  if (path === '/api/v1/scheduler/resources') return [200, schedulerSnapshot]
  if (path === '/api/v1/materials/instances') return [200, []]
  if (path === '/api/v1/workflow-tasks') return [200, envelope({ items: [], total: 0, page: 1, page_size: 20 })]
  if (path === '/api/v1/workflows') return [200, envelope({ items: [complexGraph.workflow], total: 1, page: 1, page_size: 200 })]
  if (path === `/api/v1/workflows/${complexGraph.workflow.uuid}/graph`) return [200, envelope(complexGraph)]
  return [404, { detail: 'Not Found' }]
}

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.stubGlobal('__UNILAB_BACKEND_URL__', 'http://127.0.0.1:8002')
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const [status, body] = backend(String(input))
      return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
    }),
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('App', () => {
  it('overview shows a 503 readiness summary as data, not as an error', async () => {
    renderAt('/')
    expect((await screen.findAllByText('Host 子进程未接入控制面（启动或重启中）')).length).toBeGreaterThan(0)
    expect(screen.getAllByText('未就绪').length).toBeGreaterThan(0)
    expect((await screen.findAllByText('后端在线')).length).toBeGreaterThan(0)
  })

  it('scheduler page renders held locks and blockers from a real snapshot', async () => {
    renderAt('/scheduler')
    expect((await screen.findAllByText('lock_probe_a / occupy')).length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('等待中的请求')).toBeInTheDocument()
  })

  it('blocked run preflight shows the reason without creating a task', async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/run-preflight')) return new Response(JSON.stringify(envelope({
        workflow_uuid: complexGraph.workflow.uuid, workflow_revision: complexGraph.workflow.revision,
        status: 'temporarily_unavailable', can_run: false, checked_at: '', summary: {},
        checks: [{ type: 'inventory', status: 'blocked', code: 'inventory_shortage', message: '试剂缺少 5 mL', blocking: true, details: {} }],
      })), { status: 200, headers: { 'Content-Type': 'application/json' } })
      const [status, body] = backend(url)
      void options
      return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
    })
    vi.stubGlobal('fetch', fetcher)
    renderAt(`/workflows/${complexGraph.workflow.uuid}`)
    fireEvent.click(await screen.findByRole('tab', { name: '提交任务' }))
    fireEvent.click(screen.getByRole('button', { name: '检查并提交任务' }))
    expect(await screen.findByText('试剂缺少 5 mL')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('预检发现阻塞项，请处理后重试。')).toBeInTheDocument())
    expect(fetcher.mock.calls.some(([url, options]) => String(url).endsWith('/workflow-tasks') && options?.method === 'POST')).toBe(false)
  })

  it('workflow page draws the DAG of the selected workflow', async () => {
    renderAt(`/workflows/${complexGraph.workflow.uuid}`)
    expect(await screen.findByRole('group', { name: '区域：提纯直到达标' })).toBeInTheDocument()
    expect(screen.getByLabelText(/^执行依赖：停止升温 → 提纯$/)).toBeInTheDocument()
  })
})
