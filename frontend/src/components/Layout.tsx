import { useState } from 'react'
import { Outlet } from 'react-router-dom'

import HealthBar from '../components/HealthBar'
import SessionSidebar from '../components/SessionSidebar'

/**
 * 批次 3：侧栏回调提升到 Layout 层，ChatPage 通过 context prop 接收。
 *
 * 为什么不直接让 SessionSidebar 改 ChatPage 状态？
 * 因为它们是兄弟组件（都在 Layout 下），共享状态提升到父组件是 React 标准做法。
 * Layout 只管"选中了哪个 threadId 要回看"的信号，回看数据拉取由 ChatPage 自己做。
 */
export default function Layout() {
  const [replaySignal, setReplaySignal] = useState<{ threadId: string; nonce: number } | null>(null)
  const [newSignal, setNewSignal] = useState<number>(0)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
      <header
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '12px 20px',
          borderBottom: '1px solid var(--border, #f0f0f0)',
          background: 'var(--panel, #fff)',
        }}
      >
        <div style={{ fontSize: 17, fontWeight: 600 }}>
          Fin Research Agent
          <small style={{ fontWeight: 400, color: '#888', marginLeft: 8 }}>
            年报问答 · 可核验溯源
          </small>
        </div>
      </header>

      <HealthBar />

      <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
        <SessionSidebar
          onSelect={(tid) =>
            setReplaySignal({ threadId: tid, nonce: Date.now() })
          }
          onNew={() => setNewSignal((n) => n + 1)}
        />
        <main style={{ flex: 1, padding: 20, overflowY: 'auto' }}>
          <Outlet context={{ replaySignal, newSignal }} />
        </main>
      </div>
    </div>
  )
}
