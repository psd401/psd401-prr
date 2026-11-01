import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

test('complete-reviewer-journey', async ({ page }) => {
  await login(page, 'e2e-reviewer', 'e2e-reviewer-local-only')
  await expect(page.getByRole('heading', { name: 'Review' })).toBeVisible()
  await page.getByRole('link', { name: /Open Review record/ }).click()
  await expect(page.getByRole('heading', { name: 'Work context' })).toBeVisible()
  await page.getByRole('link', { name: 'Open source context' }).click()
  await expect(page.getByRole('dialog', { name: /synthetic-review-record\.pdf/ })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByText('Restricted', { exact: true })).toBeVisible()
  await expect(page.getByText('Synthetic Requester')).toHaveCount(0)
  await expect(page.getByRole('link', { name: 'Messages' })).toHaveCount(0)

  await page.getByRole('button', { name: 'Unreviewed' }).click()
  await page.getByRole('row').filter({ hasText: 'synthetic-uat-review-record.pdf' }).click()
  const dialog = page.getByRole('dialog', { name: /synthetic-uat-review-record\.pdf/ })
  await expect(dialog.getByLabel('Review outcome')).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Confirm review outcome' })).toBeVisible()
  await expect(dialog.getByRole('heading', { name: 'Messages' })).toHaveCount(0)
  await dialog.getByLabel('Review outcome').selectOption('responsive')
  await dialog.getByRole('button', { name: 'Confirm review outcome' }).click()
  await expect(dialog.getByText('Responsive', { exact: true }).first()).toBeVisible()
  await page.goBack()
  await expect(dialog).toHaveCount(0)
  await page.goForward()
  await expect(dialog).toBeVisible()

  await page.screenshot({
    path: path.resolve('../.verification/complete-reviewer-journey.png'),
    fullPage: false,
  })
})
