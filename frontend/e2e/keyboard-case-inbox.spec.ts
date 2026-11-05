import path from 'node:path'

import { expect, test } from '@playwright/test'

import {
  fixtureRequestNumber,
  login,
  primaryInboundSubject,
  searchMessages,
} from './message-workspace.helpers.js'

test('keyboard-case-inbox', async ({ page }) => {
  let failNoAction = true
  await page.route('**/api/staff/work-items/*/no-action-required/', async (route) => {
    if (failNoAction) {
      failNoAction = false
      await route.fulfill({
        status: 500,
        json: { detail: 'Synthetic keyboard validation failure.' },
      })
      return
    }
    await route.continue()
  })
  await login(page)

  await page.getByRole('link', { name: 'Skip to main content' }).focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('main')).toBeFocused()

  const inboxLink = page.getByRole('link', { name: /Inbox/ })
  await inboxLink.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()

  // The case table's headers are controls and its rows are links, so the
  // whole surface has to be operable without a pointer: sort a column, read
  // the sort state back off the header, and reach a case from a row.
  const receivedHeader = page.getByRole('button', { name: /Received/ })
  await receivedHeader.focus()
  await page.keyboard.press('Enter')
  await expect(page).toHaveURL(/sort=received/)
  await expect(page.getByRole('button', { name: /Received/ }).locator('..'))
    .toHaveAttribute('aria-sort', 'ascending')
  await page.getByRole('button', { name: /Received/ }).focus()
  await page.keyboard.press('Enter')
  await expect(page).toHaveURL(/sort=-received/)
  await expect(page.getByRole('button', { name: /Received/ }).locator('..'))
    .toHaveAttribute('aria-sort', 'descending')
  // Tab moves along the header row to the next sortable column.
  await page.getByRole('button', { name: /Received/ }).focus()
  await page.keyboard.press('Tab')
  await expect(page.getByRole('button', { name: /Next due/ })).toBeFocused()
  const firstCaseLink = page.getByRole('link', { name: /^Open case / }).first()
  await firstCaseLink.focus()
  await expect(firstCaseLink).toBeFocused()
  // Back to the queue's own ordering, so the rest of this flow sees the
  // default Inbox.
  await page.getByRole('button', { name: /Received/ }).focus()
  await page.keyboard.press('Enter')
  await expect(page).not.toHaveURL(/sort=/)

  // Reference queues live behind "More views" now. The menu trigger and every
  // item in it must be reachable and operable by keyboard alone -- nothing on
  // this bar may depend on hover.
  const moreViews = page.getByRole('button', { name: 'More views' })
  await moreViews.focus()
  await page.keyboard.press('Enter')
  const allFilter = page.getByRole('button', { name: /^All messages/ })
  await allFilter.focus()
  await page.keyboard.press('Enter')
  // The trigger names the selected queue and carries its pressed state, so
  // the current view stays announced without reopening the menu.
  await expect(page.getByRole('button', { name: 'More views: All messages' }))
    .toHaveAttribute('aria-pressed', 'true')

  // "Open case" opens the case. Reading one message in its thread is a
  // separate, honestly-labelled action in the row's overflow.
  const overflow = page.getByRole('button', { name: /^More actions: / }).first()
  await overflow.focus()
  await page.keyboard.press('Enter')
  const readInContext = page.getByRole('link', { name: /^Read in context: / }).first()
  await readInContext.focus()
  await page.keyboard.press('Enter')
  await expect(page).toHaveURL(new RegExp(`/requests/${fixtureRequestNumber}/messages`))

  await searchMessages(page, primaryInboundSubject)
  const message = page.getByRole('button', { name: `Open ${primaryInboundSubject}` })
  await message.focus()
  await page.keyboard.press('Enter')
  await expect(message).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByText('Personal: Unread')).toBeVisible()

  const markRead = page.getByRole('button', { name: 'Mark read' })
  await markRead.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByText('Personal: Read')).toBeVisible()
  const markUnread = page.getByRole('button', { name: 'Mark unread' })
  await expect(markUnread).toBeEnabled()
  await markUnread.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByText('Personal: Unread')).toBeVisible()

  const noAction = page.getByRole('button', { name: 'No action required' })
  await expect(noAction).toBeEnabled()
  await noAction.focus()
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog', { name: 'Record no action required' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByLabel('No-action category')).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  expect(await dialog.evaluate((element) => element.contains(document.activeElement))).toBe(true)
  await page.keyboard.press('Tab')
  await expect(dialog.getByLabel('No-action category')).toBeFocused()
  await page.keyboard.press('Tab')
  await page.keyboard.type('Keyboard flow retains this synthetic message for officer review.')
  await page.keyboard.press('Tab')
  await expect(dialog.getByRole('button', { name: 'Cancel' })).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(dialog.getByRole('button', { name: 'Record no action' })).toBeFocused()
  await page.keyboard.press('Tab')
  expect(await dialog.evaluate((element) => element.contains(document.activeElement))).toBe(true)
  await page.keyboard.press('Shift+Tab')
  await expect(dialog.getByRole('button', { name: 'Record no action' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await expect(noAction).toBeFocused()

  await page.keyboard.press('Enter')
  await expect(dialog).toBeVisible()
  await expect(dialog.getByLabel('No-action category')).toBeFocused()
  await page.keyboard.press('Tab')
  await page.keyboard.type('Keyboard flow retains this synthetic message for officer review.')
  await page.keyboard.press('Tab')
  await expect(dialog.getByRole('button', { name: 'Cancel' })).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(dialog.getByRole('button', { name: 'Record no action' })).toBeFocused()
  await page.keyboard.press('Enter')
  const mutationError = dialog.getByRole('alert')
  await expect(mutationError).toContainText('Synthetic keyboard validation failure.')
  await expect(mutationError).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(dialog.getByRole('button', { name: 'Cancel' })).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(dialog.getByRole('button', { name: 'Record no action' })).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(dialog).toHaveCount(0)
  await expect(page.getByText('Team: No action required')).toBeVisible()
  const reopen = page.getByRole('button', { name: 'Reopen team work' })
  await reopen.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByText('Team: Needs attention')).toBeVisible()

  const collectionTab = page.getByRole('navigation', { name: 'Case stages' })
    .getByRole('link', { name: /^Collect:/ })
  await collectionTab.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { name: 'Collect', level: 2, exact: true })).toBeFocused()
  const liaisonRow = page.getByRole('row', { name: /e2e-liaison/ })
  await liaisonRow.focus()
  await page.keyboard.press('Enter')
  const taskDialog = page.getByRole('dialog', { name: /Task — request/ })
  await expect(taskDialog).toBeVisible()
  const assignTo = taskDialog.getByLabel('Assign to')
  await expect(assignTo.locator('option:checked')).toContainText('e2e-liaison')
  for (const [typeahead, username] of [
    ['Synthetic Officer', 'e2e-officer'],
    ['Synthetic Liaison', 'e2e-liaison'],
  ] as const) {
    // Native <select> typeahead is only as reliable as the focus underneath
    // it: the dialog moves focus on mount, so a .focus() that lands before
    // that settles sends every keystroke somewhere else and the selection
    // never moves. The buffer also clears after about a second of idleness,
    // so a retry has to start the word again rather than continue it. Both
    // are handled by asserting focus first and retrying the whole word.
    await expect(async () => {
      await assignTo.focus()
      await expect(assignTo).toBeFocused({ timeout: 1_000 })
      await assignTo.pressSequentially(typeahead)
      await expect(assignTo.locator('option:checked'))
        .toContainText(username, { timeout: 1_000 })
    }).toPass({ timeout: 20_000, intervals: [1_100, 1_100, 1_100] })
    const assignment = page.waitForResponse((response) => (
      response.request().method() === 'PATCH'
      && /\/api\/staff\/collection-tasks\/\d+\/$/.test(new URL(response.url()).pathname)
    ))
    const saveAssignment = taskDialog.getByRole('button', { name: 'Save assignment' })
    await saveAssignment.focus()
    await page.keyboard.press('Enter')
    expect((await assignment).ok()).toBe(true)
    await expect(assignTo.locator('option:checked')).toContainText(username)
    await page.waitForTimeout(1_100)
  }
  await page.keyboard.press('Escape')
  await expect(liaisonRow).toBeFocused()
  const messagesTab = page.getByRole('link', { name: 'Messages', exact: true })
  await messagesTab.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { name: 'Messages', level: 2, exact: true })).toBeFocused()
  await collectionTab.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { name: 'Collect', level: 2, exact: true })).toBeFocused()
  await page.goBack()
  await expect(page).toHaveURL(/\/messages/)
  await page.goForward()
  await expect(page).toHaveURL(/\/collect/)

  await page.screenshot({
    path: path.resolve('../.verification/keyboard-case-inbox.png'),
    fullPage: false,
  })
})
