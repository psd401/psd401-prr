import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

const caseNumber = 'PRR-E2E-0012'

// The criterion this flow exists for: pending redaction proposals are grouped
// by exemption and pattern, a reviewer accepts a whole group only after
// accepting a random sample of it, and one action builds every ready PDF.
test('reviewer-redaction-group-accept', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page, 'e2e-reviewer', 'e2e-reviewer-local-only')
  await page.goto(`/requests/${caseNumber}/redact`)

  const groups = page.getByRole('list', { name: 'Redaction proposal groups' })
  const phones = groups.getByRole('listitem').filter({ hasText: 'Phone number' })
  await expect(phones.getByText(/RCW 42.56.250\(4\) · 3 proposals on 3 records/)).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/reviewer-redaction-groups.png'),
    fullPage: false,
  })
  await phones.getByRole('button', { name: 'Accept all in group' }).click()

  const decisions = page.getByRole('list', { name: 'Sampled proposals' }).getByRole('combobox')
  await expect(decisions).toHaveCount(2)
  for (const decision of await decisions.all()) {
    await decision.selectOption('accept')
  }
  await page.screenshot({
    path: path.resolve('../.verification/reviewer-redaction-group-sample.png'),
    fullPage: false,
  })
  await page.getByRole('button', { name: 'Record decisions' }).click()
  await expect(page.getByText(/Accepted: 3 redactions, each with its exemption citation/)).toBeVisible()
  await page.getByRole('button', { name: 'Done' }).click()

  await page.getByRole('button', { name: 'Build redacted PDFs' }).click()
  await expect(page.getByText(/Built 3\. 0 await second review\./)).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/reviewer-redaction-group-built.png'),
    fullPage: false,
  })
})
