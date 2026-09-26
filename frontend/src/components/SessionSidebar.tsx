import { Button, Empty, Popconfirm, Tag, Tooltip } from 'antd'
import { DeleteOutlined, PlusOutlined, HistoryOutlined } from '@ant-design/icons'

import { useSessionStore } from '../store/sessionStore'

function formatTime(ts: number): string {
  const d = new Date(ts)
  const now = new Date()
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  if (sameDay) return `今天 ${hh}:${mm}`
  const yyyy = d.getFullYear() === now.getFullYear() ? '' : `${d.getFullYear()}/`
  return `${yyyy}${d.getMonth() + 1}/${d.getDate()} ${hh}:${mm}`
}

/**
 * 批次 3 重写后的会话侧栏：
 *   - 从 sessionStore 读（localStorage fa.sessions，最多 20 条 LRU）
 *   - 每条显示：title + 时间 + pending 标签（挂起中）
 *   - 点击 → setActive（ChatPage 监听后会调 /api/hitl/{id} 拉回 turns）
 *   - 新会话按钮 → 清 active + 跳全新 ChatPage 状态
 *   - 删除 → 本地删（批次 3 不删后端 Checkpointer，避免误删挂起会话）
 */
export default function SessionSidebar({
  onSelect,
  onNew,
}: {
  onSelect?: (threadId: string) => void
  onNew?: () => void
}) {
  const sessions = useSessionStore((s) => s.sessions)
  const activeThreadId = useSessionStore((s) => s.activeThreadId)
  const remove = useSessionStore((s) => s.remove)
  const setActive = useSessionStore((s) => s.setActive)

  const handleClick = (tid: string) => {
    setActive(tid)
    onSelect?.(tid)
  }

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
      <div
        style={{
          fontSize: 12,
          color: '#888',
          fontWeight: 600,
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}
      >
        <span>
          <HistoryOutlined style={{ marginRight: 4 }} /> 会话历史
        </span>
        <Button
          size="small"
          type="text"
          icon={<PlusOutlined />}
          onClick={() => {
            setActive(null)
            onNew?.()
          }}
        >
          新
        </Button>
      </div>

      <div style={{ flex: 1, overflowY: 'auto' }}>
        {sessions.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={<span style={{ color: '#bbb', fontSize: 12 }}>尚无历史</span>}
          />
        ) : (
          sessions.map((s) => {
            const active = s.threadId === activeThreadId
            return (
              <div
                key={s.threadId}
                onClick={() => handleClick(s.threadId)}
                style={{
                  padding: '8px 10px',
                  marginBottom: 6,
                  borderRadius: 6,
                  cursor: 'pointer',
                  background: active ? '#eaf1fb' : 'transparent',
                  border: active ? '1px solid #91caff' : '1px solid transparent',
                  transition: 'background .15s',
                }}
                onMouseEnter={(e) => {
                  if (!active) (e.currentTarget as HTMLDivElement).style.background = '#f5f7fa'
                }}
                onMouseLeave={(e) => {
                  if (!active) (e.currentTarget as HTMLDivElement).style.background = 'transparent'
                }}
              >
                <div
                  style={{
                    fontSize: 13,
                    fontWeight: 500,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                    marginBottom: 4,
                  }}
                >
                  {s.title}
                </div>
                <div
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                  }}
                >
                  <span style={{ fontSize: 11, color: '#aaa' }}>
                    {formatTime(s.lastAt)}
                  </span>
                  <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                    {s.pending && (
                      <Tag color="warning" style={{ margin: 0 }}>挂起</Tag>
                    )}
                    {s.intent && s.intent !== 'rag' && (
                      <Tag style={{ margin: 0, fontSize: 10 }}>{s.intent}</Tag>
                    )}
                    <Tooltip title="删除（只清本地）">
                      <Popconfirm
                        title="删除此会话？"
                        description="仅清本地侧栏记录，不会删除后端 Checkpointer 里的挂起状态"
                        okText="删"
                        cancelText="取消"
                        onConfirm={(e) => {
                          e?.stopPropagation?.()
                          remove(s.threadId)
                        }}
                      >
                        <Button
                          size="small"
                          type="text"
                          danger
                          icon={<DeleteOutlined />}
                          onClick={(e) => e.stopPropagation()}
                        />
                      </Popconfirm>
                    </Tooltip>
                  </div>
                </div>
              </div>
            )
          })
        )}
      </div>

      <div style={{ fontSize: 11, color: '#bbb', borderTop: '1px solid var(--border, #f0f0f0)', paddingTop: 6 }}>
        最多 20 条 · 浏览器本地存
      </div>
    </aside>
  )
}
