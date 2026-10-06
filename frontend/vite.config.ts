import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  base: './',
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/datasources': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true
      },
      '/query': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true
      },
      '/query/explain': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true
      }
    }
  }
})
