/**
 * 入库向导 API：5 步里暴露 3 个端点。
 *
 * 后端流程（src/api/routes/ingest.py）：
 *   POST /api/ingest/plan     → 自然语言 → IngestPlan 草稿（只 LLM 生成）
 *   POST /api/ingest/preview  → plan → 抓取预览（"只抓不写"，不落库）
 *   POST /api/ingest/commit   → plan + selected → 勾选项实际落库（require_admin）
 *
 * IngestPlan 是 wizard_mod.IngestPlan 的 model_dump() 形态，字段较多，这里用通用 Record。
 */
import { http } from './client'

export interface IngestPlan {
  companies?: Array<{ code: string; name: string; [k: string]: unknown }>
  indicators?: string[]
  ratios?: string[]
  periods?: number | null
  source?: string
  unresolved?: string[]
  [k: string]: unknown
}

export function ingestPlan(text: string) {
  // /api/ingest/plan 返回 { plan: IngestPlan, note: string | null }
  return http.post<{ plan: IngestPlan; note: string | null }>('/api/v1/ingest/plan', { text })
}

export function ingestPreview(plan: IngestPlan) {
  return http.post<Record<string, unknown>>('/api/v1/ingest/preview', plan)
}

export function ingestCommit(plan: IngestPlan, selected: string[][]) {
  return http.post<Record<string, unknown>>('/api/v1/ingest/commit', { plan, selected })
}
