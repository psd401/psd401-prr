import path from 'node:path'

import { expect, test } from '@playwright/test'

const fixtureRequestNumber = 'PRR-E2E-2000'
const fixtureDescription = (
  'All district correspondence about the synthetic Harborview planning '
  + 'project from January through March 2026.'
)

test('staff-login-open-case', async ({ page }) => {
  // Production gets this CSRF cookie from Django's SPA shell. Vite serves the
  // E2E shell, so visit the same-host Django shell once before the UI flow.
  await page.goto('http://127.0.0.1:8000/')
  await page.goto('/')

  await expect(page.getByRole('heading', { name: 'Public Records' })).toBeVisible()
  await page.getByLabel('Username').fill('e2e-officer')
  await page.getByLabel('Password').fill('e2e-local-only')
  await page.getByRole('button', { name: 'Sign in' }).click()

  // Wait for the authenticated shell before navigating: a goto issued while
  // the session is still being established lands back on the sign-in screen.
  await expect(page.getByRole('heading', { name: 'Today' })).toBeVisible()
  await page.goto('/inbox?view=cases')
  await page
    .getByRole('link', { name: new RegExp(`^Open case ${fixtureRequestNumber}`) })
    .first()
    .click()

  await expect(page).toHaveURL(
    new RegExp(`/requests/${fixtureRequestNumber}/intake$`),
  )
  const requestHeader = page.locator('header').filter({
    has: page.getByRole('heading', { name: fixtureRequestNumber }),
  })
  await expect(requestHeader).toBeVisible()
  await expect(
    requestHeader.getByText(/Awaiting Clarification|Gathering Records/),
  ).toBeVisible()
  await expect(page.getByText(fixtureDescription, { exact: true })).toBeVisible()

  await page.screenshot({
    path: path.resolve('../.verification/staff-login-open-case.png'),
    fullPage: false,
  })
})
