import { lazy, Suspense, useEffect, useState, type ReactNode } from 'react'
import { BrowserRouter, Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { App as AntdApp, ConfigProvider, theme } from 'antd'
import zhCN from 'antd/locale/zh_CN'

import Layout from './components/Layout'
import LoginPage from './pages/LoginPage'
import ChatPage from './pages/ChatPage'
// 批次 5: code splitting — compare / ingest 是低频页面，用 lazy + Suspense 拆独立 chunk
const ComparePage = lazy(() => import('./pages/ComparePage'))
const IngestPage = lazy(() => import('./pages/IngestPage'))
import { useAuthStore } from './store/authStore'
import { useSessionStore } from './store/sessionStore'
import { http } from './api/client'

/**
 * 路由守卫：未登录访问受保护路径 → 跳 /login。
 * /api/auth/me 会在加载时验活本地令牌，401 自动清 store。
 */
function RequireAuth({ children }: { children: ReactNode }) {
  const accessToken = useAuthStore((s) => s.accessToken)
  const logout = useAuthStore((s) => s.logout)
  const navigate = useNavigate()
  const [checking, setChecking] = useState(true)
  const sessionInit = useSessionStore((s) => s.init)

  useEffect(() => {
    if (!accessToken) {
      setChecking(false)
      return
    }
    http.get('/api/auth/me')
      .then(() => {
        // Phase 2 批次 1：登录后从后端拉一次会话列表（替换 localStorage 降级缓存）
        void sessionInit()
      })
      .catch(() => { logout(); navigate('/login', { replace: true }) })
      .finally(() => setChecking(false))
  }, [accessToken, logout, navigate, sessionInit])

  if (checking) return <div style={{ padding: 40, color: '#888' }}>验证中…</div>
  if (!accessToken) return <Navigate to="/login" replace />
  return children
}

function LoginRedirect() {
  // 已登录访问 /login → 跳 /
  const accessToken = useAuthStore((s) => s.accessToken)
  if (accessToken) return <Navigate to="/" replace />
  return <LoginPage />
}

/** 路由表。批次 1 只有 chat，批次 2+ 逐个追加 compare / ingest / audit。 */
function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginRedirect />} />

      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route path="/" element={<ChatPage />} />
        <Route path="/compare" element={
          <Suspense fallback={<div style={{ padding: 40, color: '#888' }}>加载中…</div>}>
            <ComparePage />
          </Suspense>
        } />
        <Route path="/ingest" element={
          <Suspense fallback={<div style={{ padding: 40, color: '#888' }}>加载中…</div>}>
            <IngestPage />
          </Suspense>
        } />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}

export default function App() {
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: theme.defaultAlgorithm,
        token: {
          colorPrimary: '#2b6cb0',
          borderRadius: 6,
        },
      }}
    >
      <AntdApp>
        <BrowserRouter>
          <AppRoutes />
        </BrowserRouter>
      </AntdApp>
    </ConfigProvider>
  )
}
