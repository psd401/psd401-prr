import path from 'node:path'

import { expect, test } from '@playwright/test'

import { fixtureRequestNumber, login } from './message-workspace.helpers.js'

/**
 * The internal note, written on the case and read from the list.
 *
 * The API tests pin the model: append-only, audited, scoped, and never
 * rendered into anything a requester receives. What only the browser can show
 * is the loop the note exists for -- an officer writes down what they were
 * told on the case page, and the next person scanning the Cases table sees it
 * without opening anything.
 */
test('officer-case-notes', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)

  const note = `Called the transportation custodian; video pulled ${Date.now()}.`

  await page.goto(`/requests/${fixtureRequestNumber}/overview`)
  await expect(page.getByRole('heading', { name: 'Internal notes' }))
    .toBeVisible()
  // The panel says what a note is before anyone writes one: staff-only,
  // permanent, and very possibly a public record in its own right.
  await expect(page.getByText(/never sent to the requester/i)).toBeVisible()
  await expect(page.getByText(/cannot be edited or deleted/i)).toBeVisible()

  await page.getByLabel('Add an internal note').fill(note)
  await page.getByRole('button', { name: 'Add note' }).click()

  await expect(page.getByText('Note added.')).toBeVisible()
  await expect(page.getByText(note)).toBeVisible()
  await expect(page.getByText('e2e-officer').first()).toBeVisible()

  await page.screenshot({
    path: path.resolve('../.verification/officer-case-notes-panel.png'),
    fullPage: false,
  })

  // The composer empties, because the note is now the record rather than a
  // draft somebody might send twice.
  await expect(page.getByLabel('Add an internal note')).toHaveValue('')

  // And the list carries it, so the next person scanning does not have to
  // open the case to find out what happened last.
  await page.goto('/inbox?view=cases')
  await expect(page.getByRole('columnheader', { name: 'Latest note' }))
    .toBeVisible()
  const row = page.getByRole('row').filter({ hasText: fixtureRequestNumber })
  await expect(row.first()).toContainText('Called the transportation custodian')

  await page.screenshot({
    path: path.resolve('../.verification/officer-case-latest-note.png'),
    fullPage: false,
  })
})

/**
 * The table fits the screens it is used on.
 *
 * The previous round put a badge and a full-width button in a column of their
 * own, which pushed Assignee off a laptop. A horizontal scrollbar on the one
 * screen an officer lives in is not a cosmetic problem: the columns that fall
 * off the end are the ones nobody reads.
 */
for (const width of [1440, 1280]) {
  test(`officer-case-table-fits-${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await login(page)

    for (const view of ['cases_open', 'cases']) {
      await page.goto(`/inbox?view=${view}`)
      await expect(page.getByRole('columnheader', { name: /Case #/ }))
        .toBeVisible()
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth
          - document.documentElement.clientWidth,
      )
      expect(overflow, `${view} at ${width}px`).toBeLessThanOrEqual(0)
    }

    await page.screenshot({
      path: path.resolve(`../.verification/officer-all-cases-table-${width}.png`),
      fullPage: false,
    })
  })
}
