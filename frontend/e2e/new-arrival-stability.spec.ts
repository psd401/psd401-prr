import path from 'node:path'

import { expect, test } from '@playwright/test'

import {
  fixtureRequestNumber,
  login,
  openFixtureRequest,
} from './message-workspace.helpers.js'

test('new-arrival-stability', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)
  await openFixtureRequest(page)

  const selectedSubject = 'Synthetic outbound message 1999'
  await page.getByRole('button', { name: `Open ${selectedSubject}` }).click()
  const selectedDetail = page.getByLabel(selectedSubject, { exact: true })
  await expect(
    selectedDetail.getByText('Synthetic casework message 1999.', { exact: false }),
  ).toBeVisible()

  const arrivalSubject = 'Synthetic newly arrived workspace message'
  await page.route(
    (url) => (
      url.pathname === '/api/staff/correspondence/'
      && url.searchParams.get('request') === fixtureRequestNumber
      && !url.searchParams.has('cursor')
    ),
    async (route) => {
      const upstream = await route.fetch()
      const payload = await upstream.json() as {
        count: number
        results: Array<Record<string, unknown>>
      }
      await route.fulfill({
        response: upstream,
        json: {
          ...payload,
          count: payload.count + 1,
          results: [{
            ...payload.results[0],
            id: '00000000-0000-4000-8000-000000000001',
            subject: arrivalSubject,
            snippet: 'Synthetic staged arrival.',
            created_at: '2026-08-23T20:00:00Z',
          }, ...payload.results.slice(0, 49)],
        },
      })
    },
  )

  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  const banner = page.getByRole('button', { name: 'Show 1 new message' })
  await expect(banner).toBeVisible()
  await expect(page.getByRole('button', { name: `Open ${arrivalSubject}` })).toHaveCount(0)
  await expect(
    selectedDetail.getByText('Synthetic casework message 1999.', { exact: false }),
  ).toBeVisible()
  await expect(page).toHaveURL(/message=/)

  await banner.click()
  await expect(page.getByRole('button', { name: `Open ${arrivalSubject}` })).toBeVisible()
  await expect(
    selectedDetail.getByText('Synthetic casework message 1999.', { exact: false }),
  ).toBeVisible()

  await page.screenshot({
    path: path.resolve('../.verification/new-arrival-stability.png'),
    fullPage: false,
  })
})
