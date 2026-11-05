import { writeFile } from 'node:fs/promises'
import path from 'node:path'

import { AxeBuilder } from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

import {
  fixtureRequestNumber,
  login,
  primaryInboundSubject,
  searchMessages,
} from './message-workspace.helpers.js'

interface AuditRecord {
  surface: string
  violation_count: number
  violations: Array<{
    id: string
    impact: string | null
    help: string
    help_url: string
    nodes: Array<{
      target: string[]
      html: string
      failure_summary: string
    }>
  }>
  serious_or_critical: number
  visible_loading_labels: string[]
}

interface AutomatedCheckRecord {
  surface: string
  responsive_320_css_pixels: boolean
  zoom_200_percent: boolean
  reduced_motion: boolean
  forced_colors: boolean
  zoom_method: string
  aria_snapshot: string
}

async function visibleLoadingLabels(page: Page) {
  return page.locator('main *').evaluateAll((elements) => (
    elements
      .filter((element) => (
        element.children.length === 0
        && /^Loading\b/.test(element.textContent?.trim() ?? '')
        && (element as HTMLElement).offsetParent !== null
      ))
      .map((element) => element.textContent?.trim() ?? '')
  ))
}

async function audit(page: Page, surface: string, records: AuditRecord[]) {
  await page.waitForFunction(() => document.getAnimations().every((animation) => (
    animation.playState !== 'running'
    || animation.effect?.getTiming().iterations === Number.POSITIVE_INFINITY
  )), undefined, { timeout: 5_000 })
  await expect.poll(
    () => visibleLoadingLabels(page),
    {
      message: `${surface} was not ready before axe`,
      timeout: 10_000,
    },
  ).toEqual([])
  const result = await new AxeBuilder({ page }).analyze()
  const severe = result.violations.filter(
    (violation) => violation.impact === 'serious' || violation.impact === 'critical',
  )
  const loadingAfterAudit = await visibleLoadingLabels(page)
  records.push({
    surface,
    violation_count: result.violations.length,
    violations: result.violations.map((violation) => ({
      id: violation.id,
      impact: violation.impact ?? null,
      help: violation.help,
      help_url: violation.helpUrl,
      nodes: violation.nodes.map((node) => ({
        target: node.target.map(String),
        html: node.html,
        failure_summary: node.failureSummary ?? '',
      })),
    })),
    serious_or_critical: severe.length,
    visible_loading_labels: loadingAfterAudit,
  })
  expect(severe, JSON.stringify(severe, null, 2)).toEqual([])
  expect(loadingAfterAudit, `${surface} started loading during axe`).toEqual([])
}

async function auditDialog(
  page: Page,
  triggerName: string | RegExp,
  dialogName: string | RegExp,
  surface: string,
  records: AuditRecord[],
) {
  await page.getByRole('button', { name: triggerName }).click()
  const dialog = page.getByRole('dialog', { name: dialogName })
  await expect(dialog).toBeVisible()
  await audit(page, surface, records)
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
}

async function checkResponsiveSurface(
  page: Page,
  route: string,
  surface: string,
  screenshotStem: string,
  readyHeading: string,
  readyText: string | null,
  checks: AutomatedCheckRecord[],
) {
  await page.emulateMedia({ reducedMotion: 'reduce', forcedColors: 'active' })
  await page.setViewportSize({ width: 320, height: 800 })
  await page.goto(route)
  await expect(page.getByRole('main')).toBeVisible()
  await expect(page.getByRole('heading', { name: readyHeading, exact: true })).toBeVisible()
  if (readyText) {
    await expect(page.getByText(readyText, { exact: false }).first()).toBeVisible()
  }
  await expect(page.getByText('Loading case…').filter({ visible: true })).toHaveCount(0)
  const responsive = await page.evaluate(() => (
    document.documentElement.scrollWidth <= document.documentElement.clientWidth
  ))
  const reducedMotion = await page.evaluate(() => document.getAnimations().every((animation) => {
    const duration = animation.effect?.getTiming().duration
    return typeof duration !== 'number' || duration <= 1
  }))
  const forcedColors = await page.evaluate(() => matchMedia('(forced-colors: active)').matches)
  const ariaSnapshot = await page.getByRole('main').ariaSnapshot()
  expect(responsive).toBe(true)
  expect(reducedMotion).toBe(true)
  expect(forcedColors).toBe(true)
  await page.screenshot({
    path: path.resolve(`../.verification/${screenshotStem}-320-forced-colors.png`),
    fullPage: false,
  })

  await page.emulateMedia({ reducedMotion: 'no-preference', forcedColors: 'none' })
  // A 320 CSS-pixel layout viewport is the reflow-equivalent of a 640px
  // reference viewport at 200% browser zoom. Keep this separate from the
  // forced-colors capture above so both media states are evidenced.
  await page.setViewportSize({ width: 320, height: 450 })
  const zoomResult = await page.evaluate(() => ({
    fits: document.documentElement.scrollWidth <= document.documentElement.clientWidth,
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
    overflowingElements: Array.from(document.querySelectorAll<HTMLElement>('body *'))
      .map((element) => ({
        tag: element.tagName.toLowerCase(),
        text: (element.textContent ?? '').trim().slice(0, 80),
        right: Math.round(element.getBoundingClientRect().right),
        width: Math.round(element.getBoundingClientRect().width),
        className: element.className,
      }))
      .filter((element) => element.right > document.documentElement.clientWidth + 1)
      .slice(0, 20),
  }))
  const zoom = zoomResult.fits
  expect(zoom, JSON.stringify(zoomResult, null, 2)).toBe(true)
  await page.screenshot({
    path: path.resolve(`../.verification/${screenshotStem}-200-percent-zoom.png`),
    fullPage: false,
  })
  checks.push({
    surface,
    responsive_320_css_pixels: responsive,
    zoom_200_percent: zoom,
    reduced_motion: reducedMotion,
    forced_colors: forcedColors,
    zoom_method: '640px reference viewport reflowed to 320 CSS px',
    aria_snapshot: ariaSnapshot,
  })
}

// Nexus themes (psd-dev-standards 10, rule 3). The first is the default the
// responsive and forced-colors checks run in; every theme gets the full axe pass.
const THEMES = ['nexus', 'commons', 'dark', 'commons-dark', 'high-contrast'] as const
type Theme = (typeof THEMES)[number]

async function useTheme(page: Page, theme: Theme) {
  // The same per-browser choice the Appearance picker stores.
  await page.addInitScript((value) => {
    window.localStorage.setItem('prr-theme', value)
  }, theme)
}

async function auditSurfaces(page: Page, theme: Theme, records: AuditRecord[]) {
  await page.goto('http://127.0.0.1:8000/')
  await page.goto('/')
  await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
  await audit(page, 'login', records)

  await login(page)
  await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()
  await audit(page, 'shell and Inbox', records)

  await page.goto('/inbox?view=team')
  await expect(page.getByText('Complete record search').first()).toBeVisible()
  await audit(page, 'Inbox assignment queues', records)

  // Every stage of the rail (#45) and the Messages view beside it.
  for (const tab of [
    'intake', 'scope', 'collect', 'review', 'redact', 'release', 'close', 'messages',
  ]) {
    const query = tab === 'review' ? '?view=records' : ''
    await page.goto(`/requests/${fixtureRequestNumber}/${tab}${query}`)
    await expect(page.getByRole('heading', {
      name: tab.charAt(0).toUpperCase() + tab.slice(1),
      level: 2,
      exact: true,
    })).toBeVisible()
    if (tab === 'intake') {
      await expect(page.getByText('Legal record of the request as received', { exact: false }))
        .toBeVisible()
    } else if (tab === 'messages') {
      await expect(page.getByText('Showing 1–50 of 2000 messages')).toBeVisible()
    } else if (tab === 'scope') {
      const vault = page.getByText('Google Vault scope count')
      if (!(await vault.isVisible())) {
        await page.getByText('Scope: request intent and Vault count').click()
      }
      await expect(vault).toBeVisible()
    } else if (tab === 'collect') {
      await expect(page.getByRole('list', { name: 'Collections' })).toBeVisible()
      await expect(page.getByRole('cell', { name: 'e2e-liaison' })).toBeVisible()
    } else if (tab === 'review') {
      await expect(page.getByText('synthetic-review-record.pdf')).toBeVisible()
    } else if (tab === 'redact') {
      await expect(page.getByRole('region', { name: 'Redact decision' })).toBeVisible()
    } else if (tab === 'release') {
      await expect(page.getByText(
        'Installments appear when records are released in batches.',
      )).toBeVisible()
    } else {
      await expect(page.getByText('Read-only blocker report.', { exact: false })).toBeVisible()
    }
    await audit(page, `case ${tab}`, records)
  }

  await page.goto(`/requests/${fixtureRequestNumber}/messages`)
  await searchMessages(page, primaryInboundSubject)
  await page.getByRole('button', { name: `Open ${primaryInboundSubject}` }).click()
  await expect(page.getByRole('region', {
    name: `${primaryInboundSubject} message body`,
  })).toContainText('Synthetic casework message 0000.')
  await audit(page, 'message detail', records)

  await auditDialog(
    page,
    'No action required',
    'Record no action required',
    'No action confirmation dialog',
    records,
  )

  await auditDialog(
    page,
    'Compose message',
    'Compose correspondence',
    'Compose correspondence dialog',
    records,
  )

  await page.getByRole('button', { name: 'Clear search' }).click()
  await expect(page.getByText('Showing 1–50 of 2000 messages')).toBeVisible()
  await page.getByText('Filters and sort').click()
  await page.getByLabel('Sort order').selectOption('oldest')
  await page.getByRole('button', { name: 'Open Synthetic outbound message 0001' }).click()
  await auditDialog(
    page,
    'Edit draft',
    'Edit correspondence draft',
    'Edit draft dialog',
    records,
  )
  await auditDialog(
    page,
    'Preview exact message',
    'Exact delivery preview',
    'Exact delivery preview dialog',
    records,
  )
  await auditDialog(
    page,
    'Approve and send',
    'Send this message to the requester',
    'Approve and send confirmation dialog',
    records,
  )
  await page.getByRole('button', { name: 'Open Synthetic outbound message 0012' }).click()
  await auditDialog(
    page,
    'Send message',
    'Send this approved message to the requester',
    'Send approved message confirmation dialog',
    records,
  )
  await auditDialog(
    page,
    'Cancel Synthetic outbound message 0012',
    'Cancel correspondence',
    'Cancel correspondence dialog',
    records,
  )
  await page.getByRole('button', { name: 'Open Synthetic outbound message 0004' }).click()
  await auditDialog(
    page,
    'Retry Synthetic outbound message 0004',
    'Retry correspondence delivery',
    'Retry correspondence dialog',
    records,
  )
  await auditDialog(
    page,
    'Resolve clarification',
    'Resolve clarification',
    'Resolve clarification dialog',
    records,
  )

  await page.goto(`/requests/${fixtureRequestNumber}/intake`)
  await auditDialog(page, 'Close case…', 'Close case', 'Close case dialog', records)
  await auditDialog(page, 'Log time', 'Log time', 'Log time dialog', records)

  await page.goto(`/requests/${fixtureRequestNumber}/collect`)
  await auditDialog(
    page,
    'Add search assignment',
    /Add search assignment/,
    'Add search assignment dialog',
    records,
  )
  await page.getByRole('cell', { name: 'e2e-liaison' }).click()
  const taskDialog = page.getByRole('dialog', { name: /Task — request/ })
  await expect(taskDialog).toBeVisible()
  await audit(page, 'collection task dialog', records)
  await page.keyboard.press('Escape')

  const excludeCamera = page.getByRole('button', { name: 'Exclude' }).first()
  if (await excludeCamera.count()) {
    await excludeCamera.click()
    const excludeDialog = page.getByRole('dialog', { name: /Exclude / })
    await expect(excludeDialog).toBeVisible()
    await audit(page, 'camera exclusion dialog', records)
    await page.keyboard.press('Escape')
  }

  await page.goto(`/requests/${fixtureRequestNumber}/review?view=records`)
  await page.getByRole('row').filter({ hasText: 'synthetic-review-record.pdf' }).click()
  const reviewDialog = page.getByRole('dialog', { name: /synthetic-review-record\.pdf/ })
  await expect(reviewDialog).toBeVisible()
  await audit(page, 'record review dialog', records)
  await page.keyboard.press('Escape')

  await page.goto(`/requests/${fixtureRequestNumber}/release`)
  await expect(page.getByRole('heading', {
    name: 'Release',
    level: 2,
    exact: true,
  })).toBeVisible()
  // With no fee on the case, the fee panels are folded away (#47).
  await page.getByText('Fees and deposits — no fee applies').click()
  await auditDialog(
    page,
    'Activate fee schedule',
    'Activate counsel-approved fee schedule',
    'Activate fee schedule dialog',
    records,
  )
  await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
}

test('accessibility-audit', async ({ page }) => {
  test.setTimeout(120_000)
  const records: AuditRecord[] = []
  const automatedChecks: AutomatedCheckRecord[] = []

  await useTheme(page, 'nexus')
  await auditSurfaces(page, 'nexus', records)

  // Two Inbox layouts, not one: the assignment queues render work rows (the
  // layout the retired My Work screen used to contribute here) and the
  // default view renders message triage rows.
  await checkResponsiveSurface(
    page,
    '/inbox?view=team',
    'Inbox work queues',
    'accessibility-work-queues',
    'Inbox',
    'Complete record search',
    automatedChecks,
  )
  await checkResponsiveSurface(
    page,
    '/inbox',
    'Inbox',
    'accessibility-inbox',
    'Inbox',
    null,
    automatedChecks,
  )
  // The Cases table is wider than 320 CSS px on purpose: it scrolls inside the
  // table's own container rather than widening the page.
  // What this asserts is that nothing on the page pushes the *document* wider
  // at 320px, which is where a dense row's action cluster is most at risk.
  await checkResponsiveSurface(
    page,
    '/inbox?view=cases',
    'Inbox',
    'accessibility-cases',
    'Inbox',
    // Readiness is a column header: it is the one string that only appears
    // once the case table itself has rendered, which is what this surface is
    // auditing.
    'Next due',
    automatedChecks,
  )
  await checkResponsiveSurface(
    page,
    `/requests/${fixtureRequestNumber}/intake`,
    'case overview',
    'accessibility-case-overview',
    'Intake',
    'Legal record of the request as received',
    automatedChecks,
  )
  expect(automatedChecks).toHaveLength(4)

  await writeFile(
    path.resolve('../.verification/axe-report.json'),
    `${JSON.stringify({
      source_sha: process.env.PRR_SOURCE_SHA ?? 'local-uncommitted',
      serious_or_critical: 0,
      surfaces: records,
      automated_checks: automatedChecks,
      manual_screen_reader_check: 'pending named staff validation',
    }, null, 2)}\n`,
    'utf8',
  )
})

for (const theme of THEMES.slice(1)) {
  test(`accessibility-audit in the ${theme} theme`, async ({ page }) => {
    test.setTimeout(120_000)
    const records: AuditRecord[] = []
    await useTheme(page, theme)
    await auditSurfaces(page, theme, records)
    // The same walk as the default theme, not a shortened one.
    expect(records.length).toBeGreaterThanOrEqual(20)
    await writeFile(
      path.resolve(`../.verification/axe-report-${theme}.json`),
      `${JSON.stringify({
        source_sha: process.env.PRR_SOURCE_SHA ?? 'local-uncommitted',
        theme,
        serious_or_critical: 0,
        surfaces: records,
      }, null, 2)}\n`,
      'utf8',
    )
  })
}
