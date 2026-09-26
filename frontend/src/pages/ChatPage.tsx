import { useState, useCallback, useRef, useEffect } from 'react'
import { useOutletContext } from 'react-router-dom'
import {
  Button, Input, Card, Space, Alert, Tag, Empty, Typography, App as AntdApp,
} from 'antd'
import { SendOutlined, SafetyCertificateOutlined } from '@ant-design/icons'

import { streamAsk } from '../api/ask'
import type { FinalResponse, MetaEvent, Citation, HitlResult } from '../api/ask'
import { hitlConfirm, hitlStatus } from '../api/hitl'
import { http } from '../api/client'
import { useAuthStore } from '../store/authStore'
import { useSessionStore, makeTitle } from '../store/sessionStore'

const { TextArea } = Input
const { Title, Text, Paragraph } = Typography

interface Turn {
  role: 'user' | 'assistant'
  content: string
  meta?: MetaEvent
  citations?: Citation[]
  verify?: Record<string, unknown> | null
  web?: Record<string, unknown> | null
  hitl?: HitlResult | null
  final?: FinalResponse | null
  error?: string
  streaming?: boolean
}

const SAMPLE_QUESTIONS = [
  '贵州茅台2024年的营业总收入是多少',
  '贵州茅台2024年年报的审计机构是哪家',
]

/**
 * 批次 2 ChatPage：流式驱动 + HITL 挂起面板 + 引用卡。
 *
 * 事件 → 本地态映射：
 *   meta       → turn.meta（thread_id / intent / route）
 *   token      → 逐段累加 turn.content + turn.streaming=true
 *   citations  → turn.citations
 *   verify     → turn.verify
 *   web        → turn.web
 *   hitl       → turn.hitl （挂起时面板显示）
 *   done       → turn.final + turn.streaming=false（终态）
 *
 * 批次 3 会把 sessions/回看接进来；本批次先把链路跑通。
 */
export default function ChatPage() {
  const { message } = AntdApp.useApp()
  const [question, setQuestion] = useState('')
  const [turns, setTurns] = useState<Turn[]>([])
  const [threadId, setThreadId] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const abortRef = useRef<AbortController | null>(null)

  // 批次 3：Layout 通过 Outlet context 传递的信号
  const ctx = useOutletContext<{
    replaySignal?: { threadId: string; nonce: number } | null
    newSignal?: number
  }>()
  const sessionUpsert = useSessionStore((s) => s.upsert)
  const sessionTouch = useSessionStore((s) => s.touch)

  // ---- 回看 / 新会话 ----
  useEffect(() => {
    if (ctx?.newSignal && ctx.newSignal > 0) {
      setTurns([])
      setThreadId(null)
      setLoading(false)
      abortRef.current?.abort()
    }
  }, [ctx?.newSignal])

  useEffect(() => {
    const rp = ctx?.replaySignal
    if (!rp || !rp.threadId) return
    // 调 /api/hitl/{id} 拿终态（agent_status 返回 response 字段 = FinalResponse）
    ;(async () => {
      try {
        const st = await hitlStatus(rp.threadId)
        if (!st.found) {
          message.warning(`会话不存在（thread_id=${rp.threadId}）`)
          return
        }
        setThreadId(rp.threadId)
        const resp = (st.response ?? {}) as FinalResponse
        // 重建 turns：只有终态 response（Checkpointer 只存状态快照，不存多轮 transcript）
        // 批次 3 能看到这一轮完整回答 + 引用 + HITL 状态；多轮历史后续扩展
        const reconstructed: Turn[] = []
        // 如果有 question 字段，先放 user turn
        const questionText =
          (resp as Record<string, unknown>)?.question as string | undefined
        if (questionText) reconstructed.push({ role: 'user', content: questionText })
        reconstructed.push({
          role: 'assistant',
          content: resp.answer ?? '',
          final: resp,
          citations: resp.citations,
          verify: resp.verify ?? null,
          web: resp.web ?? null,
          hitl: resp.hitl ?? null,
          streaming: false,
        })
        setTurns(reconstructed)
      } catch (err) {
        message.error(
          (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
            '回看失败',
        )
      }
    })()
  }, [ctx?.replaySignal?.nonce, ctx?.replaySignal?.threadId, message])

  const appendTurn = useCallback((updater: (prev: Turn[]) => Turn[]) => {
    setTurns((prev) => updater(prev))
  }, [])

  const runAsk = useCallback(
    async (q: string, opts?: { useStream?: boolean }) => {
      const trimmed = q.trim()
      if (!trimmed || loading) return

      const useStream = opts?.useStream ?? true
      setLoading(true)
      setQuestion('')

      // 新建 assistant turn（流式逐段追加 content）
      appendTurn((prev) => [
        ...prev,
        { role: 'user', content: trimmed },
        { role: 'assistant', content: '', streaming: true },
      ])
      const assistIdx = turns.length + 1 // 刚刚 push 的 assistant 位置（user+1）

      try {
        if (useStream) {
          abortRef.current = new AbortController()
          for await (const [name, payload] of streamAsk({
            question: trimmed,
            thread_id: threadId ?? undefined,
          })) {
            if (name === 'meta') {
              const m = payload as MetaEvent
              setThreadId(m.thread_id)
              // 批次 3：meta 里就 upsert session（标题取 question 前 30 字）
              sessionUpsert({
                threadId: m.thread_id,
                title: makeTitle(m.question ?? ''),
                firstQuestion: m.question,
                intent: m.intent,
                lastAt: Date.now(),
              })
              appendTurn((prev) => {
                const next = [...prev]
                const t = next[assistIdx]
                if (t) t.meta = m
                return next
              })
            } else if (name === 'token') {
              const text = (payload as { text: string }).text ?? ''
              appendTurn((prev) => {
                const next = [...prev]
                const t = next[assistIdx]
                if (t) t.content += text
                return next
              })
            } else if (name === 'citations') {
              appendTurn((prev) => {
                const next = [...prev]
                const t = next[assistIdx]
                if (t) t.citations = (payload as { citations: Citation[] }).citations
                return next
              })
            } else if (name === 'verify') {
              appendTurn((prev) => {
                const next = [...prev]
                const t = next[assistIdx]
                if (t) t.verify = payload as Record<string, unknown>
                return next
              })
            } else if (name === 'web') {
              appendTurn((prev) => {
                const next = [...prev]
                const t = next[assistIdx]
                if (t) t.web = payload as Record<string, unknown> | null
                return next
              })
            } else if (name === 'hitl') {
              const h = payload as HitlResult
              // 批次 3：挂起中 → touch pending=true
              const cur = (useSessionStore.getState().activeThreadId
                ?? threadId) as string | null
              if (cur) sessionTouch(cur, true)
              appendTurn((prev) => {
                const next = [...prev]
                const t = next[assistIdx]
                if (t) t.hitl = h
                return next
              })
            } else if (name === 'done') {
              const resp = (payload as { response: FinalResponse }).response
              setThreadId((tid) => resp?.thread_id ?? tid)
              // 批次 3：终态 → touch pending=false（挂起面板确认放行后后端会再给 done）
              const cur2 = resp?.thread_id ?? threadId
              if (cur2) sessionTouch(cur2, false)
              appendTurn((prev) => {
                const next = [...prev]
                const t = next[assistIdx]
                if (t) {
                  t.streaming = false
                  t.final = resp
                  // 终态 answer 覆盖流中的半成品
                  if (resp?.answer) t.content = resp.answer
                }
                return next
              })
            } else if (name === 'error') {
              appendTurn((prev) => {
                const next = [...prev]
                const t = next[assistIdx]
                if (t) {
                  t.streaming = false
                  t.error = (payload as { message: string }).message || '未知错误'
                }
                return next
              })
            }
          }
        } else {
          // 非流式降级（批次 1 的逻辑，保留作后备）
          const data = await http.post<FinalResponse>('/api/ask', {
            question: trimmed,
            thread_id: threadId ?? undefined,
          })
          setThreadId(data?.thread_id ?? threadId)
          appendTurn((prev) => {
            const next = [...prev]
            const t = next[assistIdx]
            if (t) {
              t.streaming = false
              t.content = data?.answer ?? ''
              t.final = data
              t.citations = data?.citations
              t.hitl = data?.hitl ?? null
            }
            return next
          })
        }
      } catch (err: unknown) {
        const msg =
          (err as { message?: string }).message ||
          '请求失败，请检查网络或后端状态'
        appendTurn((prev) => {
          const next = [...prev]
          const t = next[assistIdx]
          if (t) {
            t.streaming = false
            t.error = msg
          }
          return next
        })
        message.error(msg)
      } finally {
        setLoading(false)
        abortRef.current = null
      }
    },
    [loading, threadId, appendTurn, message, turns.length],
  )

  const handleHitlConfirm = async (
    threadIdParam: string,
    decision: 'approve' | 'reject',
    note?: string,
  ) => {
    try {
      await hitlConfirm(threadIdParam, {
        decision,
        note,
        reviewer: useAuthStore.getState().user?.username ?? undefined,
      })
      message.success(`已${decision === 'approve' ? '放行' : '驳回'}`)
      // 刷新当前 turn：重新调 /api/hitl/{id} 拿终态
      const st = await hitlStatus(threadIdParam)
      appendTurn((prev) => {
        const next = [...prev]
        // 找 thread_id 匹配的 turn
        const idx = next.findIndex((t) => t.meta?.thread_id === threadIdParam || t.final?.thread_id === threadIdParam)
        if (idx >= 0 && st.response) {
          next[idx] = {
            ...next[idx],
            hitl: { ...(next[idx].hitl ?? {}), pending: false } as HitlResult,
            final: st.response as FinalResponse,
            content: (st.response as FinalResponse)?.answer ?? next[idx].content,
            citations: (st.response as FinalResponse)?.citations ?? next[idx].citations,
          }
        }
        return next
      })
    } catch (err: unknown) {
      message.error(
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
          (err as Error).message,
      )
    }
  }

  const canAsk = question.trim().length > 0 && !loading

  return (
    <div style={{ maxWidth: 900, margin: '0 auto' }}>
      {turns.length === 0 && (
        <Card style={{ marginBottom: 16 }}>
          <Title level={4} style={{ marginTop: 0 }}>
            问年报，逐条给你出处
          </Title>
          <Paragraph type="secondary">
            本工作台只做年报原文的转述：每个数字都带
            <Text code> 公司 + 年份 + 页码 + 章节 </Text>
            ，可点回原文核对。检索不到证据时会明确拒答，而不是编一个听起来合理的答案。
          </Paragraph>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            {SAMPLE_QUESTIONS.map((q) => (
              <Button
                key={q}
                type="text"
                style={{
                  textAlign: 'left', padding: '6px 12px',
                  border: '1px solid #f0f0f0', borderRadius: 6,
                }}
                onClick={() => runAsk(q)}
              >
                试试：{q}
              </Button>
            ))}
          </Space>
          <Empty
            style={{ marginTop: 16 }}
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={<span style={{ color: '#bbb', fontSize: 12 }}>批次 2：流式问答 + HITL 挂起面板已接入</span>}
          />
        </Card>
      )}

      {turns.map((t, i) => (
        <div
          key={i}
          style={{
            display: 'flex',
            marginBottom: 16,
            gap: 12,
            flexDirection: t.role === 'user' ? 'row-reverse' : 'row',
          }}
        >
          <Tag
            color={t.role === 'user' ? 'blue' : 'default'}
            style={{ alignSelf: 'flex-start' }}
          >
            {t.role === 'user' ? '我' : '助手'}
          </Tag>
          <div
            style={{
              flex: 1,
              padding: '10px 14px',
              background: t.role === 'user' ? '#eaf1fb' : '#fff',
              border: '1px solid #f0f0f0',
              borderRadius: 8,
              whiteSpace: 'pre-wrap',
            }}
          >
            {/* 流式骨架屏（刚开始 token 还没到） */}
            {t.streaming && !t.content && (
              <span style={{ color: '#bbb' }}>思考中…</span>
            )}

            {/* 流式内容 + 光标 */}
            {t.content && (
              <>
                {t.content}
                {t.streaming && (
                  <span style={{ opacity: 0.5 }}>▌</span>
                )}
              </>
            )}

            {/* 错误 */}
            {t.error && (
              <Alert
                style={{ marginTop: 8 }}
                type="error"
                showIcon
                message={t.error}
              />
            )}

            {/* 拒答 */}
            {t.final && !t.final.ok && t.role === 'assistant' && !t.content && (
              <Alert
                style={{ marginTop: 8 }}
                type="warning"
                showIcon
                message={t.final.ok_reason || '拒答'}
              />
            )}

            {/* 终态 meta + 标签 */}
            {t.final && t.role === 'assistant' && (
              <div style={{ marginTop: 8, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                {t.citations && (
                  <Tag color="blue">引用 {t.citations.length} 条</Tag>
                )}
                {t.final.confidence != null && (
                  <Tag>置信度 {(t.final.confidence * 100).toFixed(0)}%</Tag>
                )}
                {t.final.intent && <Tag color="geekblue">意图: {t.final.intent}</Tag>}
                {t.final.degraded && <Tag color="warning">降级</Tag>}
              </div>
            )}

            {/* HITL 挂起面板 */}
            {t.hitl?.pending && t.role === 'assistant' && t.meta?.thread_id && (
              <HitlPanel
                hitl={t.hitl}
                threadId={t.meta.thread_id}
                onConfirm={handleHitlConfirm}
              />
            )}

            {/* 引用卡（批次 2 基础版，批次 3 做可折叠展开） */}
            {t.citations && t.citations.length > 0 && t.role === 'assistant' && (
              <CitationsList citations={t.citations} />
            )}
          </div>
        </div>
      ))}

      {/* 输入区 */}
      <Card size="small" style={{ position: 'sticky', bottom: 0, marginTop: 16 }}>
        <Space.Compact style={{ width: '100%' }}>
          <TextArea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="例如：贵州茅台2024年年报的审计机构是哪家"
            autoSize={{ minRows: 2, maxRows: 4 }}
            onPressEnter={(e) => {
              if (!e.shiftKey) { e.preventDefault(); runAsk(question) }
            }}
          />
          <Button
            type="primary"
            icon={<SendOutlined />}
            loading={loading}
            disabled={!canAsk}
            onClick={() => runAsk(question)}
          >
            {loading ? '流式中…' : '提问'}
          </Button>
        </Space.Compact>
        {threadId && (
          <div style={{ fontSize: 12, color: '#aaa', marginTop: 6 }}>
            会话键：<code>{threadId}</code>
          </div>
        )}
      </Card>
    </div>
  )
}

// ---- 子组件 ----

function HitlPanel({
  hitl,
  threadId,
  onConfirm,
}: {
  hitl: HitlResult
  threadId: string
  onConfirm: (tid: string, d: 'approve' | 'reject', note?: string) => Promise<void>
}) {
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const triggers = hitl.triggers || []
  const reason = hitl.reason || '需要人工确认'

  const handle = async (d: 'approve' | 'reject') => {
    setBusy(true)
    try {
      await onConfirm(threadId, d, note.trim() || undefined)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      style={{
        marginTop: 12,
        padding: 12,
        background: '#fffbe6',
        border: '1px solid #ffe58f',
        borderRadius: 6,
      }}
    >
      <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 6 }}>
        <SafetyCertificateOutlined style={{ color: '#faad14' }} />
        <b style={{ color: '#d48806', fontSize: 13 }}>挂起等人工确认</b>
        {hitl.confidence != null && (
          <Tag color="warning" style={{ marginLeft: 'auto' }}>
            置信度 {(hitl.confidence * 100).toFixed(0)}%
          </Tag>
        )}
      </div>
      <div style={{ fontSize: 13, color: '#874d00', marginBottom: 6 }}>{reason}</div>
      {triggers.length > 0 && (
        <div style={{ marginBottom: 8 }}>
          {triggers.map((tr) => (
            <Tag key={tr} color="warning" style={{ marginBottom: 4 }}>
              {tr}
            </Tag>
          ))}
        </div>
      )}
      {hitl.candidates && hitl.candidates.length > 0 && (
        <div style={{ marginBottom: 8 }}>
          <div style={{ fontSize: 12, color: '#874d00', marginBottom: 4 }}>候选输出：</div>
          {hitl.candidates.map((c, i) => (
            <div
              key={i}
              style={{
                background: '#fff',
                border: '1px solid #ffe58f',
                borderRadius: 4,
                padding: '4px 8px',
                fontSize: 12.5,
                marginBottom: 4,
                whiteSpace: 'pre-wrap',
              }}
            >
              {c}
            </div>
          ))}
        </div>
      )}
      <Input.TextArea
        size="small"
        rows={2}
        placeholder="备注（可选）"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        style={{ marginBottom: 8 }}
      />
      <Space size={8}>
        <Button
          type="primary"
          size="small"
          disabled={busy}
          onClick={() => handle('approve')}
        >
          放行
        </Button>
        <Button
          danger
          size="small"
          disabled={busy}
          onClick={() => handle('reject')}
        >
          驳回
        </Button>
        <span style={{ fontSize: 11, color: '#bbb', marginLeft: 8 }}>
          thread_id: {threadId}
        </span>
      </Space>
    </div>
  )
}

function CitationsList({ citations }: { citations: Citation[] }) {
  if (!citations.length) return null
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ fontSize: 12, color: '#888', fontWeight: 600, marginBottom: 6 }}>
        引用来源（{citations.length} 条）
      </div>
      {citations.map((c, i) => {
        const label =
          (c.company && c.year
            ? `${c.company} ${c.year}`
            : c.title
              ? c.title
              : c.doc_no
                ? c.doc_no
                : `#${i + 1}`) +
          (c.page_no != null ? ` · P${c.page_no}` : '') +
          (c.article_no ? ` · ${c.article_no}` : '')
        return (
          <div
            key={i}
            style={{
              fontSize: 12,
              padding: '4px 8px',
              marginBottom: 2,
              background: '#fafafa',
              border: '1px solid #f0f0f0',
              borderRadius: 4,
            }}
          >
            <b>#{i + 1}</b> {label}
            {c.section && <span style={{ color: '#999', marginLeft: 4 }}>· {c.section}</span>}
          </div>
        )
      })}
    </div>
  )
}

// 从 api/hitl.ts 复用类型：组件里调 hitlConfirm 时需要它
