import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login, signOut } from './message-workspace.helpers.js'

const evidencePath = (name: string) => path.resolve(
  `../.verification/${name}`,
)

test('role-aware-work-inbox', async ({ page }) => {
  const menu = page.getByRole('navigation', { name: 'Primary navigation' })

  await login(page)
  // The officer lands on Today; every other page is a menu stop.
  await expect(page).toHaveURL(/\/today$/)
  await expect(page.getByRole('heading', { name: 'Today' })).toBeVisible()
  await expect(menu.getByRole('link')).toHaveText([
    'Today', 'Requests', /^Email/, 'New request', 'Settings',
  ])
  await menu.getByRole('link', { name: 'Requests' }).click()
  await expect(page.getByRole('heading', { name: 'Requests' })).toBeVisible()
  await expect(page.getByRole('button', { name: /^5-day response due/ })).toBeVisible()
  await expect(page.getByRole('button', { name: /^New email/ })).toBeVisible()
  await expect(page.getByRole('combobox', { name: 'Case status' })).toHaveValue('open')
  await page.screenshot({ path: evidencePath('officer-shell.png'), fullPage: false })
  await menu.getByRole('link', { name: /^Email/ }).click()
  await expect(page.getByRole('heading', { name: 'Email' })).toBeVisible()
  await expect(page.getByRole('button', { name: /^No matching request/ }))
    .toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByRole('button', { name: /^Couldn't be read/ })).toBeVisible()
  await expect(page.getByRole('button', { name: /^Bounced/ })).toBeVisible()
  await page.waitForTimeout(250)
  await page.screenshot({ path: evidencePath('officer-inbox.png'), fullPage: false })
  // An old Inbox bookmark opens the page that does that job now.
  await page.goto('/inbox?view=unmatched')
  await expect(page).toHaveURL(/\/email\?tab=no_match$/)
  await page.goto('/configuration/identity')
  await expect(page.getByRole('heading', { name: 'Not authorized' })).toBeVisible()
  await signOut(page)

  await login(page, 'e2e-liaison', 'e2e-liaison-local-only')
  await expect(page).toHaveURL(/\/today$/)
  // A liaison works their requests and never sees cross-case email.
  await expect(menu.getByRole('link', { name: /^Email/ })).toHaveCount(0)
  await menu.getByRole('link', { name: 'Requests' }).click()
  await expect(page.getByRole('heading', { name: 'Requests' })).toBeVisible()
  await expect(page.getByRole('button', { name: /^Assigned to me/ })).toBeVisible()
  await expect(page.getByRole('button', { name: /^New email/ })).toHaveCount(0)
  await page.getByRole('searchbox', { name: 'Search cases' }).fill('PRR-E2E-2000')
  await page.getByRole('button', { name: 'Search' }).click()
  await expect(page.getByRole('link', { name: /^Open case PRR-E2E-2000/ })).toBeVisible()
  await page.screenshot({ path: evidencePath('liaison-shell.png'), fullPage: false })
  await signOut(page)

  await login(page, 'e2e-reviewer', 'e2e-reviewer-local-only')
  await expect(page).toHaveURL(/\/today$/)
  await page.goto('/review')
  await expect(page.getByRole('heading', { name: 'Review' })).toBeVisible()
  await expect(page.getByText('Review record')).toBeVisible()
  await page.screenshot({ path: evidencePath('reviewer-shell.png'), fullPage: false })
  await page.getByRole('link', { name: /Open Review record/ }).click()
  await expect(page.getByRole('heading', { name: 'Work context' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'No action required' })).toHaveCount(0)
  await expect(page.getByRole('link', { name: 'Open source context' })).toBeVisible()
  // The reviewer's Requests list has no officer-only filters; the server
  // scopes which requests they see and blanks requester identity.
  await menu.getByRole('link', { name: 'Requests' }).click()
  await expect(page.getByRole('heading', { name: 'Requests' })).toBeVisible()
  await expect(page.getByRole('button', { name: /^All/ })).toBeVisible()
  await expect(page.getByRole('button', { name: /^New email/ })).toHaveCount(0)
  await expect(page.getByRole('button', { name: /^5-day response due/ })).toHaveCount(0)
  await signOut(page)

  await login(page, 'e2e-intake', 'e2e-intake-local-only')
  await expect(page).toHaveURL(/\/staff-intake$/)
  await expect(page.getByRole('heading', { name: 'Staff intake' })).toBeVisible()
  await page.screenshot({ path: evidencePath('intake-shell.png'), fullPage: false })
  await signOut(page)

  await login(page, 'e2e-admin', 'e2e-admin-local-only')
  await expect(page).toHaveURL(/\/configuration$/)
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
  await page.screenshot({ path: evidencePath('admin-shell.png'), fullPage: false })
  await menu.getByRole('link', { name: /^Email/ }).click()
  await expect(page.getByRole('heading', { name: 'Email' })).toBeVisible()
  // An administrator sees bounced letters only, never message content.
  await expect(page.getByRole('button', { name: /^Bounced/ }))
    .toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByRole('button', { name: /^No matching request/ })).toHaveCount(0)
  await page.waitForTimeout(250)
  await page.screenshot({ path: evidencePath('admin-inbox.png'), fullPage: false })
})
