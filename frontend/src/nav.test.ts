// ABOUTME: Table-driven shell authorization and landing-page contracts.
// ABOUTME: These mirror backend role policy without replacing API enforcement.
import { describe, expect, it } from 'vitest'
import type { StaffRole } from './api/auth'
import {
  CONFIGURATION_NAV,
  PRIMARY_NAV,
  caseTabPath,
  defaultLanding,
  routeIsAuthorized,
  visibleNav,
} from './nav'

interface RoleCase {
  name: string
  roles: StaffRole[]
  labels: string[]
  landing: string
}

const ROLE_CASES: RoleCase[] = [
  {
    name: 'Records Officer',
    roles: ['records_officer'],
    labels: ['Inbox', 'Review', 'Decisions', 'Staff Intake', 'Oversight', 'Configuration'],
    landing: '/inbox',
  },
  {
    name: 'Department Liaison',
    roles: ['liaison'],
    labels: ['Inbox', 'Configuration'],
    landing: '/inbox',
  },
  {
    name: 'Reviewer',
    roles: ['reviewer'],
    labels: ['Inbox', 'Review', 'Decisions', 'Oversight', 'Configuration'],
    landing: '/review',
  },
  {
    name: 'Intake Clerk',
    roles: ['intake_clerk'],
    labels: ['Staff Intake', 'Configuration'],
    landing: '/staff-intake',
  },
  {
    name: 'Application Admin',
    roles: ['admin'],
    labels: ['Inbox', 'Oversight', 'Configuration'],
    landing: '/configuration',
  },
  {
    name: 'Officer and admin',
    roles: ['records_officer', 'admin'],
    labels: ['Inbox', 'Review', 'Decisions', 'Staff Intake', 'Oversight', 'Configuration'],
    landing: '/inbox',
  },
  {
    name: 'Liaison and reviewer',
    roles: ['liaison', 'reviewer'],
    labels: ['Inbox', 'Review', 'Decisions', 'Oversight', 'Configuration'],
    landing: '/inbox',
  },
  {
    name: 'Reviewer and admin',
    roles: ['reviewer', 'admin'],
    labels: ['Inbox', 'Review', 'Decisions', 'Oversight', 'Configuration'],
    landing: '/review',
  },
  {
    name: 'Intake and admin',
    roles: ['intake_clerk', 'admin'],
    labels: ['Inbox', 'Staff Intake', 'Oversight', 'Configuration'],
    landing: '/staff-intake',
  },
]

describe('role-aware shell contract', () => {
  it.each(ROLE_CASES)('$name sees only the permitted primary navigation', ({ roles, labels }) => {
    expect(visibleNav(PRIMARY_NAV, roles).map((item) => item.label)).toEqual(labels)
  })

  it.each(ROLE_CASES)('$name receives the correct default landing page', ({ roles, landing }) => {
    expect(defaultLanding(roles)).toBe(landing)
  })

  it('keeps personal Agent Access separate from admin-only identity management', () => {
    expect(routeIsAuthorized('/configuration/agent-access', ['intake_clerk'])).toBe(true)
    expect(routeIsAuthorized('/configuration/identity', ['intake_clerk'])).toBe(false)
    expect(routeIsAuthorized('/configuration/correspondence', ['intake_clerk'])).toBe(false)
    expect(routeIsAuthorized('/configuration/identity', ['admin'])).toBe(true)
  })

  it('authorizes case tabs by their own roles and denies unknown tab segments', () => {
    expect(routeIsAuthorized('/requests/P000123-101426/messages', ['liaison'])).toBe(true)
    expect(routeIsAuthorized('/requests/P000123-101426/messages', ['reviewer'])).toBe(false)
    expect(routeIsAuthorized('/requests/P000123-101426/unknown', ['admin'])).toBe(false)
    expect(routeIsAuthorized('/requests/P000123-101426/toString', ['admin'])).toBe(false)
    expect(routeIsAuthorized('/requests/P000123-101426/constructor', ['admin'])).toBe(false)
  })

  it('maps server tab names to stage URLs', () => {
    expect(caseTabPath('P1', 'overview')).toBe('/requests/P1/intake')
    expect(caseTabPath('P1', 'records')).toBe('/requests/P1/review?view=records')
    expect(caseTabPath('P1', 'review')).toBe('/requests/P1/review')
    expect(caseTabPath('P1', 'messages')).toBe('/requests/P1/messages')
  })

  it('authorizes the stage rail and keeps legacy tab URLs openable for redirect', () => {
    expect(routeIsAuthorized('/requests/P000123-101426/scope', ['records_officer'])).toBe(true)
    expect(routeIsAuthorized('/requests/P000123-101426/scope', ['liaison'])).toBe(false)
    expect(routeIsAuthorized('/requests/P000123-101426/collect', ['liaison'])).toBe(true)
    expect(routeIsAuthorized('/requests/P000123-101426/redact', ['admin'])).toBe(false)
    expect(routeIsAuthorized('/requests/P000123-101426/close', ['liaison'])).toBe(true)
    expect(routeIsAuthorized('/requests/P000123-101426/collection', ['liaison'])).toBe(true)
    expect(routeIsAuthorized('/requests/P000123-101426/records', ['reviewer'])).toBe(true)
    expect(routeIsAuthorized('/requests/P000123-101426/overview', ['admin'])).toBe(true)
  })

  it('keeps one work surface: Inbox absorbed My Work', () => {
    const paths = PRIMARY_NAV.map((item) => item.path)

    expect(paths).not.toContain('/my-work')
    expect(paths[0]).toBe('/inbox')
    expect(visibleNav(PRIMARY_NAV, ['liaison']).map((item) => item.path))
      .toContain('/inbox')
  })

  it('advertises correspondence destinations without the compatibility URL', () => {
    const paths = [...PRIMARY_NAV, ...CONFIGURATION_NAV]
      .map((item) => item.path)

    expect(paths).toContain('/inbox')
    expect(paths).toContain('/configuration/correspondence')
    expect(paths).not.toContain('/correspondence')
  })

  it('defines every top-level and Configuration route in the authorization registry', () => {
    const routes = [
      ...PRIMARY_NAV.map((item) => item.path),
      ...CONFIGURATION_NAV.map((item) => item.path),
    ]
    for (const path of routes) {
      expect(routeIsAuthorized(path, ['records_officer', 'intake_clerk', 'liaison', 'reviewer', 'admin'])).toBe(true)
    }
  })
})
