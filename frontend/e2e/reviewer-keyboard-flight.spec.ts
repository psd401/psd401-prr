import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

const caseNumber = 'PRR-E2E-0013'

// The criterion this flow exists for: a reviewer flies through records with
// the keyboard alone -- the AI suggestion is inline, one key decides and moves
// on with no modal, u undoes, and Cmd-K reaches any command.
test('reviewer-keyboard-flight', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page, 'e2e-reviewer', 'e2e-reviewer-local-only')
  await page.goto(`/requests/${caseNumber}/review`)

  const list = page.getByRole('listbox', { name: 'Records to review' })
  await expect(list.getByRole('option')).toHaveCount(3)
  const first = list.getByRole('option', { selected: true })
  await expect(first).toBeFocused()
  const preview = page.getByAltText(/Page 1 of synthetic-/)
  await expect(preview).toBeVisible()
  // The PDF page has actually rendered, not just the element.
  await expect.poll(() => preview.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0)
  await page.screenshot({
    path: path.resolve('../.verification/reviewer-keyboard-flight.png'),
    fullPage: false,
  })

  // Find the lunch menu, decide it, and watch it leave the unreviewed list.
  const lunch = list.getByRole('option', { name: /synthetic-flight-lunch-menu / })
  while ((await lunch.getAttribute('aria-selected')) !== 'true') await page.keyboard.press('j')
  await expect(page.getByLabel('AI rationale')).toContainText('Cafeteria lunch menu')
  await page.keyboard.press('n')
  await expect(page.getByText(/synthetic-flight-lunch-menu\.pdf: Non-responsive\./)).toBeVisible()
  await expect(list.getByRole('option')).toHaveCount(2)

  // Undo brings it back for review.
  await page.keyboard.press('u')
  await expect(page.getByText(/Undone: synthetic-flight-lunch-menu\.pdf is Unreviewed\./)).toBeVisible()
  await expect(list.getByRole('option')).toHaveCount(3)

  // The palette reaches the same decision.
  await page.keyboard.press('Meta+k')
  await page.getByLabel('Search commands').fill('responsive')
  await page.getByRole('button', { name: 'Mark responsive' }).click()
  await expect(page.getByText(/: Responsive\./)).toBeVisible()
  await expect(list.getByRole('option')).toHaveCount(2)
  await page.screenshot({
    path: path.resolve('../.verification/reviewer-keyboard-flight-decided.png'),
    fullPage: false,
  })
})
