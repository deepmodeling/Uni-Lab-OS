/**
 * 控制面 token：后端的写操作（POST / PUT / PATCH / DELETE）要求请求头
 * `Authorization: Bearer <token>`（或 `X-UniLab-Token`）。在运行 unilab 的机器上执行
 * `unilab token` 查看；控制台把它存在本浏览器的 localStorage 里。
 *
 * 也可以用 `http://127.0.0.1:5174/?token=<token>` 打开控制台：读到后存下并立即从地址栏抹掉。
 */

export const TOKEN_STORAGE_KEY = 'unilab.console.controlToken'

function storage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage
  } catch {
    return null
  }
}

export function getControlToken(): string | null {
  try {
    const value = storage()?.getItem(TOKEN_STORAGE_KEY)?.trim()
    return value ? value : null
  } catch {
    return null
  }
}

export function setControlToken(value: string): void {
  const token = value.trim()
  try {
    if (token) storage()?.setItem(TOKEN_STORAGE_KEY, token)
    else storage()?.removeItem(TOKEN_STORAGE_KEY)
  } catch {
    // 无痕模式等拒绝写入：本次会话不带 token
  }
}

/** 地址栏 `?token=` 存下后从 URL 去掉（不留在历史记录里）；返回是否取到了 token。 */
export function adoptTokenFromLocation(location: Location = window.location, history: History = window.history): boolean {
  const params = new URLSearchParams(location.search)
  const token = params.get('token')?.trim()
  if (!token) return false
  setControlToken(token)
  params.delete('token')
  const search = params.toString()
  try {
    history.replaceState(history.state, '', `${location.pathname}${search ? `?${search}` : ''}${location.hash}`)
  } catch {
    // 某些嵌入环境不允许改写地址：token 已经存下
  }
  return true
}
