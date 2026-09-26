/**
 * 会话历史 Zustand store — Phase 2 批次 1 改造成后端 sync。
 *
 * 批次 1 之前：只有 localStorage fa.sessions（最多 20 条 LRU）。
 * 批次 1 之后：
 *   - 主数据源：后端 /api/sessions CRUD
 *   - localStorage fa.sessions：降级缓存（后端不可达或 401 时用）
 *   - upsert/touch/remove：乐观更新本地态 + 异步调后端
 *   - init()：启动时从后端 listSessions() 拉一次
 *
 * 为什么"乐观更新 + 异步后端"而不是"等后端返回再改本地"？
 *   会话摘要只是元数据，后端挂了本地 UI 也要跑；让后端调用 await 住每次 meta 事件会
 *   让 SSE 流式被网络延迟拖累。所以先改本地，再 try/catch 异步 flush 后端。
 */
import { create } from 'zustand'
import { persist } from 'zustand/middleware'

import * as sessionsApi from '../api/sessions'

export interface SessionSummary {
  threadId: string
  title: string
  firstQuestion?: string
  intent?: string
  pending?: boolean
  lastAt: number
}

const MAX_SESSIONS = 50  // 后端支持 500，本地最多保留 50

interface SessionState {
  sessions: SessionSummary[]
  activeThreadId: string | null
  hydrated: boolean  // init() 完成后置 true，前端才渲染真实会话
  // actions
  init: () => Promise<void>
  upsert: (s: SessionSummary) => void
  touch: (threadId: string, pending?: boolean, intent?: string, turnsDelta?: number) => void
  remove: (threadId: string) => Promise<void>
  clear: () => void
  setActive: (threadId: string | null) => void
}

function makeTitle(question: string): string {
  const q = (question || '').trim().replace(/\s+/g, ' ')
  if (!q) return '(新会话)'
  return q.length > 30 ? q.slice(0, 30) + '…' : q
}

function backendRowToSummary(r: sessionsApi.SessionRow): SessionSummary {
  return {
    threadId: r.thread_id,
    title: r.title || r.first_question || r.thread_id.slice(0, 12) + '…',
    firstQuestion: r.first_question ?? undefined,
    intent: r.intent ?? undefined,
    pending: !!r.pending,
    lastAt: r.last_at ? new Date(r.last_at).getTime() : Date.now(),
  }
}

export const useSessionStore = create<SessionState>()(
  persist(
    (set, get) => ({
      sessions: [],
      activeThreadId: null,
      hydrated: false,

      async init() {
        try {
          const data = await sessionsApi.listSessions({ limit: MAX_SESSIONS })
          const list = data.sessions.map(backendRowToSummary)
          set({ sessions: list, hydrated: true })
        } catch {
          // 后端不可达（或 401 被 axios 拦截器清 token）：保留 localStorage 的降级缓存
          set({ hydrated: true })
        }
      },

      upsert: (s) => {
        set((prev) => {
          const idx = prev.sessions.findIndex((x) => x.threadId === s.threadId)
          let arr: SessionSummary[]
          if (idx >= 0) {
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
          if (arr.length > MAX_SESSIONS) arr = arr.slice(0, MAX_SESSIONS)
          return { sessions: arr, activeThreadId: s.threadId }
        })
        // 异步 flush 后端（乐观更新）
        const state = get()
        const cur = state.sessions.find((x) => x.threadId === s.threadId)
        if (cur) {
          void sessionsApi.touchSession(s.threadId, {
            title: cur.title,
            first_question: cur.firstQuestion,
            intent: cur.intent,
            pending: cur.pending ?? false,
          }).catch(() => {})
        }
      },

      touch: (threadId, pending, intent, turnsDelta = 0) => {
        set((prev) => {
          const arr = prev.sessions.map((s) =>
            s.threadId === threadId
              ? {
                  ...s,
                  lastAt: Date.now(),
                  pending: pending ?? s.pending,
                  intent: intent ?? s.intent,
                }
              : s,
          )
          return { sessions: arr }
        })
        void sessionsApi.touchSession(threadId, {
          pending: pending,
          intent: intent,
          turns_delta: turnsDelta,
        }).catch(() => {})
      },

      async remove(threadId) {
        set((prev) => ({
          sessions: prev.sessions.filter((s) => s.threadId !== threadId),
          activeThreadId:
            prev.activeThreadId === threadId ? null : prev.activeThreadId,
        }))
        try {
          await sessionsApi.deleteSession(threadId)
        } catch {
          // 后端挂了：本地已删，不回滚（幂等删）
        }
      },

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
