import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

const caseNumber = 'PRR-E2E-0012'

// The criterion this flow exists for: the Redact tab opens straight onto a
// responsive record's page, a dragged box plus a basis is one Enter away from
// a saved redaction, and the basis carries to the next record.
test('reviewer-redact-workspace', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page, 'e2e-reviewer', 'e2e-reviewer-local-only')
  await page.goto(`/requests/${caseNumber}/redact`)

  const records = page.getByRole('listbox', { name: 'Records to redact' })
  await expect(records.getByRole('option').first()).toHaveAttribute('aria-selected', 'true')
  const surface = page.getByLabel('Draw redactions on page 1')
  await expect(surface.getByRole('img')).toBeVisible()

  const box = await surface.boundingBox()
  if (!box) throw new Error('The page preview has no size.')
  await page.mouse.move(box.x + box.width * 0.2, box.y + box.height * 0.1)
  await page.mouse.down()
  await page.mouse.move(box.x + box.width * 0.6, box.y + box.height * 0.15, { steps: 5 })
  await page.mouse.up()

  const basis = page.getByLabel('Legal basis')
  await basis.selectOption({ index: 1 })
  const chosen = await basis.inputValue()
  await page.screenshot({
    path: path.resolve('../.verification/reviewer-redact-workspace-draft.png'),
    fullPage: false,
  })
  await page.keyboard.press('Enter')
  await expect(page.getByText('Redaction added.')).toBeVisible()
  await expect(
    page.getByRole('region', { name: 'Page' }).locator('..').getByText(/^Page 1 · /).first(),
  ).toBeVisible()

  await basis.blur()
  await page.keyboard.press('j')
  await expect(records.getByRole('option').nth(1)).toHaveAttribute('aria-selected', 'true')
  await expect(basis).toHaveValue(chosen)
  await page.screenshot({
    path: path.resolve('../.verification/reviewer-redact-workspace-next.png'),
    fullPage: false,
  })
})
