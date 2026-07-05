import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    host: true,
    proxy: {
      '/auth': { target: 'http://api:8000', changeOrigin: true },
      '/v1/dashboard': { target: 'http://api:8000', changeOrigin: true },
    },
  },
})
