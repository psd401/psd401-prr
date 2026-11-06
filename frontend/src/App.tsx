// ABOUTME: Session gate and role-policy route table for public and staff screens.
// ABOUTME: Protected children are denied before mounting; APIs remain authoritative.
import type { ReactNode } from 'react'
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router'
import { useMe, type StaffRole } from './api/auth'
import { AppLayout, PageHeader } from './layout/AppLayout'
import { defaultLanding, routeIsAuthorized } from './nav'
import { CaseWorkspace } from './case/CaseWorkspace'
import {
  LegacyCorrespondenceRedirect,
  LegacyRequestRedirect,
} from './case/LegacyScopedRoutes'
import { AgentAccess } from './pages/AgentAccess'
import { AiAgreement } from './pages/AiAgreement'
import { AiEvaluation } from './pages/AiEvaluation'
import { Audit } from './pages/Audit'
import { Correspondence } from './pages/Correspondence'
import { Decisions } from './pages/Decisions'
import { Exemptions } from './pages/Exemptions'
import { CounselDomains } from './pages/CounselDomains'
import { DistrictSettings } from './pages/DistrictSettings'
import { Inbox } from './pages/Inbox'
import { IdentitySettings } from './pages/IdentitySettings'
import { Knowledge } from './pages/Knowledge'
import { Login } from './pages/Login'
import { MessageTemplates } from './pages/MessageTemplates'
import { WorkItemDetail } from './pages/WorkItem'
import { PublicRequestForm } from './pages/PublicRequestForm'
import { PublicRequestReceipt } from './pages/PublicRequestReceipt'
import { Registry } from './pages/Registry'
import { Reporting } from './pages/Reporting'
import { Review } from './pages/Review'
import { ConfigurationIndex, OversightIndex } from './pages/SectionIndex'
import { Today } from './pages/Today'
import { StaffIntake } from './pages/StaffIntake'
import { EmptyState, Spinner } from './ui'

function NotFound() {
  return (
    <>
      <PageHeader title="Not found" />
      <EmptyState>No screen lives at this address.</EmptyState>
    </>
  )
}

function NotAuthorized() {
  return (
    <>
      <PageHeader title="Not authorized" />
      <EmptyState>Your current staff roles do not permit this screen.</EmptyState>
    </>
  )
}

function AuthorizedRoute({ roles, children }: { roles: StaffRole[]; children: ReactNode }) {
  const location = useLocation()
  return routeIsAuthorized(location.pathname, roles) ? children : <NotAuthorized />
}

function StaffApp() {
  const me = useMe()

  if (me.isPending) return <Spinner label="Checking session…" />
  if (me.isError || !me.data) return <Login />
  const user = me.data
  const guard = (element: ReactNode) => (
    <AuthorizedRoute roles={user.roles}>{element}</AuthorizedRoute>
  )

  return (
    <Routes>
      <Route element={<AppLayout user={user} />}>
        <Route index element={<Navigate to={defaultLanding(user.roles)} replace />} />

        <Route path="work/:id" element={guard(<WorkItemDetail roles={user.roles} />)} />
        <Route path="cases" element={<Navigate to="/inbox?view=cases" replace />} />
        <Route
          path="requests/:number"
          element={guard(<LegacyRequestRedirect />)}
        />
        <Route
          path="requests/:number/:tab"
          element={guard(<CaseWorkspace roles={user.roles} />)}
        />
        <Route path="today" element={guard(<Today />)} />
        <Route path="inbox" element={guard(<Inbox roles={user.roles} />)} />
        <Route path="review" element={guard(<Review />)} />
        <Route path="decisions" element={guard(<Decisions />)} />
        <Route path="staff-intake" element={guard(<StaffIntake />)} />

        <Route path="oversight" element={guard(<OversightIndex roles={user.roles} />)} />
        <Route path="oversight/reporting" element={guard(<Reporting roles={user.roles} />)} />
        <Route path="oversight/audit" element={guard(<Audit />)} />
        <Route path="oversight/ai-evaluation" element={guard(<AiEvaluation />)} />
        <Route path="oversight/ai-agreement" element={guard(<AiAgreement />)} />

        <Route
          path="configuration"
          element={guard(<ConfigurationIndex roles={user.roles} />)}
        />
        <Route path="configuration/agent-access" element={guard(<AgentAccess />)} />
        <Route path="configuration/identity" element={guard(<IdentitySettings />)} />
        <Route
          path="configuration/registry"
          element={guard(<Registry roles={user.roles} />)}
        />
        <Route
          path="configuration/message-templates"
          element={guard(<MessageTemplates roles={user.roles} />)}
        />
        <Route
          path="configuration/exemptions"
          element={guard(<Exemptions roles={user.roles} />)}
        />
        <Route path="configuration/knowledge" element={guard(<Knowledge />)} />
        <Route path="configuration/counsel-domains" element={guard(<CounselDomains />)} />
        <Route path="configuration/settings" element={guard(<DistrictSettings />)} />
        <Route
          path="configuration/correspondence"
          element={guard(<Correspondence roles={user.roles} />)}
        />

        {/* My Work merged into the Inbox; its personal queue is a chip there. */}
        <Route path="my-work" element={<Navigate to="/inbox?view=personal" replace />} />
        <Route path="agent-access" element={<Navigate to="/configuration/agent-access" replace />} />
        <Route path="requests" element={<Navigate to="/inbox?view=cases" replace />} />
        <Route path="intake" element={<Navigate to="/staff-intake" replace />} />
        <Route
          path="correspondence"
          element={<LegacyCorrespondenceRedirect roles={user.roles} />}
        />
        <Route path="registry" element={<Navigate to="/configuration/registry" replace />} />
        <Route path="message-templates" element={<Navigate to="/configuration/message-templates" replace />} />
        <Route path="exemptions" element={<Navigate to="/configuration/exemptions" replace />} />
        <Route path="identity" element={<Navigate to="/configuration/identity" replace />} />
        <Route path="reporting" element={<Navigate to="/oversight/reporting" replace />} />
        <Route path="audit" element={<Navigate to="/oversight/audit" replace />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/request" element={<PublicRequestForm />} />
        <Route path="/request/submitted" element={<PublicRequestReceipt />} />
        <Route path="/*" element={<StaffApp />} />
      </Routes>
    </BrowserRouter>
  )
}
