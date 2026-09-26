/**
 * 会话历史 Zustand store。
 *
 * 持久化：localStorage `fa.sessions`（最多 20 条，LRU 策略超出丢弃最老）。
 * 设计取舍：会话摘要存在前端（thread_id → title → lastAt → firstQuestion），
 * 回看完整对话内容时才从后端 `/api/hitl/{id}` 拉 —— Checkpointer 里存了完整终态。
 *
 * 为什么不用只在前端存完整对话？
 *   1) 单轮内容可能很长（流式 token + citations + web + verify），大对象 localStorage 会爆
 *   2) 多轮上下文存在后端 Checkpointer，前端本地存的只是"自己这次会话看到的"，重启丢失
 *   3) 后端 `/api/hitl/{id}` 是权威源
 */
import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export interface SessionSummary {
  threadId: string
  title: string         // 从 meta.question 取前 30 字，没 meta 就用 firstQuestion
  firstQuestion?: string
  intent?: string
  pending?: boolean
  lastAt: number        // Unix ms
}

const MAX_SESSIONS = 20

interface SessionState {
  sessions: SessionSummary[]
  activeThreadId: string | null
  // actions
  upsert: (s: SessionSummary) => void
  touch: (threadId: string, pending?: boolean) => void
  remove: (threadId: string) => void
  clear: () => void
  setActive: (threadId: string | null) => void
}

function makeTitle(question: string): string {
  const q = (question || '').trim().replace(/\s+/g, ' ')
  if (!q) return '(新会话)'
  return q.length > 30 ? q.slice(0, 30) + '…' : q
}

export const useSessionStore = create<SessionState>()(
  persist(
    (set) => ({
      sessions: [],
      activeThreadId: null,

      upsert: (s) =>
        set((prev) => {
          const idx = prev.sessions.findIndex((x) => x.threadId === s.threadId)
          let arr: SessionSummary[]
          if (idx >= 0) {
            // 更新（只变 lastAt / pending，不覆盖 title —— 首次设好就不再换）
            arr = [...prev.sessions]
            arr[idx] = {
              ...arr[idx],
              lastAt: s.lastAt,
              pending: s.pending ?? arr[idx].pending,
              intent: s.intent ?? arr[idx].intent,
            }
          } else {
            arr = [s, ...prev.sessions]
          }
          // LRU：超出 20 条丢最老
          if (arr.length > MAX_SESSIONS) arr = arr.slice(0, MAX_SESSIONS)
          return { sessions: arr, activeThreadId: s.threadId }
        }),

      touch: (threadId, pending) =>
        set((prev) => {
          const arr = prev.sessions.map((s) =>
            s.threadId === threadId
              ? { ...s, lastAt: Date.now(), pending: pending ?? s.pending }
              : s,
          )
          return { sessions: arr }
        }),

      remove: (threadId) =>
        set((prev) => ({
          sessions: prev.sessions.filter((s) => s.threadId !== threadId),
          activeThreadId:
            prev.activeThreadId === threadId ? null : prev.activeThreadId,
        })),

      clear: () => set({ sessions: [], activeThreadId: null }),

      setActive: (tid) => set({ activeThreadId: tid }),
    }),
    {
      name: 'fa.sessions',
      partialize: (s) => ({ sessions: s.sessions, activeThreadId: s.activeThreadId }),
    },
  ),
)

export { makeTitle }
