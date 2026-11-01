import path from 'node:path'

import { expect, test, type Response } from '@playwright/test'

import { login, openFixtureRequest } from './message-workspace.helpers.js'

async function boundedPageEvidence(
  response: Response,
  identity: 'id' | 'evidence_id' = 'id',
) {
  const body = await response.body()
  const payload = JSON.parse(body.toString('utf8')) as {
    results: Array<{ id?: unknown; evidence_id?: unknown }>
  }
  expect(payload.results).toHaveLength(50)
  const ids = payload.results.map((row) => row[identity])
  expect(ids.every((id) => typeof id === 'string' && id.length > 0)).toBe(true)
  expect(new Set(ids).size).toBe(50)
  return { ids: ids as string[], bytes: body.byteLength }
}

test('high-volume-performance', async ({ page }) => {
  // This journey deliberately traverses several 2,000-row cursor-backed
  // surfaces. Keep the per-assertion limits strict, but leave enough total
  // time for a loaded CI runner to complete every bounded-page check.
  test.setTimeout(180_000)
  let detailRequests = 0
  page.on('request', (request) => {
    if (/\/api\/staff\/correspondence\/[0-9a-f-]+\/$/.test(new URL(request.url()).pathname)) {
      detailRequests += 1
    }
  })

  await login(page)
  // Scoped to the case list: landing on the Inbox now issues its own
  // cross-case correspondence GET, which is not the page under test.
  const firstSummaryResponse = page.waitForResponse((response) => (
    response.request().method() === 'GET'
    && new URL(response.url()).pathname === '/api/staff/correspondence/'
    && new URL(response.url()).searchParams.has('request')
  ))
  await openFixtureRequest(page)
  const firstSummaryPage = await boundedPageEvidence(await firstSummaryResponse)
  const summaryPayloads = [firstSummaryPage.bytes]
  expect(summaryPayloads[0]).toBeLessThanOrEqual(256 * 1024)
  expect(detailRequests).toBe(0)

  const summaries = page.getByLabel('Message summaries').locator('button[aria-label^="Open "]')
  await expect(summaries).toHaveCount(50)
  const firstPage = firstSummaryPage.ids

  const secondSummaryResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/api/staff/correspondence/'
  ))
  await page.getByRole('button', { name: 'Load more messages' }).click()
  const secondSummaryPage = await boundedPageEvidence(await secondSummaryResponse)
  summaryPayloads.push(secondSummaryPage.bytes)
  await expect(page.getByText('Showing 51–100 of 2000 messages')).toBeVisible()
  await expect(summaries).toHaveCount(50)
  const secondPage = secondSummaryPage.ids
  expect(new Set([...firstPage, ...secondPage]).size).toBe(100)

  const thirdSummaryResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/api/staff/correspondence/'
  ))
  await page.getByRole('button', { name: 'Load more messages' }).click()
  const thirdSummaryPage = await boundedPageEvidence(await thirdSummaryResponse)
  summaryPayloads.push(thirdSummaryPage.bytes)
  await expect(page.getByText('Showing 101–150 of 2000 messages')).toBeVisible()
  await expect(summaries).toHaveCount(50)
  const thirdPage = thirdSummaryPage.ids
  expect(new Set([...firstPage, ...secondPage, ...thirdPage]).size).toBe(150)
  expect(summaryPayloads.every((bytes) => bytes <= 256 * 1024)).toBe(true)

  await page.getByRole('button', { name: 'Show newer messages' }).click()
  await expect(page.getByText('Showing 51–100 of 2000 messages')).toBeVisible()
  await expect(summaries).toHaveCount(50)

  const firstThreadResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/api/staff/correspondence/threads/'
  ))
  await page.getByRole('button', { name: 'Threads' }).click()
  const firstThreadPage = await boundedPageEvidence(
    await firstThreadResponse,
    'evidence_id',
  )
  summaryPayloads.push(firstThreadPage.bytes)
  const threads = page.locator('button').filter({ hasText: 'Display-only provider thread' })
  await expect(threads).toHaveCount(50, { timeout: 30_000 })
  const firstThreads = firstThreadPage.ids
  const secondThreadResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/api/staff/correspondence/threads/'
  ))
  await page.getByRole('button', { name: 'Load more threads' }).click()
  const secondThreadPage = await boundedPageEvidence(
    await secondThreadResponse,
    'evidence_id',
  )
  summaryPayloads.push(secondThreadPage.bytes)
  await expect(page.getByText(/^Showing 51–100 of \d+ threads$/)).toBeVisible()
  await expect(threads).toHaveCount(50)
  const secondThreads = secondThreadPage.ids
  expect(new Set([...firstThreads, ...secondThreads]).size).toBe(100)

  const firstTransportResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/api/staff/correspondence/'
  ))
  await page.goto('/configuration/correspondence')
  const firstTransportSummaryPage = await boundedPageEvidence(await firstTransportResponse)
  summaryPayloads.push(firstTransportSummaryPage.bytes)
  const transportRows = page.getByRole('table').getByRole('row')
  await expect(transportRows).toHaveCount(51)
  await expect(page.getByText('Showing 1–50 of 1998 messages')).toBeVisible()
  const firstTransportPage = firstTransportSummaryPage.ids
  const secondTransportResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/api/staff/correspondence/'
  ))
  await page.getByRole('button', { name: 'Load more messages' }).click()
  const secondTransportSummaryPage = await boundedPageEvidence(await secondTransportResponse)
  summaryPayloads.push(secondTransportSummaryPage.bytes)
  await expect(page.getByText('Showing 51–100 of 1998 messages')).toBeVisible()
  await expect(transportRows).toHaveCount(51)
  const secondTransportPage = secondTransportSummaryPage.ids
  expect(new Set([...firstTransportPage, ...secondTransportPage]).size).toBe(100)
  expect(summaryPayloads.every((bytes) => bytes <= 256 * 1024)).toBe(true)
  expect(detailRequests).toBe(0)

  await page.screenshot({
    path: path.resolve('../.verification/high-volume-performance.png'),
    fullPage: false,
  })
})
