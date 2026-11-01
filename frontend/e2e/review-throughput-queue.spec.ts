import path from 'node:path'

import { expect, test } from '@playwright/test'

import { fixtureRequestNumber, login } from './message-workspace.helpers.js'

// The criterion this flow exists for: every out-of-office reply on a case can
// be filtered to, seen, and marked in one pass -- and a message found in two
// mailboxes is one record that still names both custodians.
test('review-throughput-queue', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)
  await page.goto(`/requests/${fixtureRequestNumber}/records`)
  await expect(page.getByRole('heading', { name: 'Records', level: 2 })).toBeFocused()

  // One record collected from two mailboxes appears once, naming both.
  const deduplicated = page
    .getByRole('row')
    .filter({ hasText: 'synthetic-auto-reply-transportation.eml' })
  await expect(deduplicated).toHaveCount(1)
  await expect(deduplicated.getByText('+1 more')).toBeVisible()

  // The tag is a fact read off the message headers, and its facet count
  // describes the set the filter will return.
  const tagFilter = page.getByLabel('Tag')
  await expect(tagFilter.getByRole('option', { name: /Automatic reply \(3\)/ })).toBeAttached()
  await tagFilter.selectOption('auto_reply')
  await expect(page).toHaveURL(/tag=auto_reply/)
  await expect(page.getByRole('row').filter({ hasText: '.eml' })).toHaveCount(3)
  await expect(
    page.getByRole('row').filter({ hasText: 'synthetic-review-record.pdf' }),
  ).toHaveCount(0)

  await page.screenshot({
    path: path.resolve('../.verification/review-throughput-auto-reply-filter.png'),
    fullPage: false,
  })

  // One confirmation over the visible, explicitly selected set.
  await page.getByLabel('Select every record on this page').check()
  await expect(page.getByText('3 selected')).toBeVisible()
  await page.getByLabel('Bulk review outcome').selectOption('non_responsive')

  await page.screenshot({
    path: path.resolve('../.verification/review-throughput-bulk-selection.png'),
    fullPage: false,
  })

  await page.getByRole('button', { name: 'Review selected' }).click()
  const confirm = page.getByRole('dialog', { name: 'Record this review outcome' })
  await expect(confirm).toBeVisible()
  // The confirmation says whose decision this is, in as many words.
  await expect(
    confirm.getByText('not an acceptance of any AI suggestion', { exact: false }),
  ).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/review-throughput-bulk-confirm.png'),
    fullPage: false,
  })
  await confirm.getByRole('button', { name: 'Record 3 outcomes' }).click()

  await expect(page.getByText('3 selected')).toHaveCount(0)
  await expect(
    page.getByRole('row').filter({ hasText: '.eml' }).getByText('Non-responsive').first(),
  ).toBeVisible()

  await page.screenshot({
    path: path.resolve('../.verification/review-throughput-bulk-reviewed.png'),
    fullPage: false,
  })
})
