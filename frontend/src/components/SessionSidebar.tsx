import { Button, Empty, Space } from 'antd'
import { PlusOutlined } from '@ant-design/icons'

/**
 * 会话侧栏：批次 1 先搭壳（本地状态 + UI 占位），批次 2 接入 /api/hitl/{thread_id} 回看链路。
 * 持久化策略沿用原前端：localStorage 存 thread_id → 标题 → 时间戳列表（最多 20 条）。
 */
export default function SessionSidebar() {
  return (
    <aside
      style={{
        flex: '0 0 232px',
        width: 232,
        borderRight: '1px solid var(--border, #f0f0f0)',
        padding: 12,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        background: 'var(--panel, #fff)',
      }}
    >
      <div style={{ fontSize: 12, color: '#888', fontWeight: 600, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span>会话历史</span>
        <Button size="small" type="text" icon={<PlusOutlined />}>新</Button>
      </div>

      <div style={{ flex: 1, overflowY: 'auto' }}>
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            <span style={{ color: '#bbb', fontSize: 12 }}>
              批次 1 占位。批次 2 接 localStorage 会话回看。
            </span>
          }
        />
      </div>

      <Space direction="vertical" size={4} style={{ fontSize: 11, color: '#bbb' }}>
        <span>批次 1: 登录 → 最小问答</span>
        <span>批次 2: 会话侧栏 + 回看</span>
      </Space>
    </aside>
  )
}
