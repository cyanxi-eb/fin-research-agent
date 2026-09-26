/**
 * HITL（Human-In-The-Loop）相关 API：
 *   GET  /api/hitl/{thread_id}          → 读回挂起状态
 *   POST /api/hitl/{thread_id}/confirm  → approve / reject
 */
import { http } from './client'
import type { HitlResult } from './ask'

export interface HitlStatusResp {
  found: boolean
  pending?: boolean
  response?: Record<string, unknown>
  [k: string]: unknown
}

export interface HitlConfirmReq {
  decision: 'approve' | 'reject'
  note?: string
  reviewer?: string
}

export function hitlStatus(threadId: string) {
  return http.get<HitlStatusResp>(`/api/hitl/${encodeURIComponent(threadId)}`)
}

export function hitlConfirm(threadId: string, req: HitlConfirmReq) {
  return http.post<Record<string, unknown>>(`/api/hitl/${encodeURIComponent(threadId)}/confirm`, req)
}

export type { HitlResult }
