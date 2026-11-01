import path from 'node:path'

import { expect, test } from '@playwright/test'

import {
  fixtureRequestNumber,
  login,
  openFixtureRequest,
} from './message-workspace.helpers.js'

test('accessible-live-updates', async ({ page }) => {
  let personalCount = 2
  await page.route('**/api/staff/work-items/counts/', async (route) => {
    await route.fulfill({
      json: {
        personal: personalCount,
        team: 5,
        unassigned: 1,
        waiting: 1,
        critical: 0,
        recently_changed: 5,
        review: 1,
        inbox_needs_attention: 1,
        inbox_new: 1,
        inbox_waiting: 0,
        inbox_handled: 0,
        inbox_no_action: 0,
        inbox_all: 1,
        inbox_unmatched: 0,
        inbox_quarantined: 0,
        inbox_delivery_exceptions: 1,
      },
    })
  })
  await login(page)

  // Focus the chip whose own count is about to change: a live update must not
  // move focus or scroll even when it rewrites the focused control's badge.
  const teamFilter = page.getByRole('button', { name: /Assigned to you/ })
  await teamFilter.focus()
  const initialScroll = await page.evaluate(() => window.scrollY)
  personalCount = 3
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await expect(page.getByRole('status', { name: 'Queue count updates' })).toContainText(
    'Queue counts updated. 3 personal',
  )
  await expect(teamFilter).toBeFocused()
  expect(await page.evaluate(() => window.scrollY)).toBe(initialScroll)

  await openFixtureRequest(page)
  const selected = page.getByRole('button', { name: 'Open Synthetic outbound message 1999' })
  await selected.click()
  await expect(selected).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByRole('region', {
    name: 'Synthetic outbound message 1999 message body',
  })).toContainText('Synthetic casework message 1999.')
  const selectedBox = await selected.boundingBox()
  const selectedScroll = await page.evaluate(() => window.scrollY)
  let arrivalEnabled = false
  await page.route(
    (url) => (
      arrivalEnabled
      && url.pathname === '/api/staff/correspondence/'
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
            id: '00000000-0000-4000-8000-000000000154',
            subject: 'Synthetic accessible arrival',
          }, ...payload.results.slice(0, 49)],
        },
      })
    },
  )
  arrivalEnabled = true
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await expect(page.getByRole('status', { name: 'New message updates' })).toContainText(
    '1 new message is available.',
  )
  await expect(selected).toBeFocused()
  await expect(selected).toHaveAttribute('aria-pressed', 'true')
  expect(await selected.boundingBox()).toEqual(selectedBox)
  expect(await page.evaluate(() => window.scrollY)).toBe(selectedScroll)
  await expect(page.getByRole('button', { name: 'Open Synthetic accessible arrival' })).toHaveCount(0)

  await page.screenshot({
    path: path.resolve('../.verification/accessible-live-updates.png'),
    fullPage: false,
  })
})
