/**
 * 对比 API：GET+POST /api/compare。
 *
 * 后端返回（compare.py _compare_response）：
 *   { ok, indicator, unit, rows: CompanyRow[], periods_consistent, chart: ChartPayload, note? }
 *
 * CompanyRow: { code, name, period, value, unit, source? }
 * ChartPayload: { x_axis: [period...], series: [{ name, period_label, values: number[] }] }
 */
import { http } from './client'

export interface CompanyRow {
  code: string
  name?: string
  period?: string
  value: number | string | null
  unit?: string
  source?: Record<string, unknown> | null
  [k: string]: unknown
}

export interface ChartSeries {
  name: string
  period_label?: string
  values: (number | null)[]
}

export interface ChartPayload {
  x_axis?: string[]
  series?: ChartSeries[]
  [k: string]: unknown
}

export interface CompareResp {
  ok: boolean
  indicator: string
  unit?: string
  rows: CompanyRow[]
  periods_consistent: boolean
  chart?: ChartPayload
  note?: string
  [k: string]: unknown
}

export function compareGet(indicator: string, codes: string[], period?: string) {
  const qs = new URLSearchParams({ indicator, codes: codes.join(',') })
  if (period) qs.set('period', period)
  return http.get<CompareResp>(`/api/compare?${qs.toString()}`)
}

export function comparePost(indicator: string, codes: string[], period?: string | null) {
  return http.post<CompareResp>('/api/compare', { indicator, codes, period: period ?? null })
}
