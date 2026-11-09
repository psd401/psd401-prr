import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

const caseNumber = 'PRR-E2E-0011'

// The criterion this flow exists for: records the AI calls non-responsive with
// high confidence are only staged; nothing is discarded until a reviewer checks
// a random elusion sample of the pile, finds nothing responsive, and signs off.
test('reviewer-elusion-signoff', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page, 'e2e-reviewer', 'e2e-reviewer-local-only')
  await page.goto(`/requests/${caseNumber}/records`)

  await expect(page.getByRole('heading', { name: 'Ready to discard' })).toBeVisible()
  await expect(page.getByText(/5 unreviewed records the AI calls non-responsive with at least 90%/)).toBeVisible()
  await page.getByRole('button', { name: 'Start elusion check' }).click()

  await expect(page.getByText('Elusion check: 5 records ready to discard')).toBeVisible()
  const judgments = page.getByRole('list', { name: 'Sampled records' }).getByRole('combobox')
  await expect(judgments).toHaveCount(3)
  await page.screenshot({
    path: path.resolve('../.verification/reviewer-elusion-sample.png'),
    fullPage: false,
  })
  for (const judgment of await judgments.all()) {
    await judgment.selectOption('non_responsive')
  }
  await page.getByRole('button', { name: 'Record judgments' }).click()

  await expect(page.getByText('Applied: 5 records marked non-responsive in your name.')).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/reviewer-elusion-signed-off.png'),
    fullPage: false,
  })
})
