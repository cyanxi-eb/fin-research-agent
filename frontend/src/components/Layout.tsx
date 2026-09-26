import { useState } from 'react'
import { Outlet, useNavigate, useLocation } from 'react-router-dom'
import { Tabs } from 'antd'

import HealthBar from '../components/HealthBar'
import SessionSidebar from '../components/SessionSidebar'

/**
 * 批次 4：Layout 顶栏加 Tabs 切换工作台 / 对比 / 入库向导。
 * Tabs 用 location.pathname 做受控，切换通过 navigate。
 */
const TABS = [
  { key: '/', label: '工作台' },
  { key: '/compare', label: '指标对比' },
  { key: '/ingest', label: '数据入库' },
]

export default function Layout() {
  const navigate = useNavigate()
  const loc = useLocation()
  const [replaySignal, setReplaySignal] = useState<{ threadId: string; nonce: number } | null>(null)
  const [newSignal, setNewSignal] = useState<number>(0)

  const activeTab = TABS.find((t) =>
    t.key === '/' ? loc.pathname === '/' : loc.pathname.startsWith(t.key),
  )?.key ?? '/'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
      <header
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '8px 20px 0',
          borderBottom: '1px solid var(--border, #f0f0f0)',
          background: 'var(--panel, #fff)',
        }}
      >
        <div style={{ fontSize: 17, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 16 }}>
          Fin Research Agent
          <small style={{ fontWeight: 400, color: '#888' }}>
            年报问答 · 可核验溯源
          </small>
        </div>
      </header>

      {/* Tabs 导航（批次 4 新增） */}
      <div
        style={{
          padding: '0 20px',
          background: 'var(--panel, #fff)',
          borderBottom: '1px solid var(--border, #f0f0f0)',
        }}
      >
        <Tabs
          activeKey={activeTab}
          onChange={(k) => navigate(k)}
          items={TABS}
          size="small"
          style={{ margin: 0 }}
        />
      </div>

      <HealthBar />

      <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
        {/* 只有工作台(/ 开头)显示会话侧栏；对比/入库是工具页，不需要 */}
        {loc.pathname === '/' && (
          <SessionSidebar
            onSelect={(tid) =>
              setReplaySignal({ threadId: tid, nonce: Date.now() })
            }
            onNew={() => setNewSignal((n) => n + 1)}
          />
        )}
        <main style={{ flex: 1, padding: 20, overflowY: 'auto' }}>
          <Outlet context={{ replaySignal, newSignal }} />
        </main>
      </div>
    </div>
  )
}
