import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login, signOut } from './message-workspace.helpers.js'

/** Intake opened this one automatically from an emailed follow-up. */
const autoCreatedCase = 'PRR-E2E-0006'
/** The case that follow-up actually belongs to. */
const existingCase = 'PRR-E2E-0007'
const emailSubject = 'Re: bus route records — one more thing'

test('officer-email-to-existing-case', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)

  await page.goto(`/requests/${autoCreatedCase}/overview`)
  await expect(page.getByRole('heading', { name: 'Request', exact: true })).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/officer-auto-created-email-case.png'),
    fullPage: false,
  })

  // The officer recognizes the case this message belongs to and says so once.
  const addToCase = page.getByRole('button', { name: 'Add to an existing case…' })
  await expect(addToCase).toBeEnabled()
  await addToCase.click()

  const dialog = page.getByRole('dialog', { name: 'Add to an existing case' })
  await dialog.getByLabel(/Case to add this message to/).fill(existingCase)
  await dialog.getByLabel(/Why this message belongs to that case/).fill(
    'The requester is following up on their open bus route records case.',
  )
  await page.screenshot({
    path: path.resolve('../.verification/officer-add-email-to-existing-case.png'),
    fullPage: false,
  })
  await dialog.getByRole('button', { name: 'Add to case' }).click()

  // One action, two outcomes: the leftover case closes in writing...
  await expect(
    page.getByText('Closed – Not a Records Request').first(),
  ).toBeVisible()
  await expect(
    page.getByRole('button', { name: 'Add to an existing case…' }),
  ).toHaveCount(0)

  // ...and the message is now on the case where the work is.
  await page.goto(`/requests/${existingCase}/messages`)
  await expect(page.getByRole('heading', { name: 'Messages' })).toBeVisible()
  await expect(
    page.getByRole('button', { name: `Open ${emailSubject}` }),
  ).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/officer-message-on-existing-case.png'),
    fullPage: false,
  })

  // Closing a case is no longer the Records Officer's exclusive power: a
  // reviewer working this case can record the ending themselves.
  await signOut(page)
  await login(page, 'e2e-reviewer', 'e2e-reviewer-local-only')
  await page.goto(`/requests/${existingCase}/overview`)
  await expect(
    page.getByRole('button', { name: 'Change status' }),
  ).toHaveCount(0)

  await page.getByRole('button', { name: 'Close case…' }).click()
  const closeDialog = page.getByRole('dialog', { name: 'Close case' })
  await closeDialog.getByLabel('How this case ended').selectOption('closed_withdrawn')
  await closeDialog.getByLabel(/Closure reason/).fill(
    'Requester withdrew by phone; the call is logged in the case notes.',
  )
  await page.screenshot({
    path: path.resolve('../.verification/reviewer-close-case.png'),
    fullPage: false,
  })
  await closeDialog.getByRole('button', { name: 'Close case' }).click()

  await expect(page.getByText('Closed – Withdrawn').first()).toBeVisible()
  await expect(
    page.getByRole('button', { name: 'Close case…' }),
  ).toHaveCount(0)
  await page.screenshot({
    path: path.resolve('../.verification/reviewer-closed-case.png'),
    fullPage: false,
  })
})
