import path from 'node:path'

import { expect, test } from '@playwright/test'

import { fixtureRequestNumber, login } from './message-workspace.helpers.js'

// The criterion this flow exists for: a new case shows a count without a
// click, and the officer sends "your request matched N items, here are
// narrower options" from the Collection tab.
test('officer-scope-conversation', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)
  await page.goto(`/requests/${fixtureRequestNumber}/scope`)
  await expect(page.getByRole('heading', { name: 'Scope', level: 2, exact: true })).toBeFocused()

  // This case already has a collection, so the scoping inputs sit folded
  // under the Scope step (#45); opening it shows them unchanged.
  await page.getByText('Scope: request intent and Vault count').click()
  // The count and the proposed window are already there. Nobody asked for
  // either; the case answered "how big is this?" on arrival.
  await expect(page.getByText('29,380 possible Gmail messages')).toBeVisible()
  await expect(
    page.getByText('run automatically when the request arrived', { exact: false }),
  ).toBeVisible()
  await expect(page.getByText('2025-09-01 to 2026-01-05')).toBeVisible()
  await expect(
    page.getByText('Only the 2025 transportation plan and its revisions.'),
  ).toBeVisible()

  await page
    .getByText('29,380 possible Gmail messages')
    .scrollIntoViewIfNeeded()
  await page.screenshot({
    path: path.resolve('../.verification/officer-scope-count-and-options.png'),
    fullPage: false,
  })

  await page.getByRole('button', { name: 'Write the scope conversation' }).click()
  await expect(page).toHaveURL(
    new RegExp(`/requests/${fixtureRequestNumber}/messages`),
  )

  const dialog = page.getByRole('dialog', { name: 'Compose correspondence' })
  await expect(dialog).toBeVisible()
  // The count came across; the officer does not retype it.
  await expect(dialog.getByLabel('Items the search matched')).toHaveValue('29380')

  await dialog.getByLabel('What this letter is').selectOption('clarification')
  await expect(
    dialog.getByText('the case enters Clarification', { exact: false }),
  ).toBeVisible()
  await dialog
    .getByLabel('What was searched, in plain language')
    .fill('district Gmail for the transportation-plan terms, through the day the request arrived')
  await dialog.getByLabel('Reply requested by').fill('2026-02-06')

  await dialog.getByRole('button', { name: 'Add a narrowing option' }).click()
  await dialog
    .getByLabel('Option 1: search terms (comma separated)')
    .fill('transportation plan, 2025')
  await dialog
    .getByLabel('Option 1: what it would cover')
    .fill('Only the 2025 transportation plan and its revisions.')

  await page.screenshot({
    path: path.resolve('../.verification/officer-scope-conversation-compose.png'),
    fullPage: false,
  })

  await dialog.getByRole('button', { name: 'Create draft' }).click()
  await expect(dialog).toBeHidden()

  await page
    .getByRole('button', { name: /Scope of Your Request/ })
    .first()
    .click()

  // The exact bytes the requester would receive: the count, what was
  // searched, the option offered, and which of the two forms this is.
  await page.getByRole('button', { name: 'Preview exact message' }).first().click()
  const preview = page.getByRole('dialog', { name: 'Exact delivery preview' })
  await expect(preview).toBeVisible()
  await expect(preview.getByText('29,380 items', { exact: false })).toBeVisible()
  await expect(
    preview.getByText('RCW 42.56.520(3)', { exact: false }),
  ).toBeVisible()
  await expect(
    preview.getByText('Only the 2025 transportation plan', { exact: false }),
  ).toBeVisible()

  await page.screenshot({
    path: path.resolve('../.verification/officer-scope-conversation-draft.png'),
    fullPage: false,
  })
})
