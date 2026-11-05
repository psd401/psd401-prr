import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

const caseNumber = 'PRR-E2E-0011'

// The criterion this flow exists for: the officer approves the proposed scope
// and the Vault collection starts from it in one action, in their name.
test('officer-approve-and-collect', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)
  await page.goto(`/requests/${caseNumber}/collection`)

  await expect(page.getByText('Proposed scope:')).toBeVisible()
  const approve = page.getByRole('button', { name: 'Approve and collect' })
  await approve.scrollIntoViewIfNeeded()
  await page.screenshot({
    path: path.resolve('../.verification/officer-approve-and-collect.png'),
    fullPage: false,
  })
  await approve.click()

  await expect(page.getByRole('note').filter({ hasText: /Collecting this scope/ })).toBeVisible()
  await expect(approve).toHaveCount(0)
  await page.screenshot({
    path: path.resolve('../.verification/officer-approve-and-collect-started.png'),
    fullPage: false,
  })
})
