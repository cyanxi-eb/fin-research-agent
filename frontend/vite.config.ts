import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // 后端 FastAPI 默认 8000；鉴权/问答/对比/入库/审计/联网全走 /api 前缀
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  // Phase 1 批次 5：构建产物复制到根目录 web/，由 FastAPI StaticFiles 直接 serve
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
