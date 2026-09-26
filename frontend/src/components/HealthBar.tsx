import { useEffect, useState } from 'react'
import { Tag, Spin } from 'antd'
import { http } from '../api/client'

interface HealthIndex { ok?: boolean; chunks?: number; docs?: number; error?: string }
interface HealthLLM { ready?: boolean; provider?: string; model?: string; reason?: string }
interface HealthVec { available?: boolean; reason?: string }
interface HealthRerank { available?: boolean; backend?: string; reason?: string }
interface HealthReg { available?: boolean; articles?: number }
interface HealthCkpt { exists?: boolean; backend?: string; threads?: number }
interface HealthAud { error?: string; rows?: number }
interface HealthAuth { enabled?: boolean; users_count?: number; jwt_secret_configured?: boolean }
interface HealthWeb { enabled?: boolean; provider_available?: boolean; backend?: string; corpus_rows?: number;
  provider_reason?: string; quota?: { remaining?: number; limit?: number } }

interface HealthResp {
  index?: HealthIndex
  llm?: HealthLLM
  vector?: HealthVec
  rerank?: HealthRerank
  regulation?: HealthReg
  checkpointer?: HealthCkpt
  audit?: HealthAud
  auth?: HealthAuth
  web?: HealthWeb
}

interface Channel {
  label: string
  ok: boolean
  detail: string
}

function buildChannels(h: HealthResp | null): Channel[] {
  if (!h) return [{ label: '服务', ok: false, detail: '无法连接 /api/health' }]
  const idx = h.index || {}, vec = h.vector || {}, rer = h.rerank || {}
  const reg = h.regulation || {}, ck = h.checkpointer || {}
  const aud = h.audit || {}, llm = h.llm || {}, au = h.auth || {}, w = h.web || {}

  return [
    { label: 'web', ok: !!(w.enabled && w.provider_available),
      detail: w.enabled
        ? (w.provider_available
            ? `ok · ${w.backend || '?'} · 语料 ${w.corpus_rows ?? '?'} 条`
            : `通道不可用 · ${w.provider_reason || '原因未知'}`)
          + (w.quota ? ` · 今日余 ${w.quota.remaining}/${w.quota.limit}` : '')
        : '关闭（不联网）' },
    { label: 'index', ok: !!idx.ok,
      detail: idx.ok
        ? `chunks=${idx.chunks ?? idx.docs ?? '?'}`
        : (idx.error || '不可用') },
    { label: 'vector', ok: !!vec.available,
      detail: vec.available ? 'ok' : (vec.reason || '降级：hybrid 退回 BM25') },
    { label: 'rerank', ok: !!rer.available,
      detail: rer.available ? (rer.backend || 'ok') : `${rer.backend || ''}${rer.reason ? ' · ' + rer.reason : ''} 直通` },
    { label: 'regulation', ok: !!reg.available,
      detail: reg.available ? `articles=${reg.articles ?? '?'}` : '法规库未建' },
    { label: 'checkpointer', ok: !!ck.exists,
      detail: `${ck.backend || 'sqlite'}${ck.exists ? ` · threads=${ck.threads ?? '?'}` : ' · 尚无落盘'}` },
    { label: 'audit', ok: !aud.error,
      detail: aud.error ? '读取失败' : `rows=${aud.rows ?? '?'}` },
    { label: 'backend', ok: !!llm.ready,
      detail: `${llm.provider || '?'} / ${llm.model || '?'}${llm.ready ? '' : ` · ${llm.reason || '未就绪'}`}` },
    { label: 'auth', ok: !!au.enabled,
      detail: au.enabled
        ? `enabled · users=${au.users_count ?? '?'}${au.jwt_secret_configured ? '' : ' · 缺 JWT 密钥'}`
        : '关闭（单机免登录）' },
  ]
}

/**
 * 顶栏下方的通道健康 chip 组。
 * 后端通道不可用是**设计内的降级**（未配 Key / 向量库没建），统一用黄色 warn，不用红叉。
 */
export default function HealthBar() {
  const [data, setData] = useState<HealthResp | null>(null)
  const [loading, setLoading] = useState(true)

  const load = async () => {
    try {
      const j = await http.get<HealthResp>('/api/health')
      setData(j)
    } catch {
      setData(null)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    // 每 15s 自动轮询（跟原前端一致）
    const t = setInterval(load, 15_000)
    return () => clearInterval(t)
  }, [])

  const channels = buildChannels(data)

  return (
    <div
      style={{
        padding: '8px 20px',
        borderBottom: '1px solid var(--border, #f0f0f0)',
        background: 'var(--panel, #fff)',
        display: 'flex',
        gap: 8,
        flexWrap: 'wrap',
        alignItems: 'center',
      }}
    >
      {loading && <Spin size="small" />}
      {!loading && channels.map((c) => (
        <Tag
          key={c.label}
          color={c.ok ? 'success' : 'warning'}
          style={{ margin: 0 }}
          title={c.detail}
        >
          {c.label}{c.ok ? ' ✓' : ' ⚠'}
          <span style={{ opacity: 0.75, marginLeft: 6 }}>{c.detail}</span>
        </Tag>
      ))}
    </div>
  )
}
