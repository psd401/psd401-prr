import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

const caseNumber = 'PRR-E2E-0011'

// The criterion this flow exists for: the officer sees every count iterative
// scoping ran, why the agent tightened the query, and the proposed scope; and
// re-running scoping takes a stated reason and counts again.
test('officer-scope-iterations', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)
  await page.goto(`/requests/${caseNumber}/collection`)

  const trail = page.getByRole('list', { name: 'Scope iterations' })
  await expect(trail.getByRole('listitem')).toHaveCount(2)
  await expect(trail.getByText('2,340 messages')).toBeVisible()
  await expect(trail.getByText(/Most matches were the district newsletter/)).toBeVisible()
  await expect(page.getByText(/Proposed scope: count 2, 412 messages in 1 named mailbox/)).toBeVisible()
  await page.getByText('Proposed scope:').scrollIntoViewIfNeeded()
  await page.screenshot({
    path: path.resolve('../.verification/officer-scope-iterations.png'),
    fullPage: false,
  })

  await page.getByRole('button', { name: 'Re-run scoping' }).click()
  const run = page.getByRole('button', { name: 'Run scoping' })
  await expect(run).toBeDisabled()
  await page.getByLabel(/Why run scoping again/).fill('The enrollment office moved mailboxes.')
  await run.click()
  // A re-run counts again (the acceptance Vault stand-in answers at once) and,
  // with advisory AI off, keeps that count as the proposed scope.
  await expect(page.getByText(/Re-run by .*: The enrollment office moved mailboxes\./)).toBeVisible()
  await expect(page.getByText(/Proposed scope: count 1/)).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/officer-scope-rerun.png'),
    fullPage: false,
  })
})
