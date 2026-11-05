import path from 'node:path'

import { expect, test } from '@playwright/test'

import {
  login,
  primaryInboundSubject,
  searchMessages,
} from './message-workspace.helpers.js'

test('complete-officer-case-journey', async ({ page }) => {
  await login(page)
  // The officer lands on Inbox message triage; assigned work of every kind is
  // the "Assigned to you" chip that absorbed the retired My Work screen.
  await page.getByRole('button', { name: /^Assigned to you/ }).click()
  const assignedWork = page.getByRole('link', { name: /Open Complete record search/ }).first()
  await expect(assignedWork).toBeVisible()
  await assignedWork.click()
  await expect(page.getByRole('heading', { name: 'Work context' })).toBeVisible()
  await page.getByRole('link', { name: 'Open source context' }).click()
  await expect(page.getByRole('dialog', { name: /Task — request/ })).toBeVisible()
  await page.keyboard.press('Escape')

  await page.getByRole('link', { name: 'Messages', exact: true }).click()
  await searchMessages(page, primaryInboundSubject)
  await page.getByRole('button', { name: `Open ${primaryInboundSubject}` }).click()
  await expect(page.getByText('Team: Needs attention')).toBeVisible()
  await page.getByRole('button', { name: 'No action required' }).click()
  const noActionDialog = page.getByRole('dialog', { name: 'Record no action required' })
  await noActionDialog.getByLabel('No-action category').selectOption('informational_only')
  await noActionDialog.getByLabel('Why no team action is required').fill(
    'Synthetic officer journey records and verifies this informational disposition.',
  )
  await noActionDialog.getByRole('button', { name: 'Record no action' }).click()
  await expect(page.getByText('Team: No action required')).toBeVisible()
  await page.getByRole('button', { name: 'Reopen team work' }).click()
  await expect(page.getByText('Team: Needs attention')).toBeVisible()

  await searchMessages(page, 'Synthetic outbound message 0002')
  await page.getByRole('button', { name: 'Open Synthetic outbound message 0002' }).click()
  await expect(page.getByText('Synthetic casework message 0002.', { exact: false })).toBeVisible()
  await expect(
    page.getByRole('article', { name: 'Synthetic outbound message 0002' })
      .getByText('Sent', { exact: true })
      .first(),
  ).toBeVisible()

  // The stage rail (#45): every stage reachable, each led by its decision.
  const rail = page.getByRole('navigation', { name: 'Case stages' })
  const stage = (name: string) => rail.getByRole('link', { name: new RegExp(`^${name}:`) })

  await stage('Scope').click()
  await expect(page.getByRole('region', { name: 'Scope decision' })).toBeVisible()
  const scopeStep = page.getByText('Scope: request intent and Vault count')
  await expect(scopeStep).toBeVisible()
  if (!(await page.getByText('Google Vault scope count').isVisible())) await scopeStep.click()
  await expect(page.getByText('Google Vault scope count')).toBeVisible()

  await stage('Collect').click()
  await expect(page.getByRole('region', { name: 'Collect decision' })).toBeVisible()
  const collections = page.getByRole('list', { name: 'Collections' })
  await expect(collections.getByText('—', { exact: true })).toHaveCount(0)
  await expect(collections.getByRole('link', { name: 'View records' }).first()).toBeVisible()

  await stage('Review').click()
  await page.getByRole('button', { name: 'Record list' }).click()
  await expect(page.getByText('synthetic-review-record.pdf')).toBeVisible()

  await stage('Redact').click()
  await expect(page.getByRole('region', { name: 'Redact decision' })).toBeVisible()

  await stage('Release').click()
  await expect(page.getByRole('region', { name: 'Release decision' })).toBeVisible()

  await stage('Close').click()
  await expect(page.getByText('Read-only blocker report.', { exact: false })).toBeVisible()

  // The case timeline is the Running log on Intake.
  await stage('Intake').click()
  await expect(page.getByText('Running log')).toBeVisible()
  await expect(page.getByText('Loading case activity…')).toHaveCount(0)
  await stage('Collect').click()
  await expect(page.getByRole('list', { name: 'Collections' })).toBeVisible()

  await page.screenshot({
    path: path.resolve('../.verification/complete-officer-case-journey.png'),
    fullPage: false,
  })
})
