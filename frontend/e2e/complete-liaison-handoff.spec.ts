import path from 'node:path'

import { expect, test } from '@playwright/test'

import {
  login,
  openFixtureRequest,
  signOut,
} from './message-workspace.helpers.js'

test('complete-liaison-handoff', async ({ page }) => {
  await login(page, 'e2e-liaison', 'e2e-liaison-local-only')
  // Login lands on Today; these queues live on the Inbox.
  await expect(page.getByRole('heading', { name: 'Today' })).toBeVisible()
  await page.goto('/inbox')
  await page.getByRole('link', { name: /Open Complete record search/ }).click()
  await expect(page.getByRole('heading', { name: 'Work context' })).toBeVisible()
  await page.getByRole('link', { name: 'Open source context' }).click()
  await expect(page.getByRole('dialog', { name: /Task — request/ })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByText('Restricted', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Add search assignment' })).toHaveCount(0)

  const taskState = page.getByRole('combobox').filter({
    has: page.locator('option[value="pending"]'),
  }).last()
  await taskState.selectOption('running')
  await expect(taskState).toHaveValue('running')
  await taskState.selectOption('pending')
  await expect(taskState).toHaveValue('pending')

  await page.getByRole('cell', { name: 'e2e-liaison' }).click()
  const taskDialog = page.getByRole('dialog', { name: /Task — request/ })
  await expect(taskDialog).toBeVisible()
  await expect(taskDialog.getByRole('button', { name: /Exclude camera/ })).toHaveCount(0)
  await page.keyboard.press('Escape')

  await page.getByRole('link', { name: 'Messages', exact: true }).click()
  await page.getByText('Filters and sort').click()
  await page.getByLabel('Sort order').selectOption('oldest')
  await page.getByRole('button', { name: 'Open Synthetic outbound message 0001' }).click()
  await page.getByRole('button', { name: 'Edit draft' }).click()
  const edit = page.getByRole('dialog', { name: 'Edit correspondence draft' })
  await edit.getByLabel('Subject').fill('Synthetic liaison handoff')
  await edit.getByLabel('Message').fill(
    'Synthetic liaison-prepared draft retained for Records Officer review and approval.',
  )
  await edit.getByRole('button', { name: 'Save draft' }).click()
  await expect(page.getByRole('button', { name: 'Approve and send' })).toHaveCount(0)

  await signOut(page)
  await login(page)
  await openFixtureRequest(page)
  await page.getByText('Filters and sort').click()
  await page.getByLabel('Sort order').selectOption('oldest')
  await page.getByRole('button', { name: 'Open Synthetic liaison handoff' }).click()
  await expect(page.getByText(
    'Synthetic liaison-prepared draft retained for Records Officer review and approval.',
  )).toBeVisible()
  await expect(page.getByRole('button', { name: 'Approve and send' })).toBeVisible()

  await page.screenshot({
    path: path.resolve('../.verification/complete-liaison-handoff.png'),
    fullPage: false,
  })
})
