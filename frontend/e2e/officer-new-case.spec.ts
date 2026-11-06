import path from 'node:path'

import { expect, test } from '@playwright/test'

import { fixtureRequestNumber, login } from './message-workspace.helpers.js'

test('officer-new-case', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)
  await page.goto(`/requests/${fixtureRequestNumber}/intake`)

  await expect(page).toHaveURL(
    new RegExp(`/requests/${fixtureRequestNumber}/intake$`),
  )
  await expect(page.getByRole('region', { name: 'Case summary' }))
    .toContainText(fixtureRequestNumber)
  await expect(page.getByText('Synthetic Requester', { exact: true }).first()).toBeVisible()
  await expect(page.getByLabel('Case owner')).toHaveValue(/\d+/)
  await expect(page.getByRole('heading', { name: 'Intake', level: 2, exact: true })).toBeFocused()
  await expect(page.getByText('Legal record of the request as received', { exact: false })).toBeVisible()
  // The case timeline lives here and only here. There is no second tab
  // rendering the same feed.
  await expect(page.getByText('Running log')).toBeVisible()
  await expect(page.getByText('Collection status')).toBeVisible()

  const rail = page.getByRole('navigation', { name: 'Case stages' })
  for (const stage of ['Intake', 'Scope', 'Collect', 'Review', 'Redact', 'Release', 'Close']) {
    await expect(rail.getByRole('link', { name: new RegExp(`^${stage}:`) })).toBeVisible()
  }
  await expect(page.getByRole('link', { name: 'Messages', exact: true })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Intake decision' })).toBeVisible()

  await page.screenshot({
    path: path.resolve('../.verification/officer-new-case-overview.png'),
    fullPage: false,
  })

  for (const tab of [
    { path: 'collect', label: 'Collect', screenshot: 'officer-case-collection.png' },
    { path: 'review', label: 'Review', screenshot: 'officer-case-records.png' },
    { path: 'release', label: 'Release', screenshot: 'officer-case-releases.png' },
    { path: 'close', label: 'Close', screenshot: 'officer-case-close.png' },
  ]) {
    await rail.getByRole('link', { name: new RegExp(`^${tab.label}:`) }).click()
    await expect(page).toHaveURL(
      new RegExp(`/requests/${fixtureRequestNumber}/${tab.path}$`),
    )
    await expect(page.getByRole('heading', {
      name: tab.label,
      exact: true,
      level: 2,
    })).toBeFocused()
    await expect(page.getByRole('region', { name: `${tab.label} decision` })).toBeVisible()
    if (tab.path === 'collect') {
      await expect(page.getByRole('list', { name: 'Collections' })).toBeVisible()
    } else if (tab.path === 'review') {
      await page.getByRole('button', { name: 'Record list' }).click()
      await expect(page.getByText('synthetic-review-record.pdf')).toBeVisible()
    } else if (tab.path === 'release') {
      await expect(page.getByText(
        'Installments appear when records are released in batches.',
      )).toBeVisible()
    } else if (tab.path === 'close') {
      await expect(page.getByText('Read-only blocker report.', { exact: false })).toBeVisible()
    }
    await page.screenshot({
      path: path.resolve(`../.verification/${tab.screenshot}`),
      fullPage: false,
    })
  }
})
