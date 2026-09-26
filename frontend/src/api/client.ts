/**
 * 统一请求层：axios 实例 + 拦截器链。
 *
 * 职责（一条链路全收）：
 *   1) 请求自动带 X-Request-Id（透传 / 自动生成，对接后端 RequestId middleware）
 *   2) 请求自动带 Authorization: Bearer（从 authStore 读）
 *   3) 401 时自动用 refresh_token 换新 access，然后重放原请求（并发只刷一次）
 *   4) 刷新彻底失败 → 清 authStore → 路由跳 /login
 *
 * SSE 流式（/api/ask/stream）不走这里 —— EventSource 不支持自定义 header，
 * 但 fetch 可以，所以 src/api/ask.ts 里独立一个 streamAsk() 用 fetch + getReader()。
 */
import axios from 'axios'
import type { AxiosInstance, AxiosRequestConfig, InternalAxiosRequestConfig } from 'axios'

import { useAuthStore } from '../store/authStore'

// 一次刷新只发一个请求：并发 401 共用同一个 promise，避免刷新风暴。
let _refreshing: Promise<string | null> | null = null

function generateRequestId(): string {
  // uuid.v4 的轻量替代：crypto.randomUUID 有则用，没有就自己拼
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return (crypto as Crypto).randomUUID().replace(/-/g, '')
  }
  return (
    Math.random().toString(36).slice(2, 10) +
    Date.now().toString(36) +
    Math.random().toString(36).slice(2, 6)
  )
}

export const client: AxiosInstance = axios.create({
  baseURL: '', // Vite dev proxy 到 /api；生产同域直接打
  timeout: 30_000,
  headers: { Accept: 'application/json' },
})

// --- 请求拦截 ---
client.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    // X-Request-Id：优先透传，否则生成
    const existing = (config.headers as Record<string, string>)['X-Request-Id']
    config.headers.set('X-Request-Id', existing || generateRequestId())

    // Authorization：有 token 就带
    const token = useAuthStore.getState().accessToken
    if (token) {
      config.headers.set('Authorization', `Bearer ${token}`)
    }

    return config
  },
  (err) => Promise.reject(err),
)

// --- 响应拦截 ---

/** 后端 envelope 格式检测。
 *  开启时后端返回 {code, msg, data, trace_id}；关闭时返回原始 dict。
 *  这里统一解壳，让前端业务代码永远拿到"原始 data"，
 *  不关心后端 envelope 开关状态。
 */
function _isEnvelope(body: unknown): body is { code: number; msg: string; data: unknown; trace_id: string } {
  return (
    !!body &&
    typeof body === 'object' &&
    typeof (body as Record<string, unknown>).code === 'number' &&
    typeof (body as Record<string, unknown>).msg === 'string' &&
    'data' in (body as Record<string, unknown>) &&
    'trace_id' in (body as Record<string, unknown>)
  )
}

client.interceptors.response.use(
  // 成功响应：自动解 envelope → 把 response.data 换成解壳后的原始 data
  (resp) => {
    if (_isEnvelope(resp.data)) {
      resp.data = resp.data.data
    }
    return resp
  },
  async (err) => {
    const original = err.config as AxiosRequestConfig & { _tried?: boolean }
    const status = err.response?.status

    // 401：刷新一次 → 重放原请求（已刷过就不再递归，直接跳登录）
    if (status === 401 && !original._tried) {
      const state = useAuthStore.getState()
      const refresh = state.refreshToken
      if (refresh) {
        if (!_refreshing) {
          _refreshing = (async () => {
            try {
              // 用 client.post：享受 envelope unwrap + X-Request-Id header
              const r = await client.post('/api/v1/auth/refresh', { refresh_token: refresh })
              // r.data 已被拦截器解壳成原始响应
              if (r.data?.access_token) {
                useAuthStore.getState().setAuth(
                  r.data.access_token,
                  r.data.refresh_token ?? refresh,
                  r.data.user,
                )
                return r.data.access_token as string
              }
              return null
            } catch {
              return null
            } finally {
              _refreshing = null
            }
          })()
        }
        const newToken = await _refreshing
        if (newToken) {
          original._tried = true
          const retry = { ...original }
          if (retry.headers) {
            ;(retry.headers as Record<string, string>)['Authorization'] = `Bearer ${newToken}`
          }
          return client.request(retry)
        }
      }
      // 刷新彻底失败 → 清 auth + 跳登录
      useAuthStore.getState().logout()
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login'
      }
    }

    return Promise.reject(err)
  },
)

// 便捷封装：业务端点都用 client.get/post/delete
export const http = {
  get: <T = unknown>(url: string, cfg?: AxiosRequestConfig) =>
    client.get<T>(url, cfg).then((r) => r.data),
  post: <T = unknown>(url: string, data?: unknown, cfg?: AxiosRequestConfig) =>
    client.post<T>(url, data, cfg).then((r) => r.data),
  delete: <T = unknown>(url: string, cfg?: AxiosRequestConfig) =>
    client.delete<T>(url, cfg).then((r) => r.data),
}
