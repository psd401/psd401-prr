import path from 'node:path'

import { expect, test } from '@playwright/test'

import {
  fixtureRequestNumber,
  login,
  primaryInboundSubject,
} from './message-workspace.helpers.js'

const evidencePath = (name: string) => path.resolve(`../.verification/${name}`)

test('correspondence-destination-parity', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)

  const inboundReceiptId = 'ab558cf4-0623-5e14-a7c4-b626e9a6613c'
  const quarantinedReceiptId = '73431c31-2a18-5f26-879e-5ac649a6b00c'
  const inboundPath = `/inbox?view=unmatched&inbound_receipt=${inboundReceiptId}`
  await page.goto(`/correspondence?inbound_receipt=${inboundReceiptId}`)
  await expect.poll(() => (
    new URL(page.url()).pathname + new URL(page.url()).search
  )).toBe(inboundPath)
  await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()
  // The queue is named for what it means to an officer now.
  await expect(page.getByRole('button', { name: /Needs a case/ }))
    .toHaveAttribute('aria-pressed', 'true')
  await page.screenshot({
    path: evidencePath('correspondence-destination-parity-inbox.png'),
    fullPage: false,
  })

  await page.goto('/correspondence?status=received')
  await expect(page).toHaveURL(/\/inbox\?view=received$/)
  await expect(page.getByRole('button', { name: 'Received' }))
    .toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByRole('link', { name: /^Open case / }).first())
    .toBeVisible()

  await page.goto('/correspondence?matching_state=candidate')
  await expect(page).toHaveURL(/\/inbox\?view=unmatched$/)
  await expect(page.getByRole('button', { name: /Needs a case/ }))
    .toHaveAttribute('aria-pressed', 'true')

  await page.goto(
    `/correspondence?status=quarantined&inbound_receipt=${quarantinedReceiptId}`,
  )
  await expect(page).toHaveURL(
    new RegExp(`/inbox\\?view=quarantined&inbound_receipt=${quarantinedReceiptId}$`),
  )
  // Held mail is not a bar chip; the "More views" trigger names and presses
  // the selected queue when it lives in the menu.
  await expect(page.getByRole('button', { name: 'More views: Held for review' }))
    .toHaveAttribute('aria-pressed', 'true')
  const quarantinedReview = page.getByRole('dialog', {
    name: 'Review inbound correspondence',
  })
  await expect(quarantinedReview).toContainText(`Receipt ${quarantinedReceiptId}`)

  await page.goto(`/correspondence?request=${fixtureRequestNumber}&status=received`)
  await expect(page).toHaveURL(
    new RegExp(`/requests/${fixtureRequestNumber}/messages\\?message_status=received$`),
  )
  await expect(page.getByRole('heading', { name: 'Messages' }))
    .toBeVisible()
  await expect(page.getByRole('button', { name: `Open ${primaryInboundSubject}` }))
    .toBeVisible()
  await page.screenshot({
    path: evidencePath('correspondence-destination-parity-messages.png'),
    fullPage: false,
  })

  await page.goto('/correspondence?status=failed')
  await expect.poll(() => (
    new URL(page.url()).pathname + new URL(page.url()).search
  )).toBe('/configuration/correspondence?focus=failed')
  await expect(page.getByRole('heading', { name: 'Message operations' }))
    .toBeVisible()
  await expect(page.getByRole('heading', { name: 'Transport queue' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Failed' }))
    .toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByText(fixtureRequestNumber).first()).toBeVisible()
  await expect(page.getByText('Delivery readiness')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Messages' })).toHaveCount(0)
  await page.screenshot({
    path: evidencePath('correspondence-destination-parity-operations.png'),
    fullPage: true,
  })
})
