import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

/** The isolated case whose uploaded mail shares one sender domain. */
const bulkCase = 'PRR-E2E-0009'

// The criterion this flow exists for: every record a filter matches is
// decided in one confirmation, after the reviewer sees the server's count.
test('reviewer-bulk-review-by-filter', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)

  await page.goto(`/requests/${bulkCase}/records`)
  await page.getByLabel('Sender domain').selectOption('bulk.example.org')
  const rows = page.getByRole('row').filter({ hasText: 'synthetic-bulk-filter' })
  await expect(rows).toHaveCount(3)

  await page.getByRole('button', { name: 'Select all 3 matching' }).click()
  await page.getByLabel('Bulk review outcome').selectOption('non_responsive')
  await page.getByRole('button', { name: 'Review all matching' }).click()
  const confirm = page.getByRole('dialog', { name: 'Record this review outcome' })
  await expect(confirm.getByText('of the 3 records matching this filter')).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/reviewer-bulk-review-by-filter-confirm.png'),
    fullPage: false,
  })
  await confirm.getByRole('button', { name: 'Record 3 outcomes' }).click()
  await expect(page.getByText('Recorded 3 review outcomes.')).toBeVisible()

  await page.getByRole('button', { name: 'Non-responsive', exact: true }).click()
  await expect(rows).toHaveCount(3)
  await page.screenshot({
    path: path.resolve('../.verification/reviewer-bulk-review-by-filter-result.png'),
    fullPage: false,
  })
})
