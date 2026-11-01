import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

// Seeded as a case that arrived by forward: intake recorded the relaying
// mailbox, not the person whose five-business-day clock is running.
const forwardedRequestNumber = 'PRR-E2E-0008'
const forwardedMailbox = 'info-mailbox@example.org'
const actualRequesterName = 'Marisol Vega'
const actualRequesterEmail = 'marisol.vega@example.org'

test('officer-correct-requester-identity', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)
  await page.goto(`/requests/${forwardedRequestNumber}/messages`)
  await expect(page).toHaveURL(
    new RegExp(`/requests/${forwardedRequestNumber}/messages$`),
  )

  // The record names the forwarding mailbox, so every notice would go there.
  await expect(page.getByText(forwardedMailbox).first()).toBeVisible()

  // Correct it: name, email, and the substantive reason the endpoint requires.
  await page.getByRole('button', { name: 'Correct requester' }).click()
  const dialog = page.getByRole('dialog', { name: 'Correct requester' })
  await expect(dialog).toBeVisible()
  await dialog.getByLabel('Requester name').fill(actualRequesterName)
  await dialog.getByLabel('Requester email').fill(actualRequesterEmail)
  await dialog
    .getByLabel(/Reason for the correction/)
    .fill(
      'Forwarded by the district info mailbox; the requester is named and '
      + 'signed in the forwarded email thread.',
    )
  await page.screenshot({
    path: path.resolve('../.verification/officer-correct-requester-identity.png'),
    fullPage: false,
  })
  await dialog.getByRole('button', { name: 'Correct requester' }).click()

  await expect(
    dialog.getByText(new RegExp(`The record now names ${actualRequesterName}`)),
  ).toBeVisible()
  await dialog.getByRole('button', { name: 'Done' }).click()
  await expect(page.getByText(actualRequesterEmail).first()).toBeVisible()

  // The acknowledgment drafted after the correction greets the real requester
  // and is addressed to them, and it sends.
  await page.getByRole('button', { name: 'Compose message' }).click()
  const composer = page.getByRole('dialog', { name: 'Compose correspondence' })
  await composer.getByLabel('Message type').selectOption('acknowledgment')
  await composer.getByRole('button', { name: 'Create draft' }).click()
  await expect(composer).toBeHidden()

  const draftRow = page.getByRole('button', {
    name: new RegExp(`${forwardedRequestNumber} — `),
  }).first()
  await draftRow.click()
  // The row preview and the detail pane both start with the greeting, so
  // assert on the detail body itself rather than on any text on the page.
  await expect(
    page.getByRole('region', { name: /Acknowledgment message body/ }),
  ).toContainText(`Dear ${actualRequesterName},`)
  await expect(page.getByText(actualRequesterEmail).first()).toBeVisible()

  await page.getByRole('button', { name: 'Approve and send' }).click()
  const confirm = page.getByRole('dialog', {
    name: 'Send this message to the requester',
  })
  await expect(confirm).toBeVisible()
  await confirm.getByRole('button', { name: 'Send message' }).click()
  await expect(
    page.getByText('Message approved and sent to the requester.'),
  ).toBeVisible()
})
