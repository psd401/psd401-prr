import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login } from './message-workspace.helpers.js'

// ADR 0004: a FERPA parent request is opened deliberately on a staff channel,
// answers to 34 CFR 99.10 rather than RCW 42.56.520, and discloses nothing
// until a named officer has recorded who the requester is.
test('officer-matter-kinds', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)

  await page.goto('/staff-intake')
  await expect(
    page.getByRole('heading', { name: 'Staff intake' }),
  ).toBeVisible()

  const kind = page.getByLabel('What kind of matter is this?')
  await expect(kind).toHaveValue('public_records')
  await kind.selectOption('ferpa_parent')
  await page.getByLabel('How was the request received?').selectOption('in_person')
  await page
    .getByLabel('What records were requested?')
    .fill('Attendance and discipline records for my child for this school year.')
  await page.getByLabel('Phone number (optional)').fill('253-555-0184')

  await page.screenshot({
    path: path.resolve('../.verification/officer-matter-kind-intake.png'),
    fullPage: false,
  })

  await page.getByRole('button', { name: 'Log request' }).click()

  // The number itself says this is not a public records request.
  const number = page.locator('p.font-mono')
  await expect(number).toHaveText(/^FER-\d{4}-\d{4}$/)
  const caseNumber = (await number.textContent())!.trim()

  await page.goto(`/requests/${caseNumber}/overview`)
  await expect(page.getByRole('heading', { name: caseNumber })).toBeVisible()
  await expect(page.getByText('Running log')).toBeVisible()

  // The FERPA clock is shown and the RCW 42.56.520 one is absent entirely.
  await expect(
    page.getByText('FERPA parent or eligible student (34 CFR 99.10)', {
      exact: true,
    }),
  ).toBeVisible()
  await expect(page.getByText('FERPA access due (45 days)')).toBeVisible()
  await expect(page.getByText('Statutory response due')).toHaveCount(0)
  await expect(page.getByText('Tolled business days')).toHaveCount(0)
  await expect(page.getByText('No RCW 42.56.520 response owed')).toBeVisible()
  await expect(page.getByText('Deadline calculation pending')).toHaveCount(0)

  // Nothing may go out until identity is verified, and no PRA-only action
  // is offered on a matter that never owed a five-day response.
  await expect(
    page.getByText('Nothing may be disclosed to this requester until one is.', {
      exact: false,
    }),
  ).toBeVisible()
  await expect(
    page.getByRole('button', {
      name: 'Record a response sent outside this system…',
    }),
  ).toHaveCount(0)

  await page.screenshot({
    path: path.resolve('../.verification/officer-matter-kind-ferpa-case.png'),
    fullPage: false,
  })

  await page
    .getByRole('button', { name: 'Record identity verification…' })
    .click()
  const dialog = page.getByRole('dialog')
  await expect(
    dialog.getByRole('heading', { name: 'Record FERPA identity verification' }),
  ).toBeVisible()
  await dialog
    .getByLabel('How the identity was verified')
    .selectOption('in_person_photo_id')
  await dialog
    .getByLabel('What you relied on')
    .fill('Washington driver licence checked against the guardian of record.')

  await page.screenshot({
    path: path.resolve('../.verification/officer-matter-kind-identity.png'),
    fullPage: false,
  })

  await dialog.getByRole('button', { name: 'Record verification' }).click()

  // The panel states it, and the timeline carries the officer's own words.
  await expect(
    page.getByText('Identity verified under 34 CFR 99.10', { exact: false }).first(),
  ).toBeVisible()
  await expect(
    page.getByText('FERPA requester identity verified under 34 CFR 99.10', {
      exact: false,
    }).first(),
  ).toBeVisible()
  await expect(
    page.getByRole('button', { name: 'Record identity verification…' }),
  ).toHaveCount(0)

  await page.screenshot({
    path: path.resolve('../.verification/officer-matter-kind-identity-recorded.png'),
    fullPage: false,
  })
})
