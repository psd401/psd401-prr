// ABOUTME: Rendered allow-matrix coverage for every canonical shell route.
// ABOUTME: Route guards must mount authorized children for each independent role.
import { render, screen } from '@testing-library/react'
import { Outlet } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import type { StaffRole } from './api/auth'
import { CONFIGURATION_NAV, PRIMARY_NAV } from './nav'

const session = vi.hoisted(() => ({ roles: [] as StaffRole[] }))

vi.mock('./api/auth', () => ({
  useMe: () => ({
    isPending: false,
    isError: false,
    data: {
      authenticated: true,
      username: 'route-test-user',
      email: 'route-test-user@psd401.net',
      first_name: 'Route',
      last_name: 'Tester',
      is_superuser: false,
      roles: session.roles,
    },
  }),
}))

vi.mock('./layout/AppLayout', () => ({
  AppLayout: () => <Outlet />,
  PageHeader: ({ title }: { title: string }) => <h1>{title}</h1>,
}))
vi.mock('./pages/WorkItem', () => ({
  WorkItemDetail: () => <h1>Work detail child</h1>,
}))
vi.mock('./pages/Requests', () => ({ Requests: () => <h1>Cases child</h1> }))
vi.mock('./pages/Inbox', () => ({ Inbox: () => <h1>Inbox child</h1> }))
vi.mock('./pages/Review', () => ({ Review: () => <h1>Review child</h1> }))
vi.mock('./pages/Decisions', () => ({ Decisions: () => <h1>Decisions child</h1> }))
vi.mock('./pages/StaffIntake', () => ({
  StaffIntake: () => <h1>Staff Intake child</h1>,
}))
vi.mock('./pages/SectionIndex', () => ({
  ConfigurationIndex: () => <h1>Configuration child</h1>,
  OversightIndex: () => <h1>Oversight child</h1>,
}))
vi.mock('./pages/AgentAccess', () => ({
  AgentAccess: () => <h1>Agent access child</h1>,
}))
vi.mock('./pages/IdentitySettings', () => ({
  IdentitySettings: () => <h1>Identity and integrations child</h1>,
}))
vi.mock('./pages/Registry', () => ({
  Registry: () => <h1>System registry child</h1>,
}))
vi.mock('./pages/MessageTemplates', () => ({
  MessageTemplates: () => <h1>Message templates child</h1>,
}))
vi.mock('./pages/Exemptions', () => ({
  Exemptions: () => <h1>Exemptions child</h1>,
}))
vi.mock('./pages/Knowledge', () => ({
  Knowledge: () => <h1>Knowledge child</h1>,
}))
vi.mock('./pages/CounselDomains', () => ({
  CounselDomains: () => <h1>District counsel domains child</h1>,
}))
vi.mock('./pages/DistrictSettings', () => ({
  DistrictSettings: () => <h1>District settings child</h1>,
}))
vi.mock('./pages/Correspondence', () => ({
  Correspondence: () => <h1>Message operations child</h1>,
}))

import App from './App'

const HEADING_BY_ROUTE: Record<string, string> = {
  '/cases': 'Cases child',
  '/inbox': 'Inbox child',
  '/review': 'Review child',
  '/decisions': 'Decisions child',
  '/staff-intake': 'Staff Intake child',
  '/oversight': 'Oversight child',
  '/configuration': 'Configuration child',
  '/configuration/agent-access': 'Agent access child',
  '/configuration/identity': 'Identity and integrations child',
  '/configuration/registry': 'System registry child',
  '/configuration/message-templates': 'Message templates child',
  '/configuration/exemptions': 'Exemptions child',
  '/configuration/knowledge': 'Knowledge child',
  '/configuration/counsel-domains': 'District counsel domains child',
  '/configuration/settings': 'District settings child',
  '/configuration/correspondence': 'Message operations child',
}

const AUTHORIZED_ROUTES = [...PRIMARY_NAV, ...CONFIGURATION_NAV].flatMap((item) =>
  item.roles.map((role) => [role, item.path, HEADING_BY_ROUTE[item.path]] as const),
)

afterEach(() => {
  window.history.replaceState({}, '', '/')
  session.roles = []
})

it.each(AUTHORIZED_ROUTES)(
  '%s mounts the authorized child at %s',
  async (role, path, heading) => {
    session.roles = [role]
    window.history.replaceState({}, '', path)

    render(<App />)

    expect(await screen.findByRole('heading', { name: heading })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Not authorized' }))
      .not.toBeInTheDocument()
  },
)
