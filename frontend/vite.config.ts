/// <reference types="vitest/config" />
// ABOUTME: Vite config — dev server proxies /api to Django; production build
// ABOUTME: is served by Django under /static/app/ with SPA catch-all.
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: '/static/app/',
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['./src/**/*.test.{ts,tsx}'],
    setupFiles: ['./src/test/setup.ts'],
    // The multi-step correspondence flows drive a dozen real user
    // interactions each. Vitest's 5s default is inside their honest cost
    // under full-suite load, which made the gate flaky rather than strict.
    testTimeout: 20000,
    coverage: {
      // V8 rather than Istanbul instrumentation: no Babel pass, so the
      // coverage run costs little more than the plain one.
      provider: 'v8',
      // `json` is the machine-readable report scripts/patch_coverage.py reads;
      // `text` is for the developer watching the run.
      reporter: ['text', 'json'],
      reportsDirectory: '../.verification/frontend-coverage',
      // Report every source file, not only the ones a test happened to
      // import: an untested new module must show as uncovered, not absent.
      // Naming `include` is what does that in Vitest 3+ (it replaced `all`);
      // without it, coverage covers only files the tests pulled in.
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/**/*.test.{ts,tsx}',
        'src/test/**',
        'src/**/*.d.ts',
        'src/main.tsx',
      ],
    },
  },
})
