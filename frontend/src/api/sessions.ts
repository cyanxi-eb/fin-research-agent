/**
 * Phase 2 新增：会话后端 client。
 *
 * 批次 1 把 sessionStore 从纯 localStorage 改成后端 sync：
 *   - list      : GET  /api/sessions（应用启动 /api/sessions 路由守卫验活后调用）
 *   - touch     : POST /api/sessions/{thread_id}/touch（SSE meta 事件时顺便 upsert）
 *   - delete    : DELETE /api/sessions/{thread_id}（侧栏 🗑 按钮）
 *   - get       : GET  /api/sessions/{thread_id}（回看时调）
 */
import { http } from './client'

export interface SessionRow {
  thread_id: string
  username: string
  title?: string | null
  first_question?: string | null
  intent?: string | null
  pending?: number
  turns_count?: number
  last_at?: string
  created_at?: string
  [k: string]: unknown
}

export interface SessionTouchReq {
  title?: string | null
  first_question?: string | null
  intent?: string | null
  pending?: boolean | null
  turns_delta?: number
}

export function listSessions(params?: { limit?: number; q?: string }) {
  const qs = new URLSearchParams()
  if (params?.limit) qs.set('limit', String(params.limit))
  if (params?.q) qs.set('q', params.q)
  const s = qs.toString()
  return http.get<{ ok: boolean; sessions: SessionRow[]; count: number; limit?: number; q?: string }>(
    `/api/sessions${s ? '?' + s : ''}`,
  )
}

export function getSession(threadId: string) {
  return http.get<{ ok: boolean; session: SessionRow }>(`/api/sessions/${encodeURIComponent(threadId)}`)
}

export function touchSession(threadId: string, body: SessionTouchReq) {
  return http.post<{ ok: boolean; thread_id: string }>(
    `/api/sessions/${encodeURIComponent(threadId)}/touch`,
    body,
  )
}

export function deleteSession(threadId: string) {
  return http.delete<{ ok: boolean; thread_id: string }>(`/api/sessions/${encodeURIComponent(threadId)}`)
}
