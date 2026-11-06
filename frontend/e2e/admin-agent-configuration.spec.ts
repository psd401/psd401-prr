import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

test('admin-agent-configuration', async ({ page }) => {
  await login(page, 'e2e-admin', 'e2e-admin-local-only')
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
  await page.getByRole('link', { name: 'Agent access' }).click()
  await expect.poll(() => new URL(page.url()).pathname).toBe('/configuration/agent-access')
  await expect(page.getByRole('heading', { name: 'Agent access' })).toBeVisible()
  await page.goBack()
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()

  await page.getByRole('link', { name: 'Identity and integrations' }).click()
  await expect(page).toHaveURL(/\/configuration\/identity$/)
  await expect(page.getByRole('heading', { name: 'Staff identity' })).toBeVisible()
  await expect(page.getByRole('button', { name: /No action required/i })).toHaveCount(0)
  await page.goBack()
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()

  await page.getByRole('link', { name: 'Message operations' }).click()
  await expect(page).toHaveURL(/\/configuration\/correspondence$/)
  await expect(page.getByRole('heading', { name: 'Message operations' })).toBeVisible()
  await expect(page.getByText('Delivery readiness')).toBeVisible()
  await expect(page.getByRole('button', { name: /No action required/i })).toHaveCount(0)
  await page.goBack()
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()

  await page.goto('/agent-access')
  await expect.poll(() => new URL(page.url()).pathname).toBe('/configuration/agent-access')
  await expect(page.getByRole('heading', { name: 'Agent access' })).toBeVisible()
  await page.goBack()
  await expect(page).toHaveURL(/\/configuration$/)
  await page.screenshot({
    path: path.resolve('../.verification/admin-agent-configuration.png'),
    fullPage: false,
  })
})
