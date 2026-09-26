import { useState } from 'react'
import { Button, Input, Card, Space, Typography, Alert, Tag } from 'antd'
import { SendOutlined } from '@ant-design/icons'

import { http } from '../api/client'

const { TextArea } = Input
const { Title, Paragraph, Text } = Typography

interface AskResp {
  ok: boolean
  ok_reason?: string
  intent?: string
  route?: string
  confidence?: number
  degraded?: boolean
  answer?: string
  citations?: Array<Record<string, unknown>>
  web?: Record<string, unknown> | null
  verify?: Record<string, unknown> | null
  flags?: Record<string, unknown> | null
  notes?: string[]
  thread_id?: string
}

interface Turn {
  role: 'user' | 'assistant'
  content: string
  resp?: AskResp | null
}

const SAMPLE_QUESTIONS = [
  '贵州茅台2024年的营业总收入是多少',
  '贵州茅台2024年年报的审计机构是哪家',
]

/**
 * 批次 1 最小 ChatPage：
 *   POST /api/ask（非流式） → 渲染答案 + 拒答卡 + 引用计数。
 *
 * 批次 2 会把这里换成 SSE `/api/ask/stream`（fetch 手写），
 * 本批次跑通链路就行，引用卡/网络卡的细粒度渲染留后续。
 */
export default function ChatPage() {
  const [question, setQuestion] = useState('')
  const [turns, setTurns] = useState<Turn[]>([])
  const [loading, setLoading] = useState(false)
  const [threadId, setThreadId] = useState<string | null>(null)

  const runAsk = async (q: string) => {
    const trimmed = q.trim()
    if (!trimmed || loading) return
    setLoading(true)
    setQuestion('')
    setTurns((prev) => [...prev, { role: 'user', content: trimmed }])

    try {
      const data = await http.post<AskResp>('/api/ask', {
        question: trimmed,
        thread_id: threadId ?? undefined,
      })
      setTurns((prev) => [...prev, {
        role: 'assistant',
        content: data?.answer ?? '(服务未返回答案)',
        resp: data ?? null,
      }])
      if (data?.thread_id) setThreadId(data.thread_id)
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string }; status?: number } })
          ?.response?.data?.detail ||
        '网络错误或后端不可用'
      setTurns((prev) => [...prev, {
        role: 'assistant',
        content: '',
        resp: { ok: false, ok_reason: String(msg) } as AskResp,
      }])
    } finally {
      setLoading(false)
    }
  }

  const canAsk = question.trim().length > 0 && !loading

  return (
    <div style={{ maxWidth: 900, margin: '0 auto' }}>
      {/* 欢迎卡（空会话时显示） */}
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
        </Card>
      )}

      {/* 对话流 */}
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
          <Tag color={t.role === 'user' ? 'blue' : 'default'} style={{ alignSelf: 'flex-start' }}>
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
            {t.content}

            {/* 拒答 / 错误 */}
            {t.resp && !t.resp.ok && t.role === 'assistant' && (
              <Alert
                style={{ marginTop: 8 }}
                type="warning"
                showIcon
                message={t.resp.ok_reason || '拒答'}
              />
            )}

            {/* 引用计数 / 置信度 */}
            {t.resp?.ok && t.role === 'assistant' && (
              <div style={{ marginTop: 8, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                {t.resp.citations && (
                  <Tag color="blue">引用 {t.resp.citations.length} 条</Tag>
                )}
                {t.resp.confidence != null && (
                  <Tag>置信度 {(t.resp.confidence * 100).toFixed(0)}%</Tag>
                )}
                {t.resp.intent && <Tag color="geekblue">意图: {t.resp.intent}</Tag>}
                {t.resp.degraded && <Tag color="warning">降级</Tag>}
              </div>
            )}
          </div>
        </div>
      ))}

      {/* 输入区 */}
      <Card
        size="small"
        style={{ position: 'sticky', bottom: 0, marginTop: 16 }}
      >
        <Space.Compact style={{ width: '100%' }}>
          <TextArea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="例如：贵州茅台2024年年报的审计机构是哪家"
            autoSize={{ minRows: 2, maxRows: 4 }}
            onPressEnter={(e) => { if (!e.shiftKey) { e.preventDefault(); runAsk(question) } }}
          />
          <Button
            type="primary"
            icon={<SendOutlined />}
            loading={loading}
            disabled={!canAsk}
            onClick={() => runAsk(question)}
          >
            提问
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
