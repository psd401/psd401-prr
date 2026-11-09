import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

// The criterion this flow exists for: a Records Officer finds the model
// comparison under Oversight and reads each model's metrics side by side.
test('officer-ai-evaluation-report', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)

  await page.goto('/oversight')
  await page.getByRole('link', { name: 'AI evaluation' }).click()
  const table = page.getByRole('table', { name: 'Intake classification model comparison' })
  await expect(table.getByRole('row').filter({ hasText: 'synthetic-model-a' })).toContainText('$0.0048')
  await expect(table.getByRole('row').filter({ hasText: 'synthetic-model-b' })).toContainText('no price')
  await page.screenshot({
    path: path.resolve('../.verification/officer-ai-evaluation-report.png'),
    fullPage: false,
  })
})
