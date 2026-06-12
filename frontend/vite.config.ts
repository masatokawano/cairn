import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev mode proxies /api to the FastAPI server (port 8730).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8730',
    },
  },
})
