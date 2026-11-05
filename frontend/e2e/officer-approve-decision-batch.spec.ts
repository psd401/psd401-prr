import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

// The criterion this flow exists for: a batch an agent proposed waits in the
// Decisions queue with its evidence one click away, and one click approves it
// -- running the same review the officer would have done by hand.
test('officer-approve-decision-batch', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)
  await page.goto('/decisions')

  await expect(page.getByRole('heading', { name: 'Decisions' })).toBeVisible()
  const card = page.locator('section, article, div').filter({ hasText: 'PRR-E2E-0016' }).filter({
    has: page.getByRole('button', { name: 'Approve' }),
  }).last()
  await expect(card.getByText('Mark 2 records non-responsive.')).toBeVisible()
  await card.getByText('Evidence').click()
  await expect(card.getByText(/district newsletter sent to every staff mailbox/)).toBeVisible()
  await expect(card.getByText('synthetic-decision-newsletter-0.eml — now unreviewed')).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/officer-approve-decision-batch-queue.png'),
    fullPage: false,
  })

  await card.getByRole('button', { name: 'Approve' }).click()
  await expect(page.getByText('Mark 2 records non-responsive.')).toHaveCount(0)

  await page.getByLabel('Show').selectOption('approved')
  await expect(page.getByText('Approved by e2e-officer')).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/officer-approve-decision-batch-approved.png'),
    fullPage: false,
  })

  // The approval ran the ordinary review: the records are non-responsive.
  await page.goto('/requests/PRR-E2E-0016/records')
  await page.getByRole('button', { name: 'Non-responsive', exact: true }).click()
  await expect(page.getByText('synthetic-decision-newsletter-0.eml')).toBeVisible()
})
