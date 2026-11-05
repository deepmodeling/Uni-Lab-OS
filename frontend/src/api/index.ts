import { createApi } from './endpoints'
import { createTransport } from './http'

export { ApiError, BUSINESS_CODES, businessCategory } from './http'
export type { Api } from './endpoints'

/** 页面同源请求 /api，由 vite dev / preview 代理到 UNILAB_BACKEND_URL。 */
export const api = createApi(createTransport({ baseUrl: '' }))
