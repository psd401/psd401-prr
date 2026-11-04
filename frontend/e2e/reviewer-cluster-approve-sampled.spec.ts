import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

const caseNumber = 'PRR-E2E-0011'

// The criterion this flow exists for: a reviewer approves a whole cluster only
// after judging a random sample of it, and the decision is recorded in the
// reviewer's name for every record in the cluster.
test('reviewer-cluster-approve-sampled', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page, 'e2e-reviewer', 'e2e-reviewer-local-only')
  await page.goto(`/requests/${caseNumber}/records?group=cluster&cluster_by=sender`)

  const clusters = page.getByRole('list', { name: 'Record clusters' })
  const newsletter = clusters.getByRole('listitem').filter({
    has: page.getByRole('heading', { name: 'newsletter.example' }),
  })
  await expect(newsletter.getByText('4 records')).toBeVisible()
  // Responsive stays per-record: only the non-responsive batch is offered.
  await expect(newsletter.getByRole('button', { name: /Approve cluster as responsive/ })).toHaveCount(0)
  await newsletter.getByRole('button', { name: 'Approve cluster as non-responsive' }).click()

  const panel = page.getByText('Approve newsletter.example as non-responsive')
  await expect(panel).toBeVisible()
  const judgments = page.getByRole('list', { name: 'Sampled records' }).getByRole('combobox')
  await expect(judgments).toHaveCount(2)
  await page.screenshot({
    path: path.resolve('../.verification/reviewer-cluster-approve-sample.png'),
    fullPage: false,
  })
  for (const judgment of await judgments.all()) {
    await judgment.selectOption('non_responsive')
  }
  await page.getByRole('button', { name: 'Record judgments' }).click()

  await expect(page.getByText('Applied: 4 records marked non-responsive in your name.')).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/reviewer-cluster-approve-applied.png'),
    fullPage: false,
  })
})
