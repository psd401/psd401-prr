import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

const awaitingRequestNumber = 'PRR-E2E-0005'

const evidencePath = (name: string) => path.resolve(`../.verification/${name}`)

test('officer-statutory-response-and-templates', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)

  // Workstream B: the 5-day backlog is a Requests filter.
  await page.goto('/requests')
  await expect(page.getByRole('heading', { name: 'Requests' })).toBeVisible()
  await page.getByRole('button', { name: /^5-day response due/ }).click()
  await expect(page.getByText(awaitingRequestNumber)).toBeVisible()
  await page.screenshot({
    path: evidencePath('officer-awaiting-statutory-response.png'),
    fullPage: false,
  })

  // Workstream A: the guided statutory send steps on the case that owes one.
  await page.goto(`/requests/${awaitingRequestNumber}/overview`)
  await expect(page.getByRole('region', { name: 'Case summary' }))
    .toContainText(awaitingRequestNumber)
  const steps = page.getByRole('list', { name: 'Statutory response steps' })
  await expect(steps).toBeVisible()
  await expect(steps.getByText('Prepare the acknowledgment draft')).toBeVisible()
  await expect(steps.getByText('Mail provider confirms delivery')).toBeVisible()
  await expect(page.getByText(/^Next:/)).toBeVisible()
  // The guide sits below the fold on this viewport; the evidence must show it.
  await steps.scrollIntoViewIfNeeded()
  await page.screenshot({
    path: evidencePath('officer-guided-statutory-send.png'),
    fullPage: false,
  })

  // The same tracker sits on Messages, where the step is actually performed,
  // and its call to action opens the acknowledgment composer directly.
  await page.getByRole('link', { name: 'Messages', exact: true }).click()
  await expect(page).toHaveURL(
    new RegExp(`/requests/${awaitingRequestNumber}/messages$`),
  )
  await expect(
    page.getByRole('list', { name: 'Statutory response steps' }),
  ).toBeVisible()
  await page.getByRole('button', { name: 'Prepare acknowledgment' }).click()
  const composer = page.getByRole('dialog', { name: 'Compose correspondence' })
  await expect(composer).toBeVisible()
  await expect(composer.getByLabel('Message type')).toHaveValue('acknowledgment')
  await page.screenshot({
    path: evidencePath('officer-messages-statutory-tracker.png'),
    fullPage: false,
  })
  await page.keyboard.press('Escape')

  // Workstream A: the officer-triggered, non-statutory courtesy receipt.
  await page.getByRole('button', { name: 'Compose message' }).click()
  await page.getByLabel('Message type').selectOption('submission_receipt')
  await expect(page.getByLabel('Subject')).toHaveCount(0)
  await page.getByRole('button', { name: 'Create draft' }).click()
  // Assert the created draft row, not the (closed) select's option text.
  const receiptRow = page.getByRole('button', {
    name: new RegExp(`${awaitingRequestNumber} — Submission Received`),
  })
  await expect(receiptRow).toBeVisible()
  await expect(receiptRow).toContainText('Submission receipt')
  await expect(receiptRow).toContainText('Draft')
  await page.screenshot({
    path: evidencePath('officer-submission-receipt-draft.png'),
    fullPage: false,
  })

  // The courtesy receipt must not have satisfied the statutory response.
  await page.getByRole('navigation', { name: 'Case stages' })
    .getByRole('link', { name: /^Intake:/ }).click()
  await expect(
    page.getByRole('list', { name: 'Statutory response steps' })
      .getByText('Prepare the acknowledgment draft'),
  ).toBeVisible()
  await expect(page.getByText('Initial response delivered')).toHaveCount(0)

  // Workstream C: the editable starting point fills an authored draft.
  await page.getByRole('link', { name: 'Messages', exact: true }).click()
  await page.getByRole('button', { name: 'Compose message' }).click()
  await page.getByLabel('Message type').selectOption('general')
  await page.getByRole('button', { name: 'Start from district template' }).click()
  await expect(page.getByLabel('Subject')).toHaveValue(
    new RegExp(awaitingRequestNumber),
  )
  // A textarea's content is its value, not its textContent, and the
  // 'Message' label substring-matches 'Message type' - so match the role
  // exactly.
  await expect(page.getByRole('textbox', { name: 'Message', exact: true })).toHaveValue(
    /\[Write the message[\s\S]*Public Records Officer/,
  )
  await page.screenshot({
    path: evidencePath('officer-template-starting-point.png'),
    fullPage: false,
  })
  await page.keyboard.press('Escape')

  // Workstream C: every kind is editable from one governed configuration page.
  await page.goto('/configuration/message-templates')
  await expect(
    page.getByRole('heading', { name: 'Message templates' }),
  ).toBeVisible()
  await expect(
    page.getByText(/never changes who may send a message/),
  ).toBeVisible()
  for (const label of [
    'Five-day acknowledgment',
    'Submission receipt',
    'Clarification request',
    'Estimate revision',
    'Installment notice',
    'No-records / out-of-retention closure',
    'Responsive-records notice',
    'Closure',
    'Denial',
    'General correspondence',
  ]) {
    await expect(page.getByRole('heading', { name: label, exact: true })).toBeVisible()
  }
  await expect(page.getByText('Structured template').first()).toBeVisible()
  await expect(page.getByText('Staff-authored').first()).toBeVisible()
  await page.screenshot({
    path: evidencePath('officer-editable-message-templates.png'),
    fullPage: true,
  })
})
