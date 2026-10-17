import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The build goes into bauta/studio, which serves it; in development,
// Vite serves the app and passes /api to `bauta-studio serve`.
export default defineConfig({
  plugins: [react()],
  build: { outDir: '../../bauta/studio/static', emptyOutDir: true },
  server: { proxy: { '/api': 'http://127.0.0.1:8710' } },
})
