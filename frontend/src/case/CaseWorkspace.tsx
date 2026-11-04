// ABOUTME: Persistent, role-aware case header and reloadable nested case tabs.
// ABOUTME: Only the active tab mounts its request-scoped heavy data surface.
import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'
import { Link, useParams } from 'react-router'
import type { StaffRole } from '../api/auth'
import { api } from '../api/client'
import { PageHeader } from '../layout/AppLayout'
import { CASE_TAB_ROLES } from '../nav'
import { CorrespondencePanel } from '../pages/CorrespondencePanel'
import { Installments } from '../pages/Installments'
import { Preservations } from '../pages/Preservations'
import { RecordItems } from '../pages/RecordItems'
import { RequestDetail } from '../pages/RequestDetail'
import {
  STATUTORY_RESPONSE_LABELS,
  type StatutoryResponse,
} from '../statutory'
import { Tasks } from '../pages/Tasks'
import { TimeEntries } from '../pages/TimeEntries'
import { Badge, Card, EmptyState, ErrorNote, Spinner } from '../ui'
import { statusLabel, statusTone, TERMINAL_STATUSES } from '../caseStatus'
import { CaseActivityFeed } from './CaseActivityFeed'
import { EstimateLetterCard } from './EstimateLetterCard'
import { RecordUploadPanel } from './RecordUploadPanel'
import { ReviewFlight } from './ReviewFlight'


export type CaseTab =
  | 'overview'
  | 'messages'
  | 'collection'
  | 'records'
  | 'review'
  | 'releases'

interface CaseTabDefinition {
  id: CaseTab
  label: string
  roles: StaffRole[]
}

const CASE_TABS: CaseTabDefinition[] = [
  {
    id: 'overview',
    label: 'Overview',
    roles: CASE_TAB_ROLES.overview,
  },
  {
    id: 'messages',
    label: 'Messages',
    roles: CASE_TAB_ROLES.messages,
  },
  {
    id: 'collection',
    label: 'Collection',
    roles: CASE_TAB_ROLES.collection,
  },
  {
    id: 'records',
    label: 'Records',
    roles: CASE_TAB_ROLES.records,
  },
  {
    id: 'review',
    label: 'Review',
    roles: CASE_TAB_ROLES.review,
  },
  {
    id: 'releases',
    label: 'Releases',
    roles: CASE_TAB_ROLES.releases,
  },
]

interface CaseSummary {
  number: string
  legal_status: string
  requester: { name: string; email: string } | null
  owner: { id: number; username: string; display_name: string } | null
  statutory_response: StatutoryResponse
  estimated_completion: string | null
  counts: { new: number; needs_attention: number }
  next_action: {
    label: string
    detail: string
    tab: CaseTab
    work_item_id: string
  } | null
}

interface ClosureReadiness {
  ready: boolean
  counts: Record<string, number>
  blockers: Array<{
    code: string
    label: string
    count: number
    tab: CaseTab
  }>
}

function roleCanUseTab(tab: CaseTabDefinition, roles: StaffRole[]) {
  return tab.roles.some((role) => roles.includes(role))
}

function CaseHeader({
  summary,
  roles,
}: {
  summary: CaseSummary
  roles: StaffRole[]
}) {
  const requester = roles.includes('records_officer') ? summary.requester : null
  return (
    <>
      <PageHeader
        title={summary.number}
        subtitle="Public records case workspace"
        actions={(
          <Badge tone={statusTone(summary.legal_status)}>
            {statusLabel(summary.legal_status)}
          </Badge>
        )}
      />
      <Card className="mb-4" aria-label="Case summary">
        <dl className="grid gap-x-4 gap-y-2 text-sm sm:grid-cols-3 xl:grid-cols-6">
          <div>
            <dt className="text-ink-muted">Requester</dt>
            <dd className="font-heading font-bold text-ink">
              {requester?.name ?? 'Restricted'}
            </dd>
            {requester?.email && (
              <dd className="break-all text-xs text-ink-muted">{requester.email}</dd>
            )}
          </div>
          <div>
            <dt className="text-ink-muted">Case owner</dt>
            <dd className="font-heading font-bold text-ink">
              {summary.owner?.display_name ?? 'Unassigned'}
            </dd>
          </div>
          <div>
            <dt className="text-ink-muted">Statutory response</dt>
            <dd className="font-heading font-bold text-ink">
              {STATUTORY_RESPONSE_LABELS[summary.statutory_response.state]
                ?? summary.statutory_response.state}
            </dd>
            <dd className="text-xs text-ink-muted">
              Due {summary.statutory_response.due_date ?? '—'}
            </dd>
          </div>
          <div>
            <dt className="text-ink-muted">Estimate</dt>
            <dd className="font-heading font-bold text-ink">
              {summary.estimated_completion ?? '—'}
            </dd>
          </div>
          <div>
            <dt className="text-ink-muted">Work counts</dt>
            <dd className="flex flex-wrap gap-2 pt-1">
              <Badge tone={summary.counts.new ? 'blue' : 'neutral'}>
                {summary.counts.new} new
              </Badge>
              <Badge tone={summary.counts.needs_attention ? 'amber' : 'neutral'}>
                {summary.counts.needs_attention} need attention
              </Badge>
            </dd>
          </div>
          <div>
            <dt className="text-ink-muted">Next action</dt>
            {summary.next_action ? (
              <dd>
                <Link
                  className="font-heading font-bold text-accent underline-offset-2 hover:underline"
                  to={`/requests/${summary.number}/${summary.next_action.tab}`}
                >
                  {summary.next_action.label}
                </Link>
                <p className="mt-1 text-xs text-ink-muted">
                  {summary.next_action.detail}
                </p>
              </dd>
            ) : (
              <dd className="font-heading font-bold text-ink">No role-scoped action</dd>
            )}
          </div>
        </dl>
      </Card>
    </>
  )
}

function ClosureReadinessCard({ number }: { number: string }) {
  const readiness = useQuery({
    queryKey: ['case-closure-readiness', number],
    queryFn: () => api<ClosureReadiness>(
      `/api/staff/requests/${number}/closure-readiness/`,
      { fetchAllPages: false },
    ),
  })
  if (readiness.isPending) return <Spinner label="Checking closure readiness…" />
  if (readiness.isError) return <ErrorNote error={readiness.error} />
  return (
    <Card title="Closure readiness" className="mb-5">
      <p className="text-sm text-ink-muted">
        Read-only blocker report. Closing a case still requires the existing
        correspondence, delivery, and lifecycle services and accountable human action.
      </p>
      {readiness.data.ready ? (
        <div className="mt-3"><Badge tone="green">No listed blockers</Badge></div>
      ) : (
        <ul className="mt-4 grid gap-3 sm:grid-cols-2">
          {readiness.data.blockers.map((blocker) => (
            <li key={blocker.code} className="rounded-md border border-warning bg-warning-soft p-3">
              <Link
                to={`/requests/${number}/${blocker.tab}`}
                className="font-heading text-sm font-bold text-warning-ink underline-offset-2 hover:underline"
              >
                {blocker.label}
              </Link>
              <p className="mt-1 text-xs text-warning-ink">{blocker.count} outstanding</p>
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}

function TabContent({
  tab,
  number,
  roles,
  summary,
}: {
  tab: CaseTab
  number: string
  roles: StaffRole[]
  summary: CaseSummary
}) {
  if (tab === 'overview') {
    return (
      <div className="space-y-6">
        <RequestDetail key={number} roles={roles} mode="overview" />
        <TimeEntries roles={roles} requestNumber={number} />
      </div>
    )
  }
  if (tab === 'messages') {
    return (
      <CorrespondencePanel
        requestNumber={number}
        roles={roles}
        requesterEmail={summary.requester?.email ?? ''}
        requesterName={summary.requester?.name ?? ''}
        statutoryResponse={summary.statutory_response}
      />
    )
  }
  if (tab === 'collection') {
    return (
      <div className="space-y-6">
        {/* Keyed on the case so per-case form state (Vault scope, attestations)
            starts fresh when the officer hops to another case without a page
            load; the route element itself is reused across :number changes. */}
        <RequestDetail key={number} roles={roles} mode="collection" />
        <Tasks roles={roles} requestNumber={number} />
        {(roles.includes('records_officer') || roles.includes('admin')) && (
          <Preservations roles={roles} requestNumber={number} />
        )}
      </div>
    )
  }
  if (tab === 'records') {
    return (
      <div className="space-y-4">
        {/* Adding records is collection work, so it is the officer's alone;
            reviewers and admins see this tab without the button. */}
        {roles.includes('records_officer') && (
          <RecordUploadPanel
            requestNumber={number}
            closed={TERMINAL_STATUSES.has(summary.legal_status)}
          />
        )}
        <RecordItems roles={roles} requestNumber={number} />
      </div>
    )
  }
  if (tab === 'review') {
    return <ReviewFlight number={number} />
  }
  if (tab === 'releases') {
    return (
      <>
        <ClosureReadinessCard number={number} />
        <Installments key={number} roles={roles} requestNumber={number} />
      </>
    )
  }
  return <CaseActivityFeed number={number} />
}

export function CaseWorkspace({ roles }: { roles: StaffRole[] }) {
  const { number = '', tab: rawTab = '' } = useParams<{
    number: string
    tab: string
  }>()
  const tabDefinition = CASE_TABS.find((candidate) => candidate.id === rawTab)
  const tab = tabDefinition?.id
  const headingRef = useRef<HTMLHeadingElement>(null)
  const summary = useQuery({
    queryKey: ['case-workspace-summary', number],
    queryFn: () => api<CaseSummary>(
      `/api/staff/requests/${number}/workspace-summary/`,
      { fetchAllPages: false },
    ),
    enabled: number !== '',
    staleTime: 5 * 60 * 1000,
  })

  useEffect(() => {
    if (tab && summary.isSuccess) headingRef.current?.focus()
  }, [tab, summary.isSuccess])

  if (!tabDefinition) {
    return (
      <>
        <PageHeader title="Not found" />
        <EmptyState>This case tab does not exist.</EmptyState>
      </>
    )
  }
  if (!roleCanUseTab(tabDefinition, roles)) {
    return (
      <>
        <PageHeader title="Not authorized" />
        <EmptyState>Your current staff roles do not permit this case tab.</EmptyState>
      </>
    )
  }
  if (summary.isPending) return <Spinner label="Loading case…" />
  if (summary.isError) return <ErrorNote error={summary.error} />
  if (!tab) return null
  const visibleTabs = CASE_TABS.filter((candidate) => roleCanUseTab(candidate, roles))

  return (
    <>
      <CaseHeader summary={summary.data} roles={roles} />
      {roles.includes('records_officer') && <EstimateLetterCard number={number} />}
      <nav aria-label="Case tabs" className="mb-5 overflow-x-auto border-b border-line">
        <div className="flex min-w-max gap-1">
          {visibleTabs.map((candidate) => (
            <Link
              key={candidate.id}
              to={`/requests/${number}/${candidate.id}`}
              aria-current={candidate.id === tab ? 'page' : undefined}
              className={`border-b-2 px-4 py-3 font-heading text-sm font-bold ${
                candidate.id === tab
                  ? 'border-flow text-ink'
                  : 'border-transparent text-ink-muted hover:text-ink'
              }`}
            >
              {candidate.label}
            </Link>
          ))}
        </div>
      </nav>
      <section aria-labelledby={`case-tab-${tab}`}>
        {/* Screen-reader only, and never revealed. This is the section's
            accessible name and the element that takes focus on tab navigation
            (keyboard-case-inbox.spec.ts), but the effect above focuses it on
            EVERY tab load -- so any `focus:` reveal renders it permanently,
            floating over the content beneath. The tab underline and
            aria-current already carry the visual signal. */}
        <h2
          ref={headingRef}
          id={`case-tab-${tab}`}
          tabIndex={-1}
          className="sr-only"
        >
          {tabDefinition.label}
        </h2>
        <TabContent
          tab={tab}
          number={number}
          roles={roles}
          summary={summary.data}
        />
      </section>
    </>
  )
}
