// Vite's configuration for the web app, and Vitest's for its tests. The
// build goes into bauta/studio/static, which Studio serves; in development,
// Vite serves the app and passes /api to `bauta studio serve`.

import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: { outDir: '../../bauta/studio/static', emptyOutDir: true },
  server: { proxy: { '/api': 'http://127.0.0.1:8710' } },
  test: { environment: 'jsdom', restoreMocks: true },
})
