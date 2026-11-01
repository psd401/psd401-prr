import path from 'node:path'

import { expect, test } from '@playwright/test'

import { fixtureRequestNumber, login } from './message-workspace.helpers.js'

/**
 * The Phase 5 fee surfaces, in the app.
 *
 * Like the Phase 4 letter flow, this stops before it mutates anything. What
 * the fee estimate says, what the ten percent cap refuses, and what a deposit
 * gate actually blocks are pinned by the service, API and component tests.
 * What only the running application can show is that an officer can reach the
 * estimate with its deposit line, that the adopted schedule is entered rather
 * than assumed, and that a deposit gate and a waiver floor are reachable from
 * the Releases tab. Creating fee evidence here would leave append-only rows on
 * a fixture case the other flows count.
 */
test('officer-fee-estimate-and-deposit', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)

  await page.goto(`/requests/${fixtureRequestNumber}/messages`)
  await expect(page.getByRole('heading', { name: 'Messages' })).toBeVisible()
  await page.getByRole('button', { name: 'Compose message' }).click()

  const dialog = page.getByRole('dialog', { name: 'Compose correspondence' })
  await expect(dialog).toBeVisible()
  await dialog.getByLabel('Message type').selectOption('fee_estimate')

  await dialog.getByLabel('Estimated copying charge').fill('367.25')
  await dialog.getByLabel('How the charge was calculated').fill(
    '29,380 electronic files at five cents per four files under the adopted '
    + 'schedule, plus $2.00 for the transfer media.',
  )
  await dialog.getByLabel('Deposit requested (optional)').fill('36.72')
  await expect(dialog.getByText(/ten percent of the estimate/i)).toBeVisible()

  await page.screenshot({
    path: path.resolve('../.verification/officer-fee-estimate-compose.png'),
    fullPage: false,
  })

  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()

  await page.goto(`/requests/${fixtureRequestNumber}/releases`)
  await expect(page.getByRole('heading', { name: 'Fee waiver floor' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Deposit gates' })).toBeVisible()

  await page.getByRole('button', { name: 'Require a deposit' }).click()
  const gate = page.getByRole('dialog', {
    name: 'Require a deposit before this work continues',
  })
  await expect(gate).toBeVisible()
  await gate.getByLabel('Estimated charge').fill('367.25')
  await gate.getByLabel('Deposit requested').fill('36.72')
  await gate.getByLabel('Why the District is requiring it').fill(
    'Ten percent deposit on the estimated copying charge for this case.',
  )
  await expect(gate.getByText(/nothing closes on its own/i)).toBeVisible()

  await page.screenshot({
    path: path.resolve('../.verification/officer-fee-deposit-gate.png'),
    fullPage: false,
  })

  await page.keyboard.press('Escape')
  await expect(gate).toBeHidden()

  await page.goto('/inbox')
  await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()
  await page.getByRole('button', { name: /^More views/ }).click()
  await expect(
    page.getByRole('button', { name: /Fee unpaid 30 days/ }),
  ).toBeVisible()
})
