import { useMemo, useState } from 'react'
import {
  Button, Card, Form, Input, Select, Table, Tag, Alert, Row, Col, App as AntdApp,
} from 'antd'
import { LineChartOutlined } from '@ant-design/icons'

import { comparePost } from '../api/compare'
import type { CompareResp, CompanyRow, ChartPayload } from '../api/compare'

/**
 * 批次 4：指标对比页。
 *
 * 输入区：indicator + codes（Tag 输入）+ period（可选）
 * 输出区：对比表（antd Table）+ 手写 SVG 折线图（零额外依赖）
 *
 * 为什么手写 SVG 而不是装 recharts/echarts？
 * 一张折线图而已（x_axis 周期 × series 多家公司），用纯 SVG 画比引一个 100KB+ 库轻量，
 * 也比 antd v6 自带图表组件可控。批次 5 code splitting 后体积进一步看需求。
 */

// 常用财务指标（来自后端 indicators.py，给用户下拉参考；不做全量枚举怕漂移）
const SAMPLE_INDICATORS = [
  '营业总收入', '营业收入', '归属于上市公司股东的净利润', '总资产',
  '总负债', '股东权益合计', '经营活动产生的现金流量净额',
  '资产负债率', 'ROE', 'ROA', '毛利率',
]

// seed 数据里存在的公司（health 返回 companies=5）
const SAMPLE_CODES = [
  '贵州茅台', '五粮液', '泸州老窖', '山西汾酒', '洋河股份',
]

export default function ComparePage() {
  const { message } = AntdApp.useApp()
  const [loading, setLoading] = useState(false)
  const [resp, setResp] = useState<CompareResp | null>(null)

  const onFinish = async (values: {
    indicator: string
    codes: string[]
    period?: string
  }) => {
    if (!values.codes || values.codes.length < 2) {
      message.warning('至少选择 2 家公司')
      return
    }
    setLoading(true)
    try {
      const data = await comparePost(values.indicator, values.codes, values.period || null)
      setResp(data)
      if (!data.ok) {
        message.warning(data.note || '对比未命中（检查指标名或公司）')
      }
    } catch (err: unknown) {
      message.error(
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
          '对比请求失败',
      )
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ maxWidth: 1100, margin: '0 auto' }}>
      <Card title="多公司同指标对比" size="small" style={{ marginBottom: 16 }}>
        <Form
          layout="vertical"
          onFinish={onFinish}
          initialValues={{ indicator: '营业总收入', codes: ['贵州茅台', '五粮液'] }}
        >
          <Row gutter={16}>
            <Col xs={24} sm={8}>
              <Form.Item
                label="财务指标"
                name="indicator"
                rules={[{ required: true, message: '请输入指标名' }]}
              >
                <Input list="indicators-list" placeholder="输入或选择指标" />
              </Form.Item>
              <datalist id="indicators-list">
                {SAMPLE_INDICATORS.map((i) => <option key={i} value={i} />)}
              </datalist>
            </Col>
            <Col xs={24} sm={12}>
              <Form.Item
                label="对比公司（至少 2 家）"
                name="codes"
                rules={[{ required: true, message: '请至少选 2 家' }]}
              >
                <Select
                  mode="tags"
                  placeholder="输入公司名/代码，回车添加"
                  tokenSeparators={[',']}
                  options={SAMPLE_CODES.map((c) => ({ value: c, label: c }))}
                />
              </Form.Item>
            </Col>
            <Col xs={24} sm={4}>
              <Form.Item label="报告期（可选）" name="period">
                <Input placeholder="留空取各最新期" />
              </Form.Item>
            </Col>
          </Row>
          <Button type="primary" htmlType="submit" loading={loading} icon={<LineChartOutlined />}>
            对比
          </Button>
        </Form>
      </Card>

      {resp && (
        <CompareResult resp={resp} />
      )}
    </div>
  )
}

function CompareResult({ resp }: { resp: CompareResp }) {
  const rows = resp.rows ?? []
  const chart = resp.chart

  const columns = useMemo(
    () => [
      { title: '公司', dataIndex: ['name'], key: 'name', render: (_: unknown, r: CompanyRow) => r.name || r.code },
      { title: '代码', dataIndex: 'code', key: 'code', width: 120 },
      { title: '报告期', dataIndex: 'period', key: 'period', width: 130 },
      {
        title: resp.indicator + (resp.unit ? `（${resp.unit}）` : ''),
        dataIndex: 'value',
        key: 'value',
        width: 180,
        render: (v: number | string | null) => {
          if (v == null) return <Tag color="default">—</Tag>
          if (typeof v === 'number') return v.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
          return v
        },
      },
    ],
    [resp.indicator, resp.unit],
  )

  return (
    <>
      {!resp.periods_consistent && resp.note && (
        <Alert
          style={{ marginBottom: 12 }}
          type="warning"
          showIcon
          message="各家报告期不一致"
          description={resp.note}
        />
      )}

      {!resp.ok && resp.note && (
        <Alert style={{ marginBottom: 12 }} type="info" showIcon message={resp.note} />
      )}

      {chart && chart.series && (
        <Card title="趋势" size="small" style={{ marginBottom: 16 }}>
          <TrendSvg chart={chart} />
        </Card>
      )}

      <Card title="对比表" size="small">
        <Table
          size="small"
          rowKey={(r: CompanyRow) => r.code + '|' + (r.period ?? '')}
          dataSource={rows}
          columns={columns}
          pagination={false}
        />
      </Card>
    </>
  )
}

// ---- 手写 SVG 折线图 ----

const LINE_COLORS = [
  '#2b6cb0', '#d9534f', '#2f855a', '#d69e2e', '#805ad5',
  '#319795', '#dd6b20', '#e53e3e', '#38a169', '#4c51bf',
]

function TrendSvg({ chart }: { chart: ChartPayload }) {
  const { x_axis = [], series = [] } = chart
  if (!series.length || !x_axis.length) {
    return <div style={{ color: '#bbb', textAlign: 'center', padding: 24 }}>无趋势数据</div>
  }

  const W = 720
  const H = 260
  const padL = 52, padR = 16, padT = 24, padB = 36
  const plotW = W - padL - padR
  const plotH = H - padT - padB

  // 收集所有 series 的数值范围（含 null 跳过）
  const allVals: number[] = []
  series.forEach((s) => s.values.forEach((v) => { if (typeof v === 'number' && !Number.isNaN(v)) allVals.push(v) }))
  if (allVals.length === 0) return <div style={{ color: '#bbb', textAlign: 'center', padding: 24 }}>数值全为 null</div>
  let vmin = Math.min(...allVals), vmax = Math.max(...allVals)
  if (vmin === vmax) { vmin -= 1; vmax += 1 }
  const vpad = (vmax - vmin) * 0.08
  vmin -= vpad; vmax += vpad

  const xStep = plotW / Math.max(1, x_axis.length - 1)
  const yScale = (v: number) => padT + plotH - ((v - vmin) / (vmax - vmin)) * plotH

  const yTicks = 4
  const ticks = Array.from({ length: yTicks + 1 }, (_, i) => vmin + ((vmax - vmin) * i) / yTicks)

  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ maxWidth: W, fontFamily: 'inherit' }}>
      {/* Y 轴网格线 + 刻度 */}
      {ticks.map((t, i) => {
        const y = yScale(t)
        return (
          <g key={i}>
            <line x1={padL} y1={y} x2={padL + plotW} y2={y} stroke="#f0f0f0" />
            <text x={padL - 8} y={y + 4} fontSize={10} fill="#888" textAnchor="end">
              {formatNum(t)}
            </text>
          </g>
        )
      })}

      {/* X 轴标签 */}
      {x_axis.map((lbl, i) => {
        const x = padL + i * xStep
        return (
          <text key={i} x={x} y={H - padB + 16} fontSize={10} fill="#888" textAnchor="middle">
            {lbl}
          </text>
        )
      })}

      {/* series 折线 */}
      {series.map((s, si) => {
        const color = LINE_COLORS[si % LINE_COLORS.length]
        const points: string[] = []
        s.values.forEach((v, i) => {
          if (typeof v === 'number' && !Number.isNaN(v)) {
            const x = padL + i * xStep
            const y = yScale(v)
            points.push(`${x},${y}`)
          }
        })
        const pathD = points.length > 1 ? `M${points.join(' L')}` : ''
        return (
          <g key={si}>
            <path d={pathD} fill="none" stroke={color} strokeWidth={2} />
            {s.values.map((v, i) => {
              if (typeof v !== 'number' || Number.isNaN(v)) return null
              const x = padL + i * xStep
              const y = yScale(v)
              return <circle key={i} cx={x} cy={y} r={3} fill={color} />
            })}
          </g>
        )
      })}

      {/* 图例 */}
      <g transform={`translate(${padL + plotW - 16}, ${padT - 4})`}>
        {series.slice(0, 6).map((s, i) => (
          <g key={i} transform={`translate(${-((series.slice(0, 6).length - 1 - i) * 100)}, 0)`}>
            <rect width={10} height={10} fill={LINE_COLORS[i % LINE_COLORS.length]} x={-14} y={-8} />
            <text x={-2} y={0} fontSize={11} fill="#444">{s.name}</text>
          </g>
        ))}
      </g>
    </svg>
  )
}

function formatNum(v: number): string {
  if (Math.abs(v) >= 1e12) return (v / 1e12).toFixed(1) + '万亿'
  if (Math.abs(v) >= 1e8) return (v / 1e8).toFixed(1) + '亿'
  if (Math.abs(v) >= 1e4) return (v / 1e4).toFixed(1) + '万'
  return v.toFixed(v % 1 === 0 ? 0 : 2)
}
