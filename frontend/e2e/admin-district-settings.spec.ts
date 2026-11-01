import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login, signOut } from './message-workspace.helpers.js'

// The acceptance this work is for, end to end: an Application Admin changes a
// district switch from the Settings page with a written reason, the new value
// sticks without a redeploy, and the change appears in that setting's history.
// The dialog must refuse an empty or token reason before it sends anything,
// and a non-admin must not reach the page at all.
//
// Phase B adds the second half: a setting that is not a switch. The camera
// retention window is the one to prove it with, because it is the value whose
// wrong setting destroys a public record -- its editor must show the bounds,
// refuse a number outside them with the limit in the message, and keep the
// consequence in front of the administrator while they type.
test('admin-district-settings', async ({ page }) => {
  await login(page, 'e2e-admin', 'e2e-admin-local-only')
  await expect(page.getByRole('heading', { name: 'Configuration' })).toBeVisible()

  await page.getByRole('link', { name: 'District settings' }).click()
  await expect(page).toHaveURL(/\/configuration\/settings$/)
  await expect(page.getByRole('heading', { name: 'District settings' })).toBeVisible()

  // The consequence is on the page, not only in the dialog: an admin has to be
  // able to read what a switch costs before deciding to open it.
  // Filter on the consequence, not the label: the automated-outbound row also
  // mentions the safety net by name in its own consequence text.
  const safetyNet = page
    .getByRole('listitem')
    .filter({ hasText: /strict liability under RCW 42\.56\.520/ })
  await expect(
    safetyNet.getByText(/strict liability under RCW 42\.56\.520/),
  ).toBeVisible()
  await expect(
    safetyNet.getByText('Never changed in the application.'),
  ).toBeVisible()

  await safetyNet.getByRole('button', { name: 'Turn on' }).click()
  const dialog = page.getByRole('dialog')
  await expect(
    dialog.getByText(/strict liability under RCW 42\.56\.520/),
  ).toBeVisible()
  const confirm = dialog.getByRole('button', { name: 'Turn on' })
  await expect(confirm).toBeDisabled()
  await dialog.getByRole('textbox').fill('too short')
  await expect(confirm).toBeDisabled()
  await page.screenshot({
    path: path.resolve('../.verification/admin-district-settings-reason.png'),
    fullPage: false,
  })
  await dialog
    .getByRole('textbox')
    .fill('Counsel asked for the statutory day-5 backstop before cutover.')
  await expect(confirm).toBeEnabled()
  await confirm.click()

  await expect(page.getByRole('dialog')).toBeHidden()
  await expect(safetyNet.getByRole('button', { name: 'Turn off' })).toBeVisible()
  await expect(
    safetyNet.getByText(/Counsel asked for the statutory day-5 backstop/),
  ).toBeVisible()

  await safetyNet.getByRole('button', { name: 'History' }).click()
  await expect(
    safetyNet.getByText(/Counsel asked for the statutory day-5 backstop/).first(),
  ).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/admin-district-settings.png'),
    fullPage: false,
  })

  // The value survives a fresh load of the page, which is what "no redeploy"
  // means from the district's side.
  await page.reload()
  await expect(
    page
      .getByRole('listitem')
      .filter({ hasText: /strict liability under RCW 42\.56\.520/ })
      .getByRole('button', { name: 'Turn off' }),
  ).toBeVisible()

  // A setting that is not a switch: a bounded number, edited in place.
  const retention = page
    .getByRole('listitem')
    .filter({ hasText: /How long the district/ })
    .filter({ hasText: /retention/ })
  await expect(retention.getByText('10 days', { exact: true })).toBeVisible()
  await retention.getByRole('button', { name: 'Change' }).click()
  const limitDialog = page.getByRole('dialog')
  await expect(limitDialog.getByText(/never held and can be overwritten/))
    .toBeVisible()
  await expect(limitDialog.getByText('Between 1 and 3,650 days.')).toBeVisible()
  const value = limitDialog.getByRole('spinbutton')
  await expect(value).toHaveValue('10')

  // Out of bounds cannot be sent, and the refusal names the limit in a
  // persistent alert rather than a transient browser bubble.
  await value.fill('5000')
  await limitDialog
    .getByRole('textbox')
    .fill('Attempting a retention window the district cannot have.')
  const saveLimit = limitDialog.getByRole('button', { name: 'Save change' })
  await expect(limitDialog.getByRole('alert')).toHaveText(
    'Between 1 and 3,650 days.',
  )
  await expect(saveLimit).toBeDisabled()
  await page.screenshot({
    path: path.resolve('../.verification/admin-district-settings-limit.png'),
    fullPage: false,
  })

  await value.fill('45')
  await limitDialog
    .getByRole('textbox')
    .fill('Facilities confirmed the bus-video vendor retains 45 days.')
  await expect(saveLimit).toBeEnabled()
  await saveLimit.click()
  await expect(page.getByRole('dialog')).toBeHidden()
  await expect(retention.getByText('45 days', { exact: true })).toBeVisible()
  await expect(
    retention.getByText(/Facilities confirmed the bus-video vendor/),
  ).toBeVisible()

  await page.reload()
  await expect(
    page
      .getByRole('listitem')
      .filter({ hasText: /How long the district/ })
      .filter({ hasText: /retention/ })
      .getByText('45 days', { exact: true }),
  ).toBeVisible()

  // Phase C adds the third kind: a credential. The acceptance the district
  // asked for is a rotation an Application Admin performs from this page with
  // a reason, after which the value cannot be read back -- not by the page,
  // and not by the person who just typed it.
  const clientSecret = page
    .getByRole('listitem')
    .filter({ hasText: /It is write-only/ })
    .filter({ hasText: 'Google sign-in client secret' })
  await expect(clientSecret.getByText('Not set', { exact: true })).toBeVisible()
  await expect(
    clientSecret.getByText(/No value is installed/),
  ).toBeVisible()

  await clientSecret.getByRole('button', { name: 'Set', exact: true }).click()
  const secretDialog = page.getByRole('dialog')
  await expect(secretDialog.getByText(/It is write-only/)).toBeVisible()
  await expect(secretDialog.getByText(/cannot be read back/)).toBeVisible()
  // The editor is masked and empty: there is nothing to pre-fill it with.
  const secretField = secretDialog.locator('input[type="password"]')
  await expect(secretField).toHaveValue('')
  await secretField.fill('GOCSPX-rotated-in-the-browser')
  await secretDialog
    .locator('textarea')
    .fill('Rotated the Google sign-in client secret after the access review.')
  await page.screenshot({
    path: path.resolve('../.verification/admin-district-settings-secret.png'),
    fullPage: false,
  })
  await secretDialog.getByRole('button', { name: 'Save change' }).click()
  await expect(page.getByRole('dialog')).toBeHidden()

  await expect(clientSecret.getByText('Set', { exact: true })).toBeVisible()
  await expect(
    clientSecret.getByText(/Installed, fingerprint [0-9a-f]{12}/),
  ).toBeVisible()
  await expect(
    clientSecret.getByText(/The value cannot be read back\./),
  ).toBeVisible()
  await expect(
    clientSecret.getByRole('button', { name: 'Replace' }),
  ).toBeVisible()

  // The value is nowhere on the page, and a reload does not produce it.
  await page.reload()
  await expect(page.locator('body')).not.toContainText(
    'GOCSPX-rotated-in-the-browser',
  )
  const rotated = page
    .getByRole('listitem')
    .filter({ hasText: /It is write-only/ })
    .filter({ hasText: 'Google sign-in client secret' })
  await expect(
    rotated.getByText(/Installed, fingerprint [0-9a-f]{12}/),
  ).toBeVisible()

  // The history records that it changed, and what its fingerprint became.
  await rotated.getByRole('button', { name: 'History' }).click()
  await expect(
    rotated.getByText(/Rotated the Google sign-in client secret/).first(),
  ).toBeVisible()
  await expect(rotated.getByText(/Changed to set \([0-9a-f]{12}\)/)).toBeVisible()
  await page.screenshot({
    path: path.resolve(
      '../.verification/admin-district-settings-secret-history.png',
    ),
    fullPage: false,
  })

  await signOut(page)
  await login(page, 'e2e-officer', 'e2e-local-only')
  await page.goto('/configuration/settings')
  await expect(page.getByRole('heading', { name: 'Not authorized' })).toBeVisible()
  await page.screenshot({
    path: path.resolve('../.verification/admin-district-settings-denied.png'),
    fullPage: false,
  })
})
