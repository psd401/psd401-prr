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

// On a laptop-width window the basis form sits beside the page, not under it,
// so choosing a basis never means scrolling past the page.
test('reviewer-redact-workspace-laptop', async ({ page }) => {
  await page.setViewportSize({ width: 1200, height: 800 })
  await login(page, 'e2e-reviewer', 'e2e-reviewer-local-only')
  await page.goto(`/requests/${caseNumber}/redact`)

  const surface = page.getByLabel('Draw redactions on page 1')
  await expect(surface.getByRole('img')).toBeVisible()
  const basis = page.getByLabel('Legal basis')
  const pageBox = await surface.boundingBox()
  const basisBox = await basis.boundingBox()
  if (!pageBox || !basisBox) throw new Error('The page or the basis form has no size.')
  expect(basisBox.x).toBeGreaterThan(pageBox.x + pageBox.width - 1)
  expect(basisBox.y).toBeLessThan(pageBox.y + pageBox.height)
  // The records are a strip above the page here, not a column beside it.
  const stripBox = await page.getByRole('navigation', { name: 'Responsive records' }).boundingBox()
  if (!stripBox) throw new Error('The records strip has no size.')
  expect(stripBox.y + stripBox.height).toBeLessThanOrEqual(pageBox.y)
  // On a short window the sticky tools column is taller than the window would
  // allow, so it scrolls inside itself rather than running off the bottom.
  await page.setViewportSize({ width: 1200, height: 500 })
  const tools = page.getByRole('complementary', { name: 'Redaction tools' })
  await expect.poll(async () => (await tools.boundingBox())?.height ?? 0).toBeLessThanOrEqual(500)
  expect(await tools.evaluate((node) => node.scrollHeight > node.clientHeight)).toBe(true)
  await page.screenshot({
    path: path.resolve('../.verification/reviewer-redact-workspace-laptop.png'),
    fullPage: false,
  })
})
