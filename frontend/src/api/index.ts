import { createApi } from './endpoints'
import { createTransport } from './http'
import { adoptTokenFromLocation, getControlToken } from './token'

export { ApiError, BUSINESS_CODES, businessCategory } from './http'
export type { Api } from './endpoints'

export { getControlToken, setControlToken } from './token'

if (typeof window !== 'undefined') adoptTokenFromLocation()

/** 页面同源请求 /api，由 vite dev / preview 代理到 UNILAB_BACKEND_URL；写操作带控制面 token。 */
export const api = createApi(createTransport({ baseUrl: '', getToken: getControlToken }))
