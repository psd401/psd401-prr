import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

/** A case whose completed Vault collection swept in unreviewed mail. */
const supersedeCase = 'PRR-E2E-0009'

// The criterion this flow exists for: an over-broad export leaves review in
// one stated decision, and nothing is deleted -- the records stay one filter
// away.
test('officer-supersede-collection', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)

  await page.goto(`/requests/${supersedeCase}/records`)
  const newsletters = page.getByRole('row').filter({ hasText: 'synthetic-supersede-newsletter' })
  await expect(newsletters).toHaveCount(3)

  await page.goto(`/requests/${supersedeCase}/collection`)
  await page.getByRole('row').filter({ hasText: 'Vault' }).first().click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('button', { name: 'Supersede collection' }).click()
  await dialog.getByLabel('Reason').fill(
    'The first scope swept in district newsletters; re-scoping to named senders.',
  )
  await page.screenshot({
    path: path.resolve('../.verification/officer-supersede-collection-confirm.png'),
    fullPage: false,
  })
  await dialog.getByRole('button', { name: 'Supersede unreviewed records' }).click()
  await expect(page.getByText('3 unreviewed records left review.')).toBeVisible()

  await page.goto(`/requests/${supersedeCase}/records`)
  await expect(newsletters).toHaveCount(0)
  await page.getByRole('button', { name: 'Superseded', exact: true }).click()
  await expect(newsletters).toHaveCount(3)
  await page.screenshot({
    path: path.resolve('../.verification/officer-supersede-collection-filter.png'),
    fullPage: false,
  })
})
