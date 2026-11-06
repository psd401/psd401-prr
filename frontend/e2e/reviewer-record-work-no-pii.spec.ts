import path from 'node:path'

import { expect, test } from '@playwright/test'

import { fixtureRequestNumber, login } from './message-workspace.helpers.js'

test('reviewer-record-work-no-pii', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page, 'e2e-reviewer', 'e2e-reviewer-local-only')
  await page.goto(`/requests/${fixtureRequestNumber}/review?view=records`)

  await expect(page).toHaveURL(
    new RegExp(`/requests/${fixtureRequestNumber}/review\\?view=records$`),
  )
  await expect(page.getByRole('heading', { name: 'Review', level: 2, exact: true })).toBeFocused()
  await expect(page.getByText('Restricted', { exact: true })).toBeVisible()
  await expect(page.getByText('synthetic.requester@example.org')).toHaveCount(0)
  await expect(page.getByText('Synthetic Requester')).toHaveCount(0)
  await expect(page.getByRole('link', { name: 'Messages' })).toHaveCount(0)
  await expect(page.getByText('synthetic-review-record.pdf')).toBeVisible()

  await page.getByRole('button', { name: 'Unreviewed' }).click()
  await expect(page).toHaveURL(/record_state=unreviewed/)
  await page.getByRole('row').filter({ hasText: 'synthetic-review-record.pdf' }).click()
  await expect(page).toHaveURL(/item=[0-9a-f-]+/)
  const dialog = page.getByRole('dialog', { name: /synthetic-review-record\.pdf/ })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByLabel('Review outcome')).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Confirm review outcome' })).toBeVisible()
  await expect(dialog.getByRole('heading', { name: 'Messages' })).toHaveCount(0)
  await page.goBack()
  await expect(dialog).toHaveCount(0)
  await expect(page).toHaveURL(/record_state=unreviewed/)
  await page.goForward()
  await expect(dialog).toBeVisible()
  await expect(dialog.getByLabel('Review outcome')).toBeVisible()
  await expect(dialog.getByRole('heading', { name: 'Messages' })).toHaveCount(0)

  await page.screenshot({
    path: path.resolve('../.verification/reviewer-record-work-no-pii.png'),
    fullPage: false,
  })
})
