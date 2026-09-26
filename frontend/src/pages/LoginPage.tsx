import { useState } from 'react'
import { Button, Card, Form, Input, App as AntdApp } from 'antd'
import { useNavigate } from 'react-router-dom'

import { useAuthStore } from '../store/authStore'
import { http } from '../api/client'

interface LoginResp {
  ok: boolean
  access_token: string
  refresh_token: string
  expires_in?: number
  user?: { sub?: string; username?: string; role?: string }
  detail?: string | Record<string, unknown>
}

/**
 * 登录页：POST /api/auth/login → 成功后写 authStore → 跳 /。
 *
 * 鉴权开启（后端 AUTH_ENABLED=1）时是入口；单机免登录（AUTH_ENABLED=0）
 * 时后端 /api/auth/login 可能不强制，但前端按统一路径走。
 */
export default function LoginPage() {
  const { message } = AntdApp.useApp()
  const navigate = useNavigate()
  const setAuth = useAuthStore((s) => s.setAuth)
  const [loading, setLoading] = useState(false)

  const onFinish = async (values: { username: string; password: string }) => {
    setLoading(true)
    try {
      const data = await http.post<LoginResp>('/api/auth/login', values)
      if (data?.access_token) {
        setAuth(data.access_token, data.refresh_token, data.user ?? null)
        message.success('登录成功')
        navigate('/', { replace: true })
      } else {
        message.error((data?.detail as string) || '登录失败')
      }
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string }; status?: number } })
          ?.response?.data?.detail ||
        '网络不可达，请确认后端已启动'
      message.error(String(msg))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        justifyContent: 'center',
        paddingTop: 64,
        background: 'var(--bg, #f6f7f9)',
      }}
    >
      <Card
        style={{ width: 380, boxShadow: '0 4px 24px rgba(0,0,0,.08)' }}
        bordered
      >
        <div style={{ fontSize: 20, fontWeight: 600, marginBottom: 4 }}>
          Fin Research Agent
        </div>
        <div style={{ color: '#888', marginBottom: 24 }}>
          年报问答 · 可核验溯源
        </div>

        <Form layout="vertical" onFinish={onFinish} disabled={loading}>
          <Form.Item
            label="用户名"
            name="username"
            rules={[{ required: true, message: '请输入用户名' }]}
          >
            <Input autoComplete="username" placeholder="请输入用户名" />
          </Form.Item>

          <Form.Item
            label="口令"
            name="password"
            rules={[{ required: true, message: '请输入口令' }]}
          >
            <Input.Password autoComplete="current-password" placeholder="请输入口令" />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0 }}>
            <Button type="primary" htmlType="submit" block loading={loading}>
              登录
            </Button>
          </Form.Item>
        </Form>

        <div style={{ marginTop: 16, fontSize: 12, color: '#aaa', lineHeight: 1.7 }}>
          演示账号见 README 的「首次使用」一节；单机部署可设置
          <code> FA_AUTH_ENABLED=0 </code> 免登录直接使用。
        </div>
      </Card>
    </div>
  )
}
