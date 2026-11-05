import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

const caseNumber = 'PRR-E2E-0015'

// The criterion this flow exists for: one "Build package" action turns every
// release-ready record into a prepared package -- installment, exemption log,
// and package -- and the officer previews it before the human-only release.
test('officer-one-step-release', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)
  await page.goto(`/requests/${caseNumber}/releases`)

  const steps = page.getByRole('list', { name: 'Release steps' })
  await expect(steps.getByText('3 records ready to release.', { exact: false })).toBeVisible()
  // No fee applies, so the fee panels are folded away.
  await expect(page.getByText('Fees and deposits — no fee applies')).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/officer-one-step-release-ready.png'),
    fullPage: false,
  })

  await steps.getByRole('button', { name: 'Build package' }).click()

  const preview = page.getByRole('list', { name: 'Records in this package' })
  await expect(preview.getByText('synthetic-release-agenda-0.pdf')).toBeVisible()
  await expect(preview.getByText(/synthetic-release-agenda-2\.pdf — withheld/)).toBeVisible()
  await expect(steps.getByText('The exemption log is included.', { exact: false })).toBeVisible()
  await expect(steps.getByRole('button', { name: 'Build package' })).toHaveCount(0)

  // Release and send stays a separate, confirmed human action.
  await steps.getByRole('button', { name: 'Release and send' }).click()
  await expect(page.getByRole('dialog', { name: 'Release installment' })).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/officer-one-step-release-preview.png'),
    fullPage: false,
  })
})
