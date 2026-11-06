import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

/**
 * Sorting the case table, in the running application.
 *
 * The ordering itself is pinned by the API tests, which assert row order
 * against real data. What only the browser can show is the loop an officer
 * actually performs: click a column, have the server reorder the page, read
 * the sort state back off the header, keep it through a reload, and open a
 * case from the row. Requester is the column chosen on purpose -- it is the
 * one the server refuses to order by for any role that may not see requester
 * identity, so an officer being able to use it is half of that boundary.
 */
test('officer-case-table-sort', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)

  await page.goto('/inbox?view=cases')
  await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: /Case #/ })).toBeVisible()

  const ordered = page.waitForResponse((response) => (
    response.url().includes('/api/staff/requests/')
    && response.url().includes('ordering=requester')
  ))
  await page.getByRole('button', { name: /Requester/ }).click()
  expect((await ordered).ok()).toBe(true)
  await expect(page.getByRole('columnheader', { name: /Requester/ }))
    .toHaveAttribute('aria-sort', 'ascending')
  await expect(page).toHaveURL(/sort=requester/)

  const names = await page.getByRole('row').locator('td:nth-child(3)').allInnerTexts()
  const visible = names.filter((name) => name.trim() && name.trim() !== '—')
  expect(visible.length).toBeGreaterThan(0)
  expect([...visible].sort((a, b) => a.localeCompare(b))).toEqual(visible)

  await page.screenshot({
    path: path.resolve('../.verification/officer-case-table-sort.png'),
    fullPage: false,
  })

  // The sort is in the URL, so it survives a reload and a shared link.
  await page.reload()
  await expect(page.getByRole('columnheader', { name: /Requester/ }))
    .toHaveAttribute('aria-sort', 'ascending')

  const caseLink = page.getByRole('link', { name: /^Open case / }).first()
  const caseNumber = (await caseLink.innerText()).trim()
  await caseLink.click()
  await expect(page).toHaveURL(new RegExp(`/requests/${caseNumber}`))
  await expect(page.getByRole('region', { name: 'Case summary' })).toContainText(caseNumber)
})

/**
 * The triage view's unread marking, and the action that clears it.
 *
 * New requester mail is marked the way unread mail is marked -- a dot, a bold
 * row -- and the action that says "seen" lives in the row's own menu. It used
 * to be a badge beside a full-width button in a column of their own, which
 * cost the width that Assignee and the due date needed.
 *
 * The POST and the refetch that follows it are stubbed rather than allowed
 * through. "Caught up" disposes every waiting requester-email row on the
 * case, and those same rows are what `officer-message-disposition` and
 * `complete-officer-case-journey` triage later in this suite. The endpoint's
 * own behaviour is pinned by the API tests; what only the browser can show is
 * that the control in the table reaches it and that the row updates when it
 * answers.
 */
test('officer-case-table-caught-up', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)

  await page.goto('/inbox?view=cases_open')
  await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()
  // The column is gone; the marking moved onto the row itself.
  await expect(page.getByRole('columnheader', { name: 'New activity' }))
    .toHaveCount(0)

  // Put a flagged row on screen, whatever the fixture's current state is, and
  // keep this flow from changing that state.
  let caughtUp = false
  await page.route('**/api/staff/requests/?queue=inbox', async (route) => {
    const upstream = await route.fetch()
    const body = await upstream.json()
    const rows = Array.isArray(body) ? body : body.results
    if (rows?.length) {
      rows[0].inbox_needs_attention = caughtUp ? 0 : 2
      rows[0].latest_inbound_at = '2026-09-08T17:52:00Z'
    }
    await route.fulfill({ response: upstream, json: body })
  })
  await page.route('**/inbox-handled/', async (route) => {
    caughtUp = true
    await route.fulfill({ status: 200, json: { handled: 2 } })
  })
  await page.reload()

  const flagged = page.getByRole('row').filter({ hasText: '2 new requester messages' })
  await expect(flagged).toHaveCount(1)
  const number = (await flagged.getByRole('link', { name: /^Open case / })
    .innerText()).trim()

  await page.screenshot({
    path: path.resolve('../.verification/officer-case-table-caught-up.png'),
    fullPage: false,
  })

  await page.getByRole('button', { name: `Actions for case ${number}` }).click()
  await page.screenshot({
    path: path.resolve('../.verification/officer-case-table-row-menu.png'),
    fullPage: false,
  })
  await page.getByRole('menuitem', { name: 'Mark caught up' }).click()
  await expect(page.getByText(`${number} marked caught up.`)).toBeVisible()
  await expect(page.getByRole('row').filter({ hasText: '2 new requester messages' }))
    .toHaveCount(0)
})

/**
 * The whole row opens the case.
 *
 * A case number is a small thing to hit forty times a day. The click still has
 * to belong to whatever is under it when that is a control: selecting a case
 * for a batch must not navigate away from the list the batch is being built
 * from.
 */
test('officer-case-table-row-opens-the-case', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)

  await page.goto('/inbox?view=cases_open')
  await expect(page.getByRole('columnheader', { name: /Case #/ })).toBeVisible()
  const row = page.getByRole('row').nth(1)
  const number = (await row.getByRole('link', { name: /^Open case / })
    .innerText()).trim()

  // A checkbox owns its own click, and the list stays put.
  await row.getByRole('checkbox').first().click()
  await expect(page).toHaveURL(/\/inbox/)
  await expect(page.getByText(/1 case selected/)).toBeVisible()

  await page.screenshot({
    path: path.resolve('../.verification/officer-case-table-row-selected.png'),
    fullPage: false,
  })

  // The Status cell belongs to no control, so it opens the case.
  await row.getByRole('cell').nth(4).click()
  await expect(page).toHaveURL(new RegExp(`/requests/${number}`))
  await expect(page.getByRole('region', { name: 'Case summary' })).toContainText(number)
})

/** Keyboard parity for the same thing: Enter on a focused row opens it. */
test('officer-case-table-row-opens-from-the-keyboard', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)

  await page.goto('/inbox?view=cases_open')
  await expect(page.getByRole('columnheader', { name: /Case #/ })).toBeVisible()
  const row = page.getByRole('row').nth(1)
  const number = (await row.getByRole('link', { name: /^Open case / })
    .innerText()).trim()

  await row.focus()
  await page.screenshot({
    path: path.resolve('../.verification/officer-case-table-row-focus.png'),
    fullPage: false,
  })
  await page.keyboard.press('Enter')

  await expect(page).toHaveURL(new RegExp(`/requests/${number}`))
})

/**
 * Marking the whole selection caught up.
 *
 * One call per case, so each keeps its own audit entry: a single bulk call
 * that half-succeeded would leave no record of which half.
 */
test('officer-case-table-bulk-caught-up', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)

  const posted: string[] = []
  await page.route('**/inbox-handled/', async (route) => {
    posted.push(new URL(route.request().url()).pathname)
    await route.fulfill({ status: 200, json: { handled: 1 } })
  })

  await page.goto('/inbox?view=cases_open')
  await expect(page.getByRole('columnheader', { name: /Case #/ })).toBeVisible()
  await page
    .getByRole('checkbox', {
      name: 'Select every case on this page for a batch notice',
    })
    .click()
  await page.screenshot({
    path: path.resolve('../.verification/officer-case-table-selection-bar.png'),
    fullPage: false,
  })
  await page.getByRole('button', { name: 'Mark caught up' }).click()

  await expect
    .poll(() => posted.length, { timeout: 10_000 })
    .toBeGreaterThan(0)
  expect(posted.every((pathname) => pathname.endsWith('/inbox-handled/')))
    .toBe(true)
})
