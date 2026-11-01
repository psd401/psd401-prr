import path from 'node:path'

import { expect, test, type Response } from '@playwright/test'

import {
  fixtureRequestNumber,
  login,
  openFixtureRequest,
  primaryInboundSubject,
  searchMessages,
} from './message-workspace.helpers.js'

type Evidence<T> = { payload: T; bytes: number }

/**
 * Read a response body, or `null` when Chromium no longer has it.
 *
 * This test asserts on payload size as well as shape, and the only source of
 * the bytes is Chromium's network store. That store holds nothing for a
 * request the renderer cancelled -- React Query aborts an in-flight list fetch
 * when the workspace remounts -- so `response.body()` rejects with
 * "Network.getResponseBody: No data found for resource with given identifier".
 * Unguarded, that rejection failed the whole suite on main (FLAKY.md,
 * 2026-10-10). A cancelled response delivered nothing to the page, so it is
 * not evidence: drop it and let the assertions count what actually arrived.
 */
const PAYLOAD_DISCARDED = /No data found for resource/

function responseEvidence<T>(response: Response): Promise<Evidence<T> | null> {
  return response.body().then(
    (body) => ({
      payload: JSON.parse(body.toString('utf8')) as T,
      bytes: body.byteLength,
    }),
    // Only the discarded-payload case is tolerated. A connection reset
    // mid-download, or any other read failure, still fails the test rather
    // than quietly shrinking the evidence this spec asserts on.
    (error: unknown) => {
      if (error instanceof Error && PAYLOAD_DISCARDED.test(error.message)) return null
      throw error
    },
  )
}

/**
 * The responses the page actually received, in ARRIVAL order.
 *
 * Not page order: the assertions against these are per-response bounds (page
 * size and byte ceiling), which hold for any page, so arrival order is enough.
 * Read an index here as "a delivered response", not "page N".
 */
async function delivered<T>(captured: Array<Promise<Evidence<T> | null>>) {
  const settled = await Promise.all(captured)
  return settled.filter((evidence): evidence is Evidence<T> => evidence !== null)
}

test('officer-high-volume-messages', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  const listResponses: Array<Promise<Evidence<{
    count: number
    results: unknown[]
  }> | null>> = []
  const searchResponses: Array<Promise<Evidence<{
    count: number
    results: Array<Record<string, unknown>>
  }> | null>> = []
  let detailRequests = 0
  page.on('response', (response) => {
    const url = new URL(response.url())
    if (
      response.request().method() === 'GET'
      && url.pathname === '/api/staff/correspondence/'
      && url.searchParams.get('request') === fixtureRequestNumber
    ) {
      listResponses.push(responseEvidence<{
        count: number
        results: unknown[]
      }>(response))
    }
    if (
      response.request().method() === 'POST'
      && url.pathname === '/api/staff/correspondence/search/'
    ) {
      searchResponses.push(responseEvidence<{
        count: number
        results: Array<Record<string, unknown>>
      }>(response))
    }
  })
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (
      request.method() === 'GET'
      && /^\/api\/staff\/correspondence\/[0-9a-f-]+\/$/.test(url.pathname)
    ) detailRequests += 1
  })

  await login(page)
  await openFixtureRequest(page)
  await expect.poll(async () => (await delivered(listResponses)).length).toBe(1)
  const [firstListResponse] = await delivered(listResponses)
  if (!firstListResponse) throw new Error('the first message-list response was not delivered')

  expect(firstListResponse.payload.count).toBe(2000)
  expect(firstListResponse.payload.results.length).toBeLessThanOrEqual(50)
  expect(firstListResponse.bytes).toBeLessThanOrEqual(256 * 1024)
  expect(detailRequests).toBe(0)
  await expect(page.getByLabel('Message summaries').locator('button[aria-label^="Open "]')).toHaveCount(50)

  const firstSummary = page.getByLabel('Message summaries').locator('button[aria-label^="Open "]').first()
  await firstSummary.click()
  await expect(page.getByText('Synthetic casework message 1999.', { exact: false })).toBeVisible()
  await expect.poll(() => detailRequests).toBe(1)

  await page.getByRole('button', { name: 'Load more messages' }).click()
  await expect(page.getByText('Showing 51–100 of 2000 messages')).toBeVisible()
  await expect.poll(async () => (await delivered(listResponses)).length).toBe(2)
  const secondListResponse = (await delivered(listResponses))[1]
  if (!secondListResponse) throw new Error('the second message-list response was not delivered')
  expect(secondListResponse.payload.results.length).toBeLessThanOrEqual(50)
  expect(secondListResponse.bytes).toBeLessThanOrEqual(256 * 1024)

  await page.getByText('Filters and sort').click()
  await page.getByLabel('Direction').selectOption('inbound')
  await expect(page).toHaveURL(/message_direction=inbound/)
  await page.getByLabel('Matching state').selectOption('matched')
  await expect(page).toHaveURL(/message_matching_state=matched/)

  await searchMessages(page, primaryInboundSubject)
  await expect.poll(async () => (await delivered(searchResponses)).length).toBe(1)
  const [searchResponse] = await delivered(searchResponses)
  if (!searchResponse) throw new Error('the message-search response was not delivered')
  expect(searchResponse.payload.results.length).toBeLessThanOrEqual(50)
  expect(searchResponse.bytes).toBeLessThanOrEqual(256 * 1024)
  expect(searchResponse.payload.results.every((row) => (
    !('draft_body' in row)
    && !('approved_body' in row)
    && !('received_body' in row)
    && !('attachments' in row)
  ))).toBe(true)
  expect(page.url()).not.toContain(encodeURIComponent(primaryInboundSubject))
  expect(page.url()).not.toContain(primaryInboundSubject)

  await page.getByRole('button', { name: 'Threads' }).click()
  await expect(page).toHaveURL(/message_mode=threads/)
  const thread = page.getByText('Display-only provider thread').first()
  await expect(thread).toBeVisible()
  await thread.click()
  await expect(page.getByRole('button', { name: 'Back to thread summaries' })).toBeVisible()
  await expect(
    page.getByLabel('Message summaries').locator('button[aria-label^="Open "]').first(),
  ).toBeVisible()

  await page.screenshot({
    path: path.resolve('../.verification/officer-high-volume-messages.png'),
    fullPage: false,
  })
})
