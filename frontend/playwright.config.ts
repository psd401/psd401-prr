import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  failOnFlakyTests: true,
  forbidOnly: !!process.env.CI,
  outputDir: '../.verification/playwright-results',
  reporter: [
    ['list'],
    ['html', { outputFolder: '../.verification/playwright-report', open: 'never' }],
    ['json', { outputFile: '../.verification/playwright-results.json' }],
  ],
  use: {
    baseURL: 'http://127.0.0.1:4173',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: [
    {
      command: 'bash ../scripts/run-e2e-server.sh',
      url: 'http://127.0.0.1:8000/',
      timeout: 120_000,
      reuseExistingServer: false,
      env: {
        DJANGO_SETTINGS_MODULE: 'config.e2e_settings',
        PRR_AUTH_MODE: 'local',
        PYTHON_DOTENV_DISABLED: '1',
      },
    },
    {
      command: 'bun run dev -- --host 127.0.0.1 --port 4173 --base /',
      url: 'http://127.0.0.1:4173/',
      timeout: 120_000,
      reuseExistingServer: false,
    },
  ],
})
