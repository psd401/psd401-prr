import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

// The criterion this flow exists for: a case the system closed on its own
// says so on the case, and one click undoes it -- the case is open again as
// received, with the closure still on the timeline.
test('officer-intake-auto-apply-undo', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)
  await page.goto('/requests/PRR-E2E-0017')

  const card = page.locator('section, article, div').filter({
    has: page.getByRole('heading', { name: 'Closed automatically' }),
  }).last()
  await expect(card.getByText('Spam', { exact: true })).toBeVisible()
  await expect(card.getByText('99% confidence')).toBeVisible()
  await expect(card.getByText(/Nothing was sent to the sender/)).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/officer-intake-auto-apply-undo-closed.png'),
    fullPage: false,
  })

  await card.getByRole('button', { name: 'Undo' }).click()
  await expect(page.getByRole('heading', { name: 'Closed automatically' })).toHaveCount(0)
  await expect(page.getByText(/This request is closed\./)).toHaveCount(0)
  await page.screenshot({
    path: path.resolve('../.verification/officer-intake-auto-apply-undo-reopened.png'),
    fullPage: false,
  })

  // The running log keeps both: the system's closure and the officer's undo.
  await expect(page.getByText(/Closed automatically: the intake classification labelled this spam/)).toBeVisible()
  await expect(page.getByText('Undid the automatic closure as spam.')).toBeVisible()

  // The officer then decides it themselves, through the ordinary closure. It
  // also returns the shared fixture to a closed case, so this open email does
  // not become the newest row in the inbox flows that run after this one.
  await page.getByRole('button', { name: 'Close as non-request' }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('Closure reason').fill('Prize spam; asks for no district records.')
  await dialog.getByRole('button', { name: 'Close as non-request' }).click()
  await expect(page.getByText(/This request is closed\./)).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Closed automatically' })).toHaveCount(0)
})
