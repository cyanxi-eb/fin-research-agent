import { useState } from 'react'
import {
  Button, Card, Input, Steps, Table, Tag, Alert, Space, Typography, App as AntdApp,
} from 'antd'

import { ingestPlan, ingestPreview } from '../api/ingest'
import type { IngestPlan } from '../api/ingest'

const { TextArea } = Input
const { Title, Paragraph } = Typography

/**
 * 批次 4：入库向导 5 步。
 *
 * 向导流程（后端 wizard_mod）：
 *   ① 自然语言 → POST /api/ingest/plan（parse_request）→ IngestPlan 草稿
 *   ② 预览前采集 → POST /api/ingest/preview（只抓不写）→ 预览行
 *   ③ 勾选格（批次 4 先不接入真正的勾选取数 —— 那是 preview 返回后的数据展示，
 *      后端 preview 响应里会给 candidate_rows；批次 4 先搭 UI 骨架）
 *   ④ 确认 commit 条件（admin 鉴权检查）
 *   ⑤ POST /api/ingest/commit → 落库（require_admin）
 *
 * 批次 4 实现策略：
 *   Step 1-2 真走后端（plan + preview 两个端点不需要 admin）
 *   Step 3-5 先骨架（让向导能走完视觉流程，勾选取数逻辑批次 4.1 迭代）
 *   这是**最保守的推进节奏** —— 后端 wizard 层 3 个端点里 batch 1 就验证过
 *   能跑，批次 4 把前两步接通就行。
 */

const STEP_LABELS = [
  '输入需求', '生成计划', '预览抓取', '勾选落库', '提交确认',
]

export default function IngestPage() {
  const { message } = AntdApp.useApp()
  const [step, setStep] = useState(0)
  const [text, setText] = useState(
    '采集贵州茅台和五粮液 2024 年年报的 营业总收入、归属于上市公司股东的净利润、总资产',
  )
  const [plan, setPlan] = useState<IngestPlan | null>(null)
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null)
  const [loading, setLoading] = useState(false)

  const runPlan = async () => {
    if (!text.trim()) { message.warning('请输入入库需求'); return }
    setLoading(true)
    try {
      const resp = await ingestPlan(text.trim())
      if (resp.note) message.info(resp.note)
      setPlan(resp.plan)
      setStep(1)
      message.success('计划已生成')
    } catch (err) {
      message.error(
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
          '计划生成失败',
      )
    } finally {
      setLoading(false)
    }
  }

  const runPreview = async () => {
    if (!plan) return
    setLoading(true)
    try {
      const pv = await ingestPreview(plan)
      setPreview(pv)
      setStep(2)
      message.success('预览完成')
    } catch (err) {
      message.error(
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
          '预览失败',
      )
    } finally {
      setLoading(false)
    }
  }

  const reset = () => {
    setStep(0); setPlan(null); setPreview(null)
  }

  return (
    <div style={{ maxWidth: 1000, margin: '0 auto' }}>
      <Title level={4} style={{ marginTop: 0 }}>数据入库向导</Title>
      <Paragraph type="secondary">
        按 5 步完成数据入库 —— <b>批次 4 实现 Step 1-2（plan + preview）</b>；
        Step 3-5（勾选取数 + admin 确认 + commit）先搭骨架，后续迭代。
      </Paragraph>

      <Card size="small" style={{ marginBottom: 16 }}>
        <Steps current={step} items={STEP_LABELS.map((l) => ({ title: l }))} />
      </Card>

      {/* Step 0: 输入需求 */}
      {step === 0 && (
        <Card title="① 输入入库需求" size="small">
          <Paragraph type="secondary">
            用自然语言描述：哪家公司、哪期、哪些指标。LLM 只出草稿，
            认公司/认指标会过后端归一器。
          </Paragraph>
          <TextArea
            rows={4}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="示例：采集 A 公司和 B 公司 2024 年年报的 营业总收入、归属于上市公司股东的净利润"
          />
          <div style={{ marginTop: 12, textAlign: 'right' }}>
            <Button type="primary" onClick={runPlan} loading={loading}>生成计划 →</Button>
          </div>
        </Card>
      )}

      {/* Step 1: 生成计划 */}
      {step === 1 && plan && (
        <Card title="② 需求单（可人工核对）" size="small">
          <PlanReview plan={plan} />
          <Space style={{ marginTop: 16 }}>
            <Button onClick={() => setStep(0)}>← 改需求</Button>
            <Button type="primary" onClick={runPreview} loading={loading}>→ 预览抓取</Button>
          </Space>
        </Card>
      )}

      {/* Step 2: 预览抓取 */}
      {step === 2 && (
        <Card title="③ 抓取预览（批次 4：已接入）" size="small">
          <PreviewView preview={preview} />
          <Space style={{ marginTop: 16 }}>
            <Button onClick={() => setStep(1)}>← 改计划</Button>
            <Button type="primary" onClick={() => setStep(3)}>→ 下一步（骨架）</Button>
          </Space>
        </Card>
      )}

      {/* Step 3-5: 骨架 */}
      {step === 3 && (
        <Card title="④ 勾选落库（骨架）" size="small">
          <Alert
            type="info" showIcon
            message="勾选取数逻辑批次 4.1 接入"
            description="preview 返回候选行 → 前端 checkbox 勾选 → 收集 [code, period, indicator] 数组"
          />
          <Space style={{ marginTop: 16 }}>
            <Button onClick={() => setStep(2)}>← 改预览</Button>
            <Button type="primary" onClick={() => setStep(4)}>→ 下一步</Button>
          </Space>
        </Card>
      )}

      {step === 4 && (
        <Card title="⑤ 提交确认（骨架）" size="small">
          <Alert
            type="info" showIcon
            message="commit 步骤批次 4.1 接入"
            description="POST /api/ingest/commit 需要 require_admin；当前账号 admin 可走通，但勾选取数骨架未接好"
          />
          <Space style={{ marginTop: 16 }}>
            <Button onClick={() => setStep(3)}>← 回退</Button>
            <Button danger onClick={reset}>完成后重置</Button>
          </Space>
        </Card>
      )}
    </div>
  )
}

// ---- 子组件 ----

function PlanReview({ plan }: { plan: IngestPlan }) {
  return (
    <div>
      {plan.companies && plan.companies.length > 0 && (
        <Section title={`公司（${plan.companies.length} 家）`}>
          <Table
            size="small"
            pagination={false}
            rowKey="code"
            dataSource={plan.companies}
            columns={[
              { title: '代码', dataIndex: 'code' },
              { title: '名称', dataIndex: 'name' },
            ]}
          />
        </Section>
      )}
      {plan.indicators && plan.indicators.length > 0 && (
        <Section title={`指标（${plan.indicators.length} 项）`}>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {plan.indicators.map((ind, i) => (
              <Tag key={i} color="geekblue">{ind}</Tag>
            ))}
          </div>
        </Section>
      )}
      {plan.ratios && plan.ratios.length > 0 && (
        <Section title={`衍生比率（${plan.ratios.length} 项）`}>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {plan.ratios.map((r, i) => (
              <Tag key={i} color="purple">{r}</Tag>
            ))}
          </div>
        </Section>
      )}
      {plan.unresolved && plan.unresolved.length > 0 && (
        <Section title={`未归一（${plan.unresolved.length} 项）`}>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {plan.unresolved.map((u, i) => (
              <Tag key={i} color="orange">{u}</Tag>
            ))}
          </div>
          <Paragraph type="secondary" style={{ marginTop: 8 }}>
            未归一项是 LLM 识别但归一器没认出来的（新公司名、别名、非常规指标），
            可以在入库向导里手动修正后再提交。
          </Paragraph>
        </Section>
      )}
      <div style={{ marginTop: 8, fontSize: 12, color: '#888' }}>
        来源：<Tag>{plan.source || 'eastmoney'}</Tag>
        {plan.periods != null && <> · 年期望：{plan.periods}</>}
      </div>
    </div>
  )
}

function PreviewView({ preview }: { preview: Record<string, unknown> | null }) {
  if (!preview) return null
  // 批次 4 把 preview 整个 JSON 渲染出来（批次 4.1 再按候选行结构做 Table）
  return (
    <div>
      <Paragraph type="secondary">preview 端点原始返回（批次 4 先 JSON 全量展示）：</Paragraph>
      <pre
        style={{
          background: '#f6f7f9',
          padding: 12,
          borderRadius: 6,
          maxHeight: 320,
          overflow: 'auto',
          fontSize: 12,
        }}
      >
        {JSON.stringify(preview, null, 2)}
      </pre>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ fontWeight: 600, marginBottom: 6, color: '#555' }}>{title}</div>
      {children}
    </div>
  )
}
