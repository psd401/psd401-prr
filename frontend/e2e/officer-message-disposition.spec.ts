import path from 'node:path'

import { expect, test } from '@playwright/test'

import {
  login,
  openFixtureRequest,
  primaryInboundSubject,
  searchMessages,
} from './message-workspace.helpers.js'

test('officer-message-disposition', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)
  await expect(page.getByRole('heading', { name: 'Today' })).toBeVisible()
  // Requester replies are triaged inside their request's Messages tab; the
  // Requests list flags a request that has new email.
  await page.getByRole('navigation', { name: 'Primary navigation' })
    .getByRole('link', { name: 'Requests' }).click()
  await expect(page.getByRole('heading', { name: 'Requests' })).toBeVisible()
  await page.getByRole('button', { name: /^New email/ }).click()
  await expect(page.getByRole('row').nth(1)).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/officer-requests-new-email.png'),
    fullPage: false,
  })

  await openFixtureRequest(page)
  await searchMessages(page, primaryInboundSubject)
  await page.getByRole('button', { name: `Open ${primaryInboundSubject}` }).click()

  await expect(page.getByText('Personal: Unread')).toBeVisible()
  await expect(page.getByText('Team: Needs attention')).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/officer-message-needs-attention.png'),
    fullPage: false,
  })

  await page.getByRole('button', { name: 'Mark read' }).click()
  await expect(page.getByText('Personal: Read')).toBeVisible()
  await page.getByRole('button', { name: 'No action required' }).click()
  const dialog = page.getByRole('dialog', { name: 'Record no action required' })
  await dialog.getByLabel('No-action category').selectOption('informational_only')
  await dialog.getByLabel('Why no team action is required').fill(
    'Synthetic informational reply requires no additional team action.',
  )
  await dialog.getByRole('button', { name: 'Record no action' }).click()
  await expect(page.getByText('Team: No action required')).toBeVisible()

  await page.getByRole('button', { name: 'Sign out' }).click()
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
  await login(page, 'e2e-observer', 'e2e-observer-local-only')
  await openFixtureRequest(page)
  await searchMessages(page, primaryInboundSubject)
  await page.getByRole('button', { name: `Open ${primaryInboundSubject}` }).click()

  await expect(page.getByText('Personal: Unread')).toBeVisible()
  await expect(page.getByText('Team: No action required')).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/officer-message-no-action.png'),
    fullPage: false,
  })
})
