import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

const caseNumber = 'PRR-E2E-0010'

// The criterion this flow exists for: the system drafts the interpretation-
// and-estimate letter, nothing is sent until a Records Officer approves it,
// and approving and sending it is one action on the case header.
test('officer-approve-estimate-letter', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)
  await page.goto(`/requests/${caseNumber}/overview`)

  await expect(page.getByText('Interpretation and estimate letter ready')).toBeVisible()
  await expect(page.getByText(/Drafted by the system/)).toBeVisible()
  const preview = page.getByLabel('Letter preview')
  await expect(preview).toContainText('Route 12 bus video from March 3, 2026')
  await expect(preview).toContainText('The District expects about 3 responsive records')
  await expect(preview).toContainText('about $0.15')
  await expect(preview).toContainText('RCW 42.56.520')
  await page.screenshot({
    path: path.resolve('../.verification/officer-approve-estimate-letter.png'),
    fullPage: false,
  })

  await page.getByRole('button', { name: 'Approve and send' }).click()

  await expect(page.getByText('Interpretation and estimate letter approved and sent to the requester.')).toBeVisible()
  await expect(page.getByText('Interpretation and estimate letter ready')).toHaveCount(0)
  await page.screenshot({
    path: path.resolve('../.verification/officer-approve-estimate-letter-sent.png'),
    fullPage: false,
  })
})
