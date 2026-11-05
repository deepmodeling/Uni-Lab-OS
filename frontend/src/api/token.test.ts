import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createTransport } from './http'
import { TOKEN_STORAGE_KEY, adoptTokenFromLocation, getControlToken, setControlToken } from './token'

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

describe('control token', () => {
  beforeEach(() => window.localStorage.clear())

  it('is stored trimmed and cleared by an empty value', () => {
    setControlToken('  abc  ')
    expect(window.localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('abc')
    expect(getControlToken()).toBe('abc')
    setControlToken('')
    expect(getControlToken()).toBeNull()
  })

  it('is adopted from ?token= and removed from the address bar', () => {
    const replaceState = vi.fn()
    const adopted = adoptTokenFromLocation(
      { search: '?token=t-1&x=1', pathname: '/tasks', hash: '#h' } as Location,
      { state: null, replaceState } as unknown as History,
    )
    expect(adopted).toBe(true)
    expect(getControlToken()).toBe('t-1')
    expect(replaceState).toHaveBeenCalledWith(null, '', '/tasks?x=1#h')
  })

  it('adds a Bearer header only when a token is set, read per request', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ ok: true }))
    let token: string | null = null
    const transport = createTransport({ fetchImpl, getToken: () => token })
    await transport.request('/api/v1/health')
    token = 'secret'
    await transport.request('/api/v1/things', { method: 'POST', body: {} })
    const headers = fetchImpl.mock.calls.map((call) => (call as unknown as [string, RequestInit])[1].headers as Record<string, string>)
    expect(headers[0].Authorization).toBeUndefined()
    expect(headers[1].Authorization).toBe('Bearer secret')
  })
})
