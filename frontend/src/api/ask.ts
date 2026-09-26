/**
 * SSE 流式客户端：fetch + ReadableStream getReader() 手动解析 text/event-stream。
 *
 * EventSource 不支持自定义 header（鉴权必须 Authorization: Bearer），
 * 所以这条链路独立于 axios client 走原生 fetch —— 但 header 组装逻辑对齐
 * src/api/client.ts（X-Request-Id / Authorization），改一处就行。
 *
 * 事件协议（后端 src/streaming.py 定义）：
 *   meta     → { intent, route, thread_id, question }
 *   token    → { text, final? }   逐段追加
 *   citations→ { citations, count }
 *   verify   → VerifyResult
 *   web      → WebResult | null
 *   hitl     → HitlResult         挂起时才有
 *   done     → { response: FinalResponse }  终态，结构 = 非流式 /api/ask 返回
 *
 * 用法：
 *   for await (const [name, payload] of streamAsk({ question: '...' })) {
 *     if (name === 'token') appendToken(payload.text)
 *     if (name === 'done') setFinal(payload.response)
 *   }
 */
import { useAuthStore } from '../store/authStore'

export type SseEvent =
  | ['meta', MetaEvent]
  | ['token', TokenEvent]
  | ['citations', CitationsEvent]
  | ['verify', Record<string, unknown>]
  | ['web', Record<string, unknown> | null]
  | ['hitl', HitlResult]
  | ['done', { response: FinalResponse }]
  | ['error', { message: string }]

export interface MetaEvent {
  intent: string
  route: Record<string, unknown>
  thread_id: string
  question?: string
}

export interface TokenEvent { text: string; final?: boolean }

export interface CitationsEvent {
  citations: Citation[]
  count: number
}

export interface Citation {
  company?: string
  year?: number | string
  page_no?: number
  section?: string
  part?: string
  parts_total?: string | number
  chunk_id?: string
  title?: string
  doc_no?: string
  article_no?: string
  article_label?: string
  chapter?: string
  status?: string
  effective_from?: string
  source_name?: string
  url?: string
  [k: string]: unknown
}

export interface HitlResult {
  pending: boolean
  triggers?: string[]
  reason?: string
  confidence?: number
  candidates?: string[]
  current?: string
  [k: string]: unknown
}

export interface FinalResponse {
  ok: boolean
  ok_reason?: string
  intent?: string
  route?: Record<string, unknown>
  confidence?: number
  degraded?: boolean
  answer?: string
  refused?: boolean
  refusal_reason?: string
  citations?: Citation[]
  verify?: Record<string, unknown> | null
  web?: Record<string, unknown> | null
  hitl?: HitlResult | null
  notes?: string[]
  disclaimer?: string
  retrieval?: Record<string, unknown>
  thread_id?: string
  [k: string]: unknown
}

export interface StreamAskParams {
  question: string
  code?: string | null
  year?: number | null
  topk?: number | null
  mode?: string | null
  use_llm?: boolean
  intent?: string | null
  thread_id?: string | null
  web_search?: boolean | null
}

function _generateRequestId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return (crypto as Crypto).randomUUID().replace(/-/g, '')
  }
  return Math.random().toString(36).slice(2) + Date.now().toString(36)
}

/**
 * 解析 SSE 响应体。一条 SSE 帧是多行 `event: xxx` + `data: json` + 空行分隔。
 * text/event-stream 编码统一 UTF-8，不需要手动 decode UTF-8 字节。
 */
async function* parseSse(reader: ReadableStreamDefaultReader<Uint8Array>): AsyncGenerator<SseEvent> {
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      // 按空行切帧（\n\n 或 \r\n\r\n）
      let idx: number
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        const evt = _parseFrame(frame)
        if (evt) yield evt
      }
    }
  } finally {
    reader.releaseLock()
  }

  // 最后一帧（如果没有末尾空行）
  const last = buffer.trim()
  if (last) {
    const evt = _parseFrame(last)
    if (evt) yield evt
  }
}

function _parseFrame(frame: string): SseEvent | null {
  let eventName = 'message'
  let dataStr = ''
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) {
      eventName = line.slice(6).trim()
    } else if (line.startsWith('data:')) {
      dataStr += (dataStr ? '\n' : '') + line.slice(5).trimStart()
    }
  }
  if (!dataStr) return null
  let payload: unknown
  try {
    payload = JSON.parse(dataStr)
  } catch {
    payload = dataStr
  }
  return [eventName, payload as Record<string, unknown>] as SseEvent
}

/**
 * 流式问答主入口。throw 出的错误带 `.kind = 'http' | 'network'` 供上层区分。
 */
export async function* streamAsk(params: StreamAskParams): AsyncGenerator<SseEvent> {
  const token = useAuthStore.getState().accessToken
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'Accept': 'text/event-stream',
    'X-Request-Id': _generateRequestId(),
  }
  if (token) headers['Authorization'] = `Bearer ${token}`

  let resp: Response
  try {
    resp = await fetch('/api/v1/ask/stream', {
      method: 'POST',
      headers,
      body: JSON.stringify(params),
    })
  } catch (err) {
    const e = err as Error
    e.message = `网络不可达：${e.message}`
    ;(e as Error & { kind?: string }).kind = 'network'
    throw e
  }

  if (!resp.ok) {
    const e = new Error(`HTTP ${resp.status}`)
    ;(e as Error & { kind?: string }).kind = 'http'
    ;(e as Error & { status?: number }).status = resp.status
    try {
      const body = await resp.json()
      ;(e as Error & { detail?: unknown }).detail = body?.detail ?? body
    } catch {
      // ignore
    }
    throw e
  }

  if (!resp.body) {
    const e = new Error('响应体为空')
    ;(e as Error & { kind?: string }).kind = 'network'
    throw e
  }

  yield* parseSse(resp.body.getReader())
}
