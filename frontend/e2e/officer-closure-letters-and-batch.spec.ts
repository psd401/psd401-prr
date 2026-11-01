import path from 'node:path'

import { expect, test } from '@playwright/test'

import { fixtureRequestNumber, login } from './message-workspace.helpers.js'

/** A second open case, so the batch selection is a real multi-select. */
const secondCaseNumber = 'PRR-E2E-0007'

/**
 * The Phase 4 letters and the batch selection, in the app.
 *
 * This flow stops at the confirm button on purpose. The batch's behaviour --
 * what it drafts, what it refuses, what it reports per case -- is pinned by
 * the service, API and component tests; what only the running application can
 * show is that the three new officer-only kinds are reachable with their own
 * fields and that selecting several cases produces one drafting dialog. A
 * browser flow that actually created correspondence would leave rows on
 * fixture cases the other flows count, which is a worse trade than stopping
 * one click short.
 */
test('officer-closure-letters-and-batch', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)

  await page.goto(`/requests/${fixtureRequestNumber}/messages`)
  await expect(page.getByRole('heading', { name: 'Messages' })).toBeVisible()
  await page.getByRole('button', { name: 'Compose message' }).click()

  const dialog = page.getByRole('dialog', { name: 'Compose correspondence' })
  await expect(dialog).toBeVisible()
  const kind = dialog.getByLabel('Message type')

  await kind.selectOption('third_party_notice')
  await expect(dialog.getByLabel('Person notified')).toBeVisible()
  await expect(dialog.getByLabel('Their email address')).toBeVisible()
  await expect(dialog.getByLabel('Attorney email (optional)')).toBeVisible()

  await kind.selectOption('nonpayment_closure')
  await expect(dialog.getByLabel('Amount requested')).toBeVisible()
  await expect(dialog.getByLabel('Payment requested on')).toBeVisible()

  await kind.selectOption('completion_closure')
  const summary = dialog.getByLabel('What the District produced')
  await expect(summary).toBeVisible()
  await summary.fill(
    'The District produced every responsive record it located and '
    + 'withheld nothing.',
  )

  await page.screenshot({
    path: path.resolve('../.verification/officer-closure-letter-compose.png'),
    fullPage: false,
  })

  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()

  await page.goto('/inbox?view=cases')
  await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()
  for (const number of [secondCaseNumber, fixtureRequestNumber]) {
    await page
      .getByRole('checkbox', {
        name: `Select case ${number} for a batch notice`,
      })
      .check()
  }
  await expect(page.getByText('2 cases selected.')).toBeVisible()
  await page
    .getByRole('button', { name: 'Draft notices for these cases' })
    .click()

  const batch = page.getByRole('dialog', {
    name: 'Draft one notice for 2 cases',
  })
  await expect(batch).toBeVisible()
  await batch.getByLabel('What the District produced').fill(
    'The District produced every responsive record it located and '
    + 'withheld nothing.',
  )
  await expect(batch.getByRole('button', { name: 'Create drafts' }))
    .toBeEnabled()

  await page.screenshot({
    path: path.resolve('../.verification/officer-batch-closure-drafts.png'),
    fullPage: false,
  })

  // Nothing was created and nothing was sent: drafting is a deliberate click.
  await batch.getByRole('button', { name: 'Cancel' }).click()
  await expect(batch).toBeHidden()
  await page.getByRole('button', { name: 'Clear selection' }).click()
  await expect(page.getByText('cases selected.')).toBeHidden()
})
