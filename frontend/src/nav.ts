// ABOUTME: Role-aware information architecture for the authenticated shell.
// ABOUTME: API role policies remain authoritative; this registry guards routes and navigation.
import type { StaffRole } from './api/auth'

const WORKFLOW_ROLES: StaffRole[] = ['records_officer', 'liaison', 'reviewer', 'admin']
const ALL_ROLES: StaffRole[] = [...WORKFLOW_ROLES, 'intake_clerk']

export const CASE_TAB_ROLES = {
  overview: WORKFLOW_ROLES,
  messages: ['records_officer', 'liaison'],
  collection: ['records_officer', 'liaison', 'admin'],
  records: ['records_officer', 'reviewer', 'admin'],
  releases: ['records_officer', 'reviewer', 'admin'],
  activity: WORKFLOW_ROLES,
} satisfies Record<string, StaffRole[]>

type CaseTabKey = keyof typeof CASE_TAB_ROLES

// hasOwn, not `in`: a path segment such as `toString` must not match a
// prototype member.
function isCaseTabKey(tab: string): tab is CaseTabKey {
  return Object.hasOwn(CASE_TAB_ROLES, tab)
}

export type QueueCountKey = 'inbox' | 'review'

export interface NavItem {
  label: string
  path: string
  roles: StaffRole[]
  countKey?: QueueCountKey
  description?: string
}

export const PRIMARY_NAV: NavItem[] = [
  // One work surface, not two. My Work and Inbox split the same casework
  // between screens that were each incomplete: the Inbox listed messages an
  // officer could not act on, and My Work read empty for anyone with nothing
  // personally assigned. Inbox now carries both, grouped by decision.
  //
  // Cases was the second half of that same split. It listed requests while the
  // Inbox listed work, so an officer needing "every case, open and closed" had
  // to leave the surface they work in. It is now the "All cases" view here,
  // and reviewers reach the Inbox for that view alone -- the server scopes
  // which cases they see and blanks requester identity for them, so this is a
  // way in rather than a way around.
  {
    label: 'Inbox',
    path: '/inbox',
    roles: ['records_officer', 'liaison', 'admin', 'reviewer'],
    countKey: 'inbox',
  },
  {
    label: 'Review',
    path: '/review',
    roles: ['records_officer', 'reviewer'],
    countKey: 'review',
  },
  {
    label: 'Staff Intake',
    path: '/staff-intake',
    roles: ['records_officer', 'intake_clerk'],
  },
  {
    label: 'Oversight',
    path: '/oversight',
    roles: ['records_officer', 'reviewer', 'admin'],
  },
  { label: 'Configuration', path: '/configuration', roles: ALL_ROLES },
]

export const CONFIGURATION_NAV: NavItem[] = [
  {
    label: 'Agent access',
    path: '/configuration/agent-access',
    roles: ALL_ROLES,
    description: 'Manage your own personal agent credentials.',
  },
  {
    label: 'Identity and integrations',
    path: '/configuration/identity',
    roles: ['admin'],
    description: 'Configure staff identity mappings and integration principals.',
  },
  {
    label: 'System registry',
    path: '/configuration/registry',
    roles: WORKFLOW_ROLES,
    description: 'Review district record systems, routing, and custodians.',
  },
  {
    label: 'Message templates',
    path: '/configuration/message-templates',
    roles: WORKFLOW_ROLES,
    description: 'Inspect versioned requester-correspondence templates.',
  },
  {
    label: 'Exemptions',
    path: '/configuration/exemptions',
    roles: ['records_officer', 'reviewer', 'admin'],
    description: 'Maintain the citation-backed exemption taxonomy.',
  },
  {
    label: 'District counsel domains',
    path: '/configuration/counsel-domains',
    roles: ['admin'],
    description: 'Name the district’s own law firms so their records are separated for review.',
  },
  {
    label: 'District settings',
    path: '/configuration/settings',
    roles: ['admin'],
    description: 'Turn district switches on or off, with a reason and a permanent history.',
  },
  {
    label: 'Message operations',
    path: '/configuration/correspondence',
    roles: ['records_officer', 'admin'],
    description: 'Inspect transport readiness and sanitized delivery operations.',
  },
]

export const OVERSIGHT_NAV: NavItem[] = [
  {
    label: 'JLARC reporting',
    path: '/oversight/reporting',
    roles: ['records_officer', 'reviewer', 'admin'],
  },
  {
    label: 'Audit log',
    path: '/oversight/audit',
    roles: ['records_officer', 'admin'],
  },
]

const ROUTE_ROLES = new Map<string, StaffRole[]>([
  ...PRIMARY_NAV.map((item) => [item.path, item.roles] as const),
  ...CONFIGURATION_NAV.map((item) => [item.path, item.roles] as const),
  ...OVERSIGHT_NAV.map((item) => [item.path, item.roles] as const),
  ['/requests', WORKFLOW_ROLES],
  ['/requests/:number', WORKFLOW_ROLES],
  ['/work/:id', WORKFLOW_ROLES],
])

export function visibleNav(items: NavItem[], roles: StaffRole[]): NavItem[] {
  const allowed = new Set(roles)
  return items.filter((item) => item.roles.some((role) => allowed.has(role)))
}

function routePattern(path: string): string {
  if (path.startsWith('/work/')) return '/work/:id'
  if (path.startsWith('/requests/')) return '/requests/:number'
  return path.replace(/\/$/, '') || '/'
}

export function routeIsAuthorized(path: string, roles: StaffRole[]): boolean {
  const caseMatch = path.replace(/\/$/, '').match(/^\/requests\/[^/]+\/([^/]+)$/)
  if (caseMatch) {
    const tab = caseMatch[1]
    if (tab === undefined || !isCaseTabKey(tab)) return false
    return CASE_TAB_ROLES[tab].some((role) => roles.includes(role))
  }
  const allowed = ROUTE_ROLES.get(routePattern(path))
  if (!allowed) return false
  return allowed.some((role) => roles.includes(role))
}

export function defaultLanding(roles: StaffRole[]): string {
  if (roles.includes('records_officer') || roles.includes('liaison')) return '/inbox'
  if (roles.includes('reviewer')) return '/review'
  if (roles.includes('intake_clerk')) return '/staff-intake'
  if (roles.includes('admin')) return '/configuration'
  return '/'
}
