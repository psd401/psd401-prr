import path from 'node:path'

import { expect, test } from '@playwright/test'

import { fixtureRequestNumber, login } from './message-workspace.helpers.js'

test('officer-clarification-response', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)
  await page.goto(`/requests/${fixtureRequestNumber}/messages`)

  await expect(page).toHaveURL(
    new RegExp(`/requests/${fixtureRequestNumber}/messages$`),
  )
  await expect(page.getByRole('heading', { name: 'Messages', level: 2 })).toBeFocused()
  await expect(page.getByRole('heading', { name: 'Messages' })).toBeVisible()
  await expect(page.getByText('Clarification response received')).toBeVisible()

  await page.getByRole('button', { name: 'Resolve clarification' }).click()
  const dialog = page.getByRole('dialog', { name: 'Resolve clarification' })
  await expect(dialog).toBeVisible()
  await dialog.getByLabel('Resolution notes').fill(
    'The requester confirmed the date range and the district systems to search.',
  )

  await page.screenshot({
    path: path.resolve('../.verification/officer-clarification-response.png'),
    fullPage: false,
  })

  await dialog.getByRole('button', { name: 'Resolve cycle' }).click()
  await expect(
    page.getByText('Clarification resolved and request returned to gathering.'),
  ).toBeVisible()
  await expect(page.getByText('Gathering Records', { exact: true })).toBeVisible()
})
