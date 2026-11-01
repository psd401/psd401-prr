import path from 'node:path'

import { expect, test } from '@playwright/test'

import { login, signOut } from './message-workspace.helpers.js'

const evidencePath = (name: string) => path.resolve(
  `../.verification/${name}`,
)

test('role-aware-work-inbox', async ({ page }) => {
  await login(page)
  // One work surface: the officer lands on the Inbox, and the queues that
  // used to live on My Work are a chip group on it.
  await expect(page).toHaveURL(/\/inbox$/)
  await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Needs attention' })).toBeVisible()
  // "Assigned to you" is a command-bar chip; the rest of the retired My Work
  // queues keep their group inside the menu.
  await expect(page.getByRole('button', { name: /Assigned to you/ })).toBeVisible()
  // Reference and secondary queues live behind "More views" now; the command
  // bar carries only the queues where not looking has a consequence.
  await page.getByRole('button', { name: /More views/ }).click()
  await expect(
    page.getByRole('group', { name: 'Inbox views: Your queues' }),
  ).toBeVisible()
  await page.getByRole('button', { name: /^Team/ }).click()
  await expect(page.getByText('Complete record search').first()).toBeVisible()
  await page.screenshot({ path: evidencePath('officer-shell.png'), fullPage: false })
  await page.getByRole('link', { name: /^Open .* for / }).first().click()
  await expect(page.getByRole('heading', { name: 'Work context' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'No action required' })).toBeVisible()
  await page.goBack()
  await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()
  // Reference and secondary queues live behind "More views" now; the command
  // bar carries only the queues where not looking has a consequence.
  await page.getByRole('button', { name: /More views/ }).click()
  await page.getByRole('button', { name: /^All messages/ }).click()
  // The row is named by its subject and states where its case stands, so it
  // can be triaged without opening anything.
  await expect(page.getByRole('link', { name: /^Open case / }).first()).toBeVisible()
  const triageRow = page.getByRole('listitem').first()
  await expect(triageRow.getByText('Synthetic inbound message 0000')).toBeVisible()
  await expect(triageRow.getByText('Requester reply')).toBeVisible()
  await expect(triageRow.getByText(/PRR-E2E-2000 · Response due /)).toBeVisible()
  // Open case is always on the row. While the row still has unresolved work,
  // Mark handled joins it inline and the reasoned officer-only decisions sit
  // one disclosure away, so the row stays dense without hiding the common
  // action. Earlier specs in this suite may already have dispositioned this
  // row, in which case only the deep link remains.
  await expect(triageRow.getByRole('link', { name: /^Open case / })).toBeVisible()
  const overflow = triageRow.getByRole('button', { name: /^More actions: / })
  if (await overflow.count()) {
    await expect(triageRow.getByRole('button', { name: /^Mark handled: / }))
      .toBeVisible()
    await overflow.click()
    await expect(triageRow.getByRole('button', { name: /^No action: / }))
      .toBeVisible()
  }
  await expect(triageRow.getByRole('link', { name: /^Work details: / })).toBeVisible()
  if (await overflow.count()) await overflow.click()
  await page.waitForTimeout(250)
  await page.screenshot({ path: evidencePath('officer-inbox.png'), fullPage: false })
  await page.goto('/configuration/identity')
  await expect(page.getByRole('heading', { name: 'Not authorized' })).toBeVisible()
  await signOut(page)

  await login(page, 'e2e-liaison', 'e2e-liaison-local-only')
  await expect(page).toHaveURL(/\/inbox$/)
  await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()
  // A liaison gets the assignment queues and no cross-case message triage.
  await expect(page.getByRole('button', { name: /Assigned to you/ }))
    .toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByRole('button', { name: 'Needs attention' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Delivery exceptions' }))
    .toHaveCount(0)
  await page.getByRole('button', { name: /^Team/ }).click()
  await expect(page.getByText('Complete record search').first()).toBeVisible()
  await page.screenshot({ path: evidencePath('liaison-shell.png'), fullPage: false })
  await page.getByRole('link', { name: /^Open .* for / }).first().click()
  await expect(page.getByRole('heading', { name: 'Work context' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'No action required' })).toHaveCount(0)
  await page.getByRole('link', { name: 'Open source context' }).click()
  await expect(page).toHaveURL(
    /\/requests\/PRR-E2E-2000\/collection\?task=\d+$/,
  )
  await expect(page.getByRole('dialog', { name: /Task — request/ })).toBeVisible()
  await page.keyboard.press('Escape')
  await signOut(page)

  await login(page, 'e2e-reviewer', 'e2e-reviewer-local-only')
  await expect(page).toHaveURL(/\/review$/)
  await expect(page.getByRole('heading', { name: 'Review' })).toBeVisible()
  await expect(page.getByText('Review record')).toBeVisible()
  await page.screenshot({ path: evidencePath('reviewer-shell.png'), fullPage: false })
  await page.getByRole('link', { name: /Open Review record/ }).click()
  await expect(page.getByRole('heading', { name: 'Work context' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'No action required' })).toHaveCount(0)
  await expect(page.getByRole('link', { name: 'Open source context' })).toBeVisible()
  // The reviewer's Inbox is the case index and nothing else: the server scopes
  // which cases they see and blanks requester identity, and no cross-case
  // message queue is offered to them at all.
  await page.goto('/inbox')
  await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()
  await expect(page.getByRole('button', { name: /All cases/ })).toBeVisible()
  await expect(page.getByRole('button', { name: /Needs attention/ })).toHaveCount(0)
  await expect(page.getByRole('button', { name: /Awaiting statutory response/ }))
    .toHaveCount(0)
  await signOut(page)

  await login(page, 'e2e-intake', 'e2e-intake-local-only')
  await expect(page).toHaveURL(/\/staff-intake$/)
  await expect(page.getByRole('heading', { name: 'Staff intake' })).toBeVisible()
  await page.screenshot({ path: evidencePath('intake-shell.png'), fullPage: false })
  await signOut(page)

  await login(page, 'e2e-admin', 'e2e-admin-local-only')
  await expect(page).toHaveURL(/\/configuration$/)
  await expect(page.getByRole('heading', { name: 'Configuration' })).toBeVisible()
  await page.screenshot({ path: evidencePath('admin-shell.png'), fullPage: false })
  await page.getByRole('link', { name: /Inbox/ }).click()
  await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Delivery exceptions' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Needs attention' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: /Assigned to you/ })).toHaveCount(0)
  await page.waitForTimeout(250)
  await page.screenshot({ path: evidencePath('admin-inbox.png'), fullPage: false })
  await page.getByRole('link', { name: /^Open .* for / }).first().click()
  await expect(page.getByRole('heading', { name: 'Work context' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'No action required' })).toHaveCount(0)
})
