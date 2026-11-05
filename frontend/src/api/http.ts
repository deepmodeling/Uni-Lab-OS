/**
 * 微后端 HTTP 传输层：同一个 fetch 包装，按路径域选择两种响应约定。
 *
 * - 信封域（workflow / graphs / chemicals / registry）：成功 `{"code": 0, "data": ...}`；
 *   业务失败固定 HTTP 200 + `{"code": N, "error": {"msg": "..."}}`，业务码表见
 *   `unilabos/server/api/envelope.py`（0 / 1000 / 3002 / 3003 / 5001 / 1）。
 * - 其余域直出 DTO：失败用 HTTP 状态码（404 / 409 / 422 / 503），正文 `{"detail": ...}`。
 */

export const BUSINESS_CODES = {
  ok: 0,
  internal: 1,
  invalid: 1000,
  not_found: 3002,
  conflict: 3003,
  unavailable: 5001,
} as const

export type BusinessCategory = keyof typeof BUSINESS_CODES

const CATEGORY_BY_CODE = new Map<number, BusinessCategory>(
  (Object.entries(BUSINESS_CODES) as [BusinessCategory, number][]).map(([category, code]) => [code, category]),
)

/** 未登记的非零业务码按 internal 处理（与后端 business_code 的兜底一致）。 */
export function businessCategory(code: number): BusinessCategory {
  return CATEGORY_BY_CODE.get(code) ?? 'internal'
}

/** 后端 `install_envelope_validation` 登记的信封式路径前缀，加上 graphs / chemicals / registry。 */
export const ENVELOPE_PREFIXES = [
  '/api/v1/workflows',
  '/api/v1/workflow-tasks',
  '/api/v1/workflow-node-runs',
  '/api/v1/workflow-node-jobs',
  '/api/v1/workflow-manual-confirmations',
  '/api/v1/workflow-publications',
  '/api/v1/graphs',
  '/api/v1/chemicals',
  '/api/v1/registry',
] as const

export function usesEnvelope(path: string): boolean {
  const bare = path.split('?', 1)[0]
  return ENVELOPE_PREFIXES.some((prefix) => bare === prefix || bare.startsWith(`${prefix}/`))
}

export type ApiErrorKind = 'business' | 'http' | 'network' | 'invalid_response'

export class ApiError extends Error {
  readonly kind: ApiErrorKind
  readonly method: string
  readonly path: string
  /** HTTP 状态码；网络错误时缺省。 */
  readonly status?: number
  /** 信封业务码。 */
  readonly code?: number
  readonly category?: BusinessCategory
  readonly body?: unknown

  constructor(
    message: string,
    init: { kind: ApiErrorKind; method: string; path: string; status?: number; code?: number; body?: unknown },
  ) {
    super(message)
    this.name = 'ApiError'
    this.kind = init.kind
    this.method = init.method
    this.path = init.path
    this.status = init.status
    this.code = init.code
    this.category = init.code === undefined ? undefined : businessCategory(init.code)
    this.body = init.body
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * 从失败正文里取一句人能读的说明：FastAPI `{"detail": str | [{loc, msg}]}`、
 * 信封 `{"error": {"msg"}}`，其余回退到 `fallback`。
 */
export function errorMessage(body: unknown, fallback: string): string {
  if (typeof body === 'string' && body.trim()) return body.trim().slice(0, 500)
  if (!isRecord(body)) return fallback
  const { detail } = body
  if (typeof detail === 'string' && detail) return detail
  if (Array.isArray(detail) && detail.length) {
    return detail
      .slice(0, 5)
      .map((item) => {
        if (!isRecord(item)) return String(item)
        const location = Array.isArray(item.loc) ? item.loc.join('.') : ''
        return location ? `${location}: ${String(item.msg ?? '')}` : String(item.msg ?? '')
      })
      .join('; ')
  }
  if (isRecord(body.error) && typeof body.error.msg === 'string' && body.error.msg) return body.error.msg
  if (typeof body.message === 'string' && body.message) return body.message
  return fallback
}

/** 解开 `{code, data | error}` 信封：code=0 返回 data（可缺省），否则抛 business ApiError。 */
export function unwrapEnvelope<T>(body: unknown, context: { method: string; path: string; status?: number }): T {
  if (!isRecord(body) || typeof body.code !== 'number') {
    throw new ApiError('响应不是 {code, data} 信封', { kind: 'invalid_response', ...context, body })
  }
  if (body.code === BUSINESS_CODES.ok) return body.data as T
  const category = businessCategory(body.code)
  throw new ApiError(errorMessage(body, `业务失败（${category} / ${body.code}）`), {
    kind: 'business',
    ...context,
    code: body.code,
    body,
  })
}

export type QueryValue = string | number | boolean | null | undefined
export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'

export interface RequestOptions {
  method?: HttpMethod
  query?: Record<string, QueryValue>
  body?: unknown
  signal?: AbortSignal
  /** 额外视为成功的非 2xx 状态（例如 readiness 的 503 也带摘要正文）。 */
  acceptStatus?: number[]
}

export function buildUrl(baseUrl: string, path: string, query?: Record<string, QueryValue>): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null || value === '') continue
    params.set(key, String(value))
  }
  const search = params.toString()
  return `${baseUrl.replace(/\/+$/, '')}${path}${search ? `?${search}` : ''}`
}

/** 把 `{name}` 路径模板按参数填好（逐段 encodeURIComponent）。 */
export function fillPath(template: string, params: Record<string, string | number> = {}): string {
  return template.replace(/\{(\w+)\}/g, (_, name: string) => {
    const value = params[name]
    if (value === undefined || value === '') throw new Error(`路径参数 ${name} 缺失：${template}`)
    return encodeURIComponent(String(value))
  })
}

export interface Transport {
  readonly baseUrl: string
  request<T>(path: string, options?: RequestOptions): Promise<T>
  url(path: string, query?: Record<string, QueryValue>): string
}

export function createTransport({
  baseUrl = '',
  fetchImpl,
}: { baseUrl?: string; fetchImpl?: typeof fetch } = {}): Transport {
  const doFetch: typeof fetch = fetchImpl ?? ((input, init) => globalThis.fetch(input, init))

  async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const method = options.method ?? 'GET'
    const context = { method, path }
    const headers: Record<string, string> = { Accept: 'application/json' }
    if (options.body !== undefined) headers['Content-Type'] = 'application/json'
    let response: Response
    try {
      response = await doFetch(buildUrl(baseUrl, path, options.query), {
        method,
        headers,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        signal: options.signal,
      })
    } catch (error) {
      if ((error as { name?: string })?.name === 'AbortError') throw error
      throw new ApiError(`无法连接后端：${error instanceof Error ? error.message : String(error)}`, {
        kind: 'network',
        ...context,
      })
    }
    const status = response.status
    const text = await response.text()
    let body: unknown = undefined
    if (text) {
      try {
        body = JSON.parse(text)
      } catch {
        body = text
      }
    }
    const accepted = response.ok || (options.acceptStatus ?? []).includes(status)
    if (!accepted) {
      const code = isRecord(body) && typeof body.code === 'number' && body.code !== 0 ? body.code : undefined
      throw new ApiError(errorMessage(body, `HTTP ${status}`), { kind: 'http', ...context, status, code, body })
    }
    if (usesEnvelope(path)) return unwrapEnvelope<T>(body, { ...context, status })
    if (typeof body === 'string') {
      throw new ApiError('响应不是 JSON', { kind: 'invalid_response', ...context, status, body })
    }
    return body as T
  }

  return {
    baseUrl,
    request,
    url: (path, query) => buildUrl(baseUrl, path, query),
  }
}
