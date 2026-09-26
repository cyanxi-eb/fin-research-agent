/**
 * 鉴权状态：access_token / refresh_token / user。
 *
 * 持久化：token 类放 localStorage（现有前端的选择，无痕模式可能禁用 → 降级内存）。
 * 刷新链：api/client.ts 的 401 拦截器会调 setAuth / logout，store 不关心刷新细节。
 */
import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export interface AuthUser {
  sub?: string
  username?: string
  role?: string
  auth_enabled?: boolean
  [k: string]: unknown
}

interface AuthState {
  accessToken: string | null
  refreshToken: string | null
  user: AuthUser | null
  // actions
  setAuth: (access: string, refresh: string, user: AuthUser | null) => void
  setUser: (user: AuthUser) => void
  logout: () => void
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      accessToken: null,
      refreshToken: null,
      user: null,

      setAuth: (access, refresh, user) =>
        set({ accessToken: access, refreshToken: refresh, user }),

      setUser: (user) => set({ user }),

      logout: () =>
        set({ accessToken: null, refreshToken: null, user: null }),
    }),
    {
      name: 'fa.auth',
      partialize: (s) => ({
        accessToken: s.accessToken,
        refreshToken: s.refreshToken,
        user: s.user,
      }),
    },
  ),
)
