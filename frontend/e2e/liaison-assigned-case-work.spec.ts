import path from 'node:path'

import { expect, test } from '@playwright/test'

import { fixtureRequestNumber, login } from './message-workspace.helpers.js'

test('liaison-assigned-case-work', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page, 'e2e-liaison', 'e2e-liaison-local-only')
  await page.goto(`/requests/${fixtureRequestNumber}/collection`)

  await expect(page).toHaveURL(
    new RegExp(`/requests/${fixtureRequestNumber}/collection$`),
  )
  await expect(page.getByRole('heading', {
    name: 'Collection',
    level: 2,
    exact: true,
  })).toBeFocused()
  await expect(page.getByText('Restricted', { exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Messages' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Records' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Add search assignment' })).toHaveCount(0)

  const taskState = page.getByRole('combobox').filter({ has: page.locator('option[value="pending"]') }).last()
  await expect(taskState).toHaveValue('pending')
  await taskState.selectOption('running')
  await expect(taskState).toHaveValue('running')
  await expect(page.getByText('Task updated.')).toBeVisible()

  await page.getByRole('cell', { name: 'e2e-liaison' }).click()
  await expect(page).toHaveURL(/\?task=\d+$/)
  await expect(page.getByRole('dialog', { name: /Task — request/ })).toBeVisible()
  await page.goBack()
  await expect(page.getByRole('dialog', { name: /Task — request/ })).toHaveCount(0)
  await page.goForward()
  await expect(page.getByRole('dialog', { name: /Task — request/ })).toBeVisible()

  await page.screenshot({
    path: path.resolve('../.verification/liaison-assigned-case-work.png'),
    fullPage: false,
  })
})
