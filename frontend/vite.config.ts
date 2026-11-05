import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

/**
 * 调试控制台只在本机 dev / preview 服务器下运行：页面一律请求同源 `/api`，
 * 由这里的代理转发到微后端。后端地址按优先级取
 * `UNILAB_BACKEND_URL`（环境变量或 frontend/.env.local）→ 默认 `http://127.0.0.1:8002`。
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const backend = (env.UNILAB_BACKEND_URL || 'http://127.0.0.1:8002').trim().replace(/\/+$/, '')
  const proxy = {
    // REST 与 SSE（/api/v1/events）都在 /api 下；SSE 由 http-proxy 原样流式转发。
    '/api': { target: backend, changeOrigin: true },
  }

  return {
    plugins: [react()],
    define: {
      __UNILAB_BACKEND_URL__: JSON.stringify(backend),
    },
    server: {
      host: '127.0.0.1',
      port: 5174,
      proxy,
    },
    preview: {
      host: '127.0.0.1',
      port: 5174,
      proxy,
    },
    test: {
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      include: ['src/**/*.test.{ts,tsx}'],
    },
  }
})
