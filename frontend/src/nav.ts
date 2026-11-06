// ABOUTME: Role-aware information architecture for the authenticated shell.
// ABOUTME: API role policies remain authoritative; this registry guards routes and navigation.
import type { StaffRole } from './api/auth'

const WORKFLOW_ROLES: StaffRole[] = ['records_officer', 'liaison', 'reviewer', 'admin']
const ALL_ROLES: StaffRole[] = [...WORKFLOW_ROLES, 'intake_clerk']

const STAGE_ROLES = {
  intake: WORKFLOW_ROLES,
  scope: ['records_officer'],
  collect: ['records_officer', 'liaison', 'admin'],
  review: ['records_officer', 'reviewer', 'admin'],
  redact: ['records_officer', 'reviewer'],
  release: ['records_officer', 'reviewer', 'admin'],
  close: WORKFLOW_ROLES,
} satisfies Record<string, StaffRole[]>

/** The tab each pre-rail case URL now opens (#45); old links redirect. */
export const LEGACY_CASE_TABS = {
  overview: 'intake',
  collection: 'collect',
  records: 'review',
  releases: 'release',
} as const satisfies Record<string, keyof typeof STAGE_ROLES>

/**
 * Who may open each case URL segment: the seven stages, the Messages and
 * Activity views beside the rail, and the legacy tab names, which carry the
 * roles of the stage they redirect to.
 */
export const CASE_TAB_ROLES = {
  ...STAGE_ROLES,
  messages: ['records_officer', 'liaison'],
  activity: WORKFLOW_ROLES,
  overview: STAGE_ROLES.intake,
  collection: STAGE_ROLES.collect,
  records: STAGE_ROLES.review,
  releases: STAGE_ROLES.release,
} satisfies Record<string, StaffRole[]>

type CaseTabKey = keyof typeof CASE_TAB_ROLES

function stageForTab(tab: string): string {
  return Object.hasOwn(LEGACY_CASE_TABS, tab)
    ? LEGACY_CASE_TABS[tab as keyof typeof LEGACY_CASE_TABS]
    : tab
}

/** The rail's name for a stage, or for the stage a legacy tab now opens. */
export function stageLabel(tab: string): string {
  const id = stageForTab(tab)
  return id.charAt(0).toUpperCase() + id.slice(1)
}

/**
 * A case URL for a tab name the server sends (a next action, a closure or
 * release blocker). The server still names the pre-rail tabs; this maps them
 * to their stage so the link lands without a redirect.
 */
export function caseTabPath(number: string, tab: string): string {
  const stage = stageForTab(tab)
  const view = tab === 'records' ? '?view=records' : ''
  return `/requests/${number}/${stage}${view}`
}

// hasOwn, not `in`: a path segment such as `toString` must not match a
// prototype member.
function isCaseTabKey(tab: string): tab is CaseTabKey {
  return Object.hasOwn(CASE_TAB_ROLES, tab)
}

export interface NavItem {
  label: string
  path: string
  roles: StaffRole[]
  description?: string
}

/**
 * Three ways in, nothing more: the day's cases, a new request, and settings.
 * Inbox, Review, Decisions, and Oversight still open from their links (Today
 * points to whatever in them needs a person); they are no longer menu stops.
 */
export const PRIMARY_NAV: NavItem[] = [
  { label: 'Today', path: '/today', roles: WORKFLOW_ROLES },
  {
    label: 'New request',
    path: '/staff-intake',
    roles: ['records_officer', 'intake_clerk'],
  },
  { label: 'Settings', path: '/configuration', roles: ALL_ROLES },
]

/** Pages reached from Today or Settings rather than the menu. */
const LINKED_ROUTES: [string, StaffRole[]][] = [
  ['/inbox', ['records_officer', 'liaison', 'admin', 'reviewer']],
  ['/review', ['records_officer', 'reviewer']],
  ['/decisions', ['records_officer', 'reviewer']],
  ['/oversight', ['records_officer', 'reviewer', 'admin']],
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
    label: 'Knowledge',
    path: '/configuration/knowledge',
    roles: ['records_officer'],
    description: 'Tell the intent advisory who held each role, which senders are noise, and how each system is searched.',
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
  {
    label: 'AI evaluation',
    path: '/oversight/ai-evaluation',
    roles: ['records_officer'],
  },
  {
    label: 'AI agreement',
    path: '/oversight/ai-agreement',
    roles: ['records_officer'],
  },
]

const ROUTE_ROLES = new Map<string, StaffRole[]>([
  ...PRIMARY_NAV.map((item) => [item.path, item.roles] as const),
  ...LINKED_ROUTES,
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
  if (WORKFLOW_ROLES.some((role) => roles.includes(role) && role !== 'admin')) return '/today'
  if (roles.includes('intake_clerk')) return '/staff-intake'
  if (roles.includes('admin')) return '/configuration'
  return '/'
}
