import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

const awaitingRequestNumber = 'PRR-E2E-0005'

const evidencePath = (name: string) => path.resolve(`../.verification/${name}`)

test('officer-statutory-response-and-templates', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)

  // Workstream B: grouped chips and the statutory backlog with triage context.
  await page.getByRole('link', { name: /Inbox/ }).click()
  await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()
  // Decide-now queues are chips; the taxonomy that used to fill the page is
  // intact inside the menu.
  await expect(page.getByRole('button', { name: /Needs attention/ })).toBeVisible()
  // Reference and secondary queues live behind "More views" now; the command
  // bar carries only the queues where not looking has a consequence.
  await page.getByRole('button', { name: /More views/ }).click()
  await expect(
    page.getByRole('group', { name: 'Inbox views: Reference' }),
  ).toBeVisible()
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: /More views/ }).click()

  await page.getByRole('button', { name: /^Awaiting statutory response/ }).click()
  await expect(page).toHaveURL(/view=awaiting_statutory_response/)
  await expect(page.getByText(/does not satisfy the deadline/)).toBeVisible()
  await expect(page.getByText(awaitingRequestNumber)).toBeVisible()
  await expect(page.getByText(/Statutory response due: Jan 20, 2026/)).toBeVisible()
  await page.screenshot({
    path: evidencePath('officer-awaiting-statutory-response.png'),
    fullPage: false,
  })

  // Officer message rows are named by their subject and carry triage context.
  // Reference and secondary queues live behind "More views" now; the command
  // bar carries only the queues where not looking has a consequence.
  await page.getByRole('button', { name: /More views/ }).click()
  await page.getByRole('button', { name: /^All messages/ }).click()
  await expect(page.getByRole('link', { name: /^Open case / }).first()).toBeVisible()
  const triageRow = page.getByRole('listitem').first()
  // The subject is the row title and the snippet sits under it. The
  // scanning metadata - work kind, message kind, legal status, statutory
  // position - is one small line of plain text at the bottom of the row now,
  // not a stack of uppercase pills above the message.
  await expect(triageRow.getByText('Synthetic inbound message 0000'))
    .toBeVisible()
  await expect(triageRow.getByText('Requester reply')).toBeVisible()
  await expect(triageRow.getByText('Clarification response')).toBeVisible()
  // The case's legal status, whatever earlier flows in this suite moved it to.
  await expect(triageRow).toHaveText(
    /(Received|Acknowledged|Awaiting clarification|Gathering records|Review and redaction|Producing installments|Fulfilled|Denied|Closed — )/,
  )
  await expect(triageRow.getByText('Five-day response delivered')).toBeVisible()
  await expect(triageRow.getByText(/Jan \d+, 2026, \d+:\d+/)).toBeVisible()
  await expect(triageRow.getByText(/Synthetic casework message 0000/))
    .toBeVisible()
  await page.screenshot({
    path: evidencePath('officer-inbox-triage-context.png'),
    fullPage: false,
  })

  // Workstream A: the guided statutory send steps on the case that owes one.
  await page.goto(`/requests/${awaitingRequestNumber}/overview`)
  await expect(
    page.getByRole('heading', { name: awaitingRequestNumber }),
  ).toBeVisible()
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
  await page.getByRole('link', { name: 'Overview', exact: true }).click()
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
    'Responsive-records notice (starting point)',
    'Closure (starting point)',
    'Denial (starting point)',
    'General correspondence (starting point)',
  ]) {
    await expect(page.getByRole('heading', { name: label })).toBeVisible()
  }
  await expect(page.getByText('Structured template').first()).toBeVisible()
  await expect(page.getByText('Starting point').first()).toBeVisible()
  await page.screenshot({
    path: evidencePath('officer-editable-message-templates.png'),
    fullPage: true,
  })
})
