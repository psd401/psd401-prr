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
  // Login lands on Today; these queues live on the Inbox.
  await expect(page.getByRole('heading', { name: 'Today' })).toBeVisible()
  await page.goto('/inbox')

  // The Inbox row carries the dispositions itself: an officer must not have to
  // open a case to clear a message that needs nothing further.
  // Reference and secondary queues live behind "More views" now; the command
  // bar carries only the queues where not looking has a consequence.
  await page.getByRole('button', { name: /More views/ }).click()
  await page.getByRole('button', { name: /^All messages/ }).click()
  const inboxRow = page.getByRole('listitem').first()
  await expect(inboxRow.getByRole('link', { name: /^Open case / })).toBeVisible()
  await expect(inboxRow.getByRole('button', { name: /^Mark handled: / })).toBeVisible()
  // Reasoned, officer-only decisions sit one disclosure away from the routine
  // one-click disposition, and stay reachable without a pointer.
  await inboxRow.getByRole('button', { name: /^More actions: / }).click()
  await expect(inboxRow.getByRole('button', { name: /^No action: / })).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/officer-inbox-row-actions.png'),
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
