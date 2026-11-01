import { expect, type Page } from '@playwright/test'

export const fixtureRequestNumber = 'PRR-E2E-2000'
export const primaryInboundSubject = 'Synthetic inbound message 0000'

export async function login(
  page: Page,
  username = 'e2e-officer',
  password = 'e2e-local-only',
) {
  // Vite proxies the API but Django owns the same-host CSRF cookie.
  await page.goto('http://127.0.0.1:8000/')
  await page.goto('/')
  await page.getByLabel('Username').fill(username)
  await page.getByLabel('Password').fill(password)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible()
}

export async function signOut(page: Page) {
  await page.getByRole('button', { name: 'Sign out' }).click()
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
}

export async function openFixtureRequest(page: Page) {
  await page.goto('/inbox?view=cases')
  await page
    .getByRole('link', { name: new RegExp(`^Open case ${fixtureRequestNumber}`) })
    .first()
    .click()
  await expect(page).toHaveURL(
    new RegExp(`/requests/${fixtureRequestNumber}/overview$`),
  )
  await page.getByRole('link', { name: 'Messages' }).click()
  await expect(page).toHaveURL(
    new RegExp(`/requests/${fixtureRequestNumber}/messages$`),
  )
  await expect(
    page.getByRole('heading', { name: 'Messages' }),
  ).toBeVisible()
  await expect(page.getByText('Showing 1–50 of 2000 messages')).toBeVisible()
}

export async function searchMessages(page: Page, term: string) {
  const search = page.getByLabel('Search message content')
  await search.fill(term)
  await page.getByRole('button', { name: 'Search messages' }).click()
  await expect(page.getByRole('button', { name: `Open ${term}` })).toBeVisible()
}
