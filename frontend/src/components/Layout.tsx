import { Outlet } from 'react-router-dom'

import HealthBar from '../components/HealthBar'
import SessionSidebar from '../components/SessionSidebar'

/**
 * 工作台壳：顶栏（健康条 + 用户 chip）+ 侧栏（会话列表）+ 主区（Outlet）。
 * 批次 1 先把壳搭好，批次 2+ 各页面逐个填进去。
 */
export default function Layout() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
      {/* 顶栏 */}
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

      {/* 通道健康条 */}
      <HealthBar />

      {/* 两栏工作台 */}
      <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
        <SessionSidebar />
        <main style={{ flex: 1, padding: 20, overflowY: 'auto' }}>
          <Outlet />
        </main>
      </div>
    </div>
  )
}
