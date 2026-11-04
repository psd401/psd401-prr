import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

// The criterion this flow exists for: a Records Officer maintains the district
// context the intent advisory reads -- who held a role and when, which senders
// are noise, how a system is searched -- entirely in the app.
test('officer-knowledge-pack-edit', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)

  await page.goto('/configuration/knowledge')
  await expect(page.getByRole('heading', { name: 'Knowledge', exact: true })).toBeVisible()

  // Close the seeded role account's open end.
  const roles = page.getByRole('table', { name: 'Role accounts' })
  await roles.getByText('Principal, Fixture Elementary').click()
  let dialog = page.getByRole('dialog')
  await dialog.getByLabel('Until').fill('2026-06-30')
  await page.screenshot({
    path: path.resolve('../.verification/officer-knowledge-pack-edit.png'),
    fullPage: false,
  })
  await dialog.getByRole('button', { name: 'Save' }).click()
  await expect(page.getByText('Role account updated.')).toBeVisible()
  await expect(roles.getByRole('row').filter({ hasText: 'Principal, Fixture Elementary' }))
    .toContainText('2026-06-30')

  await page.getByRole('button', { name: 'Add noise sender' }).click()
  dialog = page.getByRole('dialog')
  await dialog.getByLabel(/^Sender/).fill('@News.Example.org')
  await dialog.getByRole('button', { name: 'Save' }).click()
  await expect(page.getByText('Noise sender added.')).toBeVisible()
  await expect(page.getByRole('table', { name: 'Noise senders' }))
    .toContainText('news.example.org')

  await page.getByRole('button', { name: 'Add search notes' }).click()
  dialog = page.getByRole('dialog')
  await dialog.getByLabel('System').selectOption({ label: 'Fixture shared mailbox' })
  await dialog.getByLabel('Search notes').fill('Search the shared office mailbox before any staff account.')
  await dialog.getByRole('button', { name: 'Save' }).click()
  await expect(page.getByText('Search notes added.')).toBeVisible()
  await expect(page.getByRole('table', { name: 'System search notes' }))
    .toContainText('Fixture shared mailbox')

  await page.screenshot({
    path: path.resolve('../.verification/officer-knowledge-pack-saved.png'),
    fullPage: true,
  })
})
