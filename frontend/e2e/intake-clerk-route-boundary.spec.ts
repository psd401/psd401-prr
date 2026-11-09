import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

test('intake-clerk-route-boundary', async ({ page }) => {
  await login(page, 'e2e-intake', 'e2e-intake-local-only')
  await expect(page.getByRole('heading', { name: 'Staff intake' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Inbox' })).toHaveCount(0)
  await expect(page.getByRole('link', { name: /Inbox/ })).toHaveCount(0)

  const protectedRequests: string[] = []
  page.on('request', (request) => {
    const url = request.url()
    if ([
      '/api/staff/work-items/',
      '/api/staff/correspondence/',
      '/api/staff/requests/',
      '/api/staff/requesters/',
      '/api/staff/google-role-mappings/',
      '/api/staff/integration-identities/',
    ].some((path) => url.includes(path))) protectedRequests.push(url)
  })
  for (const path of [
    '/inbox',
    '/cases',
    '/requests/PRR-E2E-2000',
    '/configuration/identity',
  ]) {
    await page.goto(path)
    await expect(page.getByRole('heading', { name: 'Not authorized' })).toBeVisible()
  }
  await expect.poll(() => protectedRequests).toEqual([])

  await page.goto('/staff-intake')
  await page.getByRole('link', { name: 'Settings' }).click()
  await page.getByRole('link', { name: 'Agent access' }).click()
  await expect(page).toHaveURL(/\/configuration\/agent-access$/)
  await expect(page.getByRole('heading', { name: 'Agent access' })).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/intake-clerk-route-boundary.png'),
    fullPage: false,
  })
})
