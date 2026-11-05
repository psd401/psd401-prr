import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

const caseNumber = 'PRR-E2E-0014'

// The criterion this flow exists for: when a narrower scope is proposed for a
// case that already collected a broad one, the officer sees the difference in
// totals, gives a reason, and re-scoping collects the new scope and supersedes
// the old collection's unreviewed records.
test('officer-rescope', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)
  await page.goto(`/requests/${caseNumber}/collection`)

  await expect(page.getByText(/current collection has 2,340 messages; the proposed scope has 412 messages/)).toBeVisible()
  const approve = page.getByRole('button', { name: 'Approve re-scope' })
  await expect(approve).toBeDisabled()
  await page.getByLabel(/Why change the scope/).fill('The broad search was mostly the district newsletter.')
  await approve.scrollIntoViewIfNeeded()
  await page.screenshot({
    path: path.resolve('../.verification/officer-rescope-diff.png'),
    fullPage: false,
  })
  await approve.click()
  await expect(page.getByRole('note').filter({ hasText: /Collecting this scope/ })).toBeVisible()

  await page.goto(`/requests/${caseNumber}/records`)
  // Superseded records leave review on purpose; they sit one filter away.
  await page.getByRole('button', { name: 'Superseded', exact: true }).click()
  await expect(page.getByText('synthetic-rescope-mail-0.eml')).toBeVisible()
  await expect(page.getByText('synthetic-rescope-mail-1.eml')).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/officer-rescope-superseded.png'),
    fullPage: false,
  })
})
