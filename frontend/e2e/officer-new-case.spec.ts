import path from 'node:path'

import { expect, test } from '@playwright/test'

import { fixtureRequestNumber, login } from './message-workspace.helpers.js'

test('officer-new-case', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)
  await page.goto(`/requests/${fixtureRequestNumber}/overview`)

  await expect(page).toHaveURL(
    new RegExp(`/requests/${fixtureRequestNumber}/overview$`),
  )
  await expect(
    page.getByRole('heading', { name: fixtureRequestNumber }),
  ).toBeVisible()
  await expect(page.getByText('Synthetic Requester', { exact: true }).first()).toBeVisible()
  await expect(page.getByLabel('Case owner')).toHaveValue(/\d+/)
  await expect(page.getByRole('heading', { name: 'Overview', level: 2 })).toBeFocused()
  await expect(page.getByText('Legal record of the request as received', { exact: false })).toBeVisible()
  // The case timeline lives here and only here. There is no second tab
  // rendering the same feed.
  await expect(page.getByText('Running log')).toBeVisible()
  await expect(page.getByText('Collection status')).toBeVisible()

  for (const tab of ['Overview', 'Messages', 'Collection', 'Records', 'Releases']) {
    await expect(page.getByRole('link', { name: tab, exact: true })).toBeVisible()
  }

  await page.screenshot({
    path: path.resolve('../.verification/officer-new-case-overview.png'),
    fullPage: false,
  })

  for (const tab of [
    { path: 'collection', label: 'Collection', screenshot: 'officer-case-collection.png' },
    { path: 'records', label: 'Records', screenshot: 'officer-case-records.png' },
    { path: 'releases', label: 'Releases', screenshot: 'officer-case-releases.png' },
  ]) {
    await page.getByRole('link', {
      name: tab.label,
      exact: true,
    }).click()
    await expect(page).toHaveURL(
      new RegExp(`/requests/${fixtureRequestNumber}/${tab.path}$`),
    )
    await expect(page.getByRole('heading', {
      name: tab.label,
      exact: true,
      level: 2,
    })).toBeFocused()
    if (tab.path === 'collection') {
      await expect(page.getByText('Google Vault scope count')).toBeVisible()
    } else if (tab.path === 'records') {
      await expect(page.getByText('synthetic-review-record.pdf')).toBeVisible()
    } else if (tab.path === 'releases') {
      await expect(page.getByText(
        'Installments appear when records are released in batches.',
      )).toBeVisible()
      await expect(page.getByText('Read-only blocker report.', { exact: false })).toBeVisible()
    }
    await page.screenshot({
      path: path.resolve(`../.verification/${tab.screenshot}`),
      fullPage: false,
    })
  }
})
