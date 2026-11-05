// ABOUTME: Persistent, role-aware case header and the stage rail (#45): Intake
// ABOUTME: through Close, each stage led by its decision card. One stage mounts.
import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'
import { Link, Navigate, useNavigate, useParams, useSearchParams } from 'react-router'
import type { StaffRole } from '../api/auth'
import { api } from '../api/client'
import { PageHeader } from '../layout/AppLayout'
import { CASE_TAB_ROLES, caseTabPath, LEGACY_CASE_TABS } from '../nav'
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
import { CollectionCards } from './CollectionCards'
import { useCaseCollections } from './caseCollections'
import { EstimateLetterCard } from './EstimateLetterCard'
import { RecordUploadPanel } from './RecordUploadPanel'
import { RedactionProposals } from './RedactionProposals'
import { ReviewFlight } from './ReviewFlight'
import { ScopingTrail } from './ScopingTrail'

export type CaseStage =
  | 'intake'
  | 'scope'
  | 'collect'
  | 'review'
  | 'redact'
  | 'release'
  | 'close'

/** A case URL segment: a stage, or one of the two views beside the rail. */
export type CaseTab = CaseStage | 'messages' | 'activity'

type StageState = 'done' | 'current' | 'blocked' | 'upcoming'

interface StageSummary {
  id: CaseStage
  label: string
  state: StageState
  open_count: number
  summary: string
  reason: string
}

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
    tab: string
    work_item_id: string
  } | null
  stages: StageSummary[]
}

interface ClosureReadiness {
  ready: boolean
  counts: Record<string, number>
  blockers: Array<{
    code: string
    label: string
    count: number
    tab: string
  }>
}

const SIDE_VIEWS: Array<{ id: 'messages' | 'activity'; label: string }> = [
  { id: 'messages', label: 'Messages' },
  { id: 'activity', label: 'Activity' },
]

/**
 * What each stage asks of the person, and the way back where one exists.
 * The undo lines state only what the services guarantee.
 */
const STAGE_DECISIONS: Record<CaseStage, { title: string; undo?: string }> = {
  intake: {
    title: 'Acknowledge, clarify, or close this request',
    undo: 'A closed request can be reopened; the closure stays on the timeline.',
  },
  scope: {
    title: 'Set the scope and start collection',
    undo: 'A count creates no export, hold, collection task, status change, or requester message.',
  },
  collect: {
    title: 'Finish the open collections',
    undo: 'Supersede takes unreviewed records out of review; nothing is deleted.',
  },
  review: { title: 'Decide which records are responsive' },
  redact: { title: 'Decide the redaction proposals' },
  release: { title: 'Release the responsive records' },
  close: {
    title: 'Close the case',
    undo: 'A closed request can be reopened; the closure stays on the timeline.',
  },
}

function roleCanOpen(tab: string, roles: StaffRole[]) {
  if (!Object.hasOwn(CASE_TAB_ROLES, tab)) return false
  return CASE_TAB_ROLES[tab as keyof typeof CASE_TAB_ROLES].some((role) => roles.includes(role))
}

function isCaseTab(tab: string): tab is CaseTab {
  return [
    'intake', 'scope', 'collect', 'review', 'redact', 'release', 'close', 'messages', 'activity',
  ].includes(tab)
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
                  to={caseTabPath(summary.number, summary.next_action.tab)}
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

const STATE_TEXT: Record<StageState, string> = {
  done: 'Done',
  current: 'Current',
  blocked: 'Blocked',
  upcoming: 'Upcoming',
}

function StageMark({ state }: { state: StageState }) {
  const base = 'grid h-6 w-6 flex-none place-items-center rounded-full border-2 text-xs font-bold'
  if (state === 'done') {
    return <span aria-hidden className={`${base} border-success bg-success text-surface-raised`}>✓</span>
  }
  if (state === 'blocked') {
    return <span aria-hidden className={`${base} border-warning bg-warning-soft text-warning-ink`}>!</span>
  }
  if (state === 'current') {
    return <span aria-hidden className={`${base} border-flow bg-flow-soft`} />
  }
  return <span aria-hidden className={`${base} border-line-strong`} />
}

/**
 * The rail: every stage in order with its state, and the reason when a
 * stage is blocked. A stage the viewer's roles cannot open is shown, not
 * linked, so the case's progress reads the same for everyone.
 */
function StageRail({
  number,
  stages,
  active,
  roles,
}: {
  number: string
  stages: StageSummary[]
  active: CaseTab
  roles: StaffRole[]
}) {
  const sideViews = SIDE_VIEWS.filter((view) => roleCanOpen(view.id, roles))
  return (
    <nav aria-label="Case stages" className="mb-5 border-b border-line pb-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <ol className="flex min-w-0 flex-1 flex-wrap gap-1">
          {stages.map((stage) => {
            const selected = stage.id === active
            const detail = stage.state === 'blocked' ? stage.reason : stage.summary
            const label = `${stage.label}: ${STATE_TEXT[stage.state]}${detail ? `, ${detail}` : ''}`
            const body = (
              <>
                <StageMark state={stage.state} />
                <span className="min-w-0">
                  <span className="block font-heading text-sm font-bold">{stage.label}</span>
                  <span className="block text-xs text-ink-muted">
                    {STATE_TEXT[stage.state]}
                    {detail ? ` · ${detail}` : ''}
                  </span>
                </span>
              </>
            )
            const className = `flex max-w-56 items-start gap-2 rounded-md border-b-2 px-3 py-2 ${
              selected ? 'border-flow bg-surface-selected text-ink' : 'border-transparent text-ink-muted'
            }`
            return (
              <li key={stage.id} data-stage-state={stage.state}>
                {roleCanOpen(stage.id, roles) ? (
                  <Link
                    to={`/requests/${number}/${stage.id}`}
                    aria-label={label}
                    aria-current={selected ? 'page' : undefined}
                    className={`${className} hover:text-ink`}
                  >
                    {body}
                  </Link>
                ) : (
                  <span className={className}>{body}</span>
                )}
              </li>
            )
          })}
        </ol>
        {sideViews.length > 0 && (
          <ul className="flex gap-1" aria-label="Case views">
            {sideViews.map((view) => (
              <li key={view.id}>
                <Link
                  to={`/requests/${number}/${view.id}`}
                  aria-current={view.id === active ? 'page' : undefined}
                  className={`block border-b-2 px-3 py-2 font-heading text-sm font-bold ${
                    view.id === active
                      ? 'border-flow text-ink'
                      : 'border-transparent text-ink-muted hover:text-ink'
                  }`}
                >
                  {view.label}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
    </nav>
  )
}

/**
 * The stage's lead (Nexus DecisionCard): the decision it needs, why, what is
 * at stake, the evidence one click away, and the way back where one exists.
 * No model recommends these calls, so the card is the person's (`--needs`).
 */
function StageDecision({ stage }: { stage: StageSummary }) {
  const decision = STAGE_DECISIONS[stage.id]
  const open = stage.state === 'current' || stage.state === 'blocked'
  const title = stage.state === 'done' ? `${stage.label} is done` : decision.title
  let why: string
  if (stage.state === 'blocked') why = stage.reason
  else if (stage.state === 'done') why = 'Nothing is open at this stage.'
  else if (stage.summary) why = stage.summary
  else if (stage.state === 'upcoming') why = 'Nothing has reached this stage yet.'
  else why = 'Every earlier stage is done; this is the next step.'
  return (
    <section
      className={`nx-decision mb-5 ${open ? 'nx-decision--needs' : ''}`}
      aria-label={`${stage.label} decision`}
    >
      <span className="nx-eyebrow">{stage.label} · {STATE_TEXT[stage.state]}</span>
      <h3 className="nx-decision__title">{title}</h3>
      <p className="nx-decision__why">{why}</p>
      {/* Only the stage the case is in asks for a call; the others stay
          compact so their own work leads the page. */}
      {open && (
        <div className="nx-decision__rec">
          <span className="nx-decision__rec-label">Your call</span>
          <span className="nx-decision__conf">
            <b>{stage.open_count}</b>
            open
          </span>
        </div>
      )}
      <div className="nx-decision__actions">
        <a
          href="#stage-evidence"
          className="rounded-md border border-line px-3 py-1.5 text-sm font-semibold text-accent hover:bg-surface-hover"
        >
          See the evidence
        </a>
      </div>
      {decision.undo && <p className="nx-decision__undo">{decision.undo}</p>}
    </section>
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
                to={caseTabPath(number, blocker.tab)}
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

/**
 * The scoping inputs. Once a collection exists they fold away under one
 * "Scope" step, because the Collect stage's cards are what matter then;
 * the scoping trail, which holds Re-scope, stays open.
 */
function ScopeStage({ number, roles }: { number: string; roles: StaffRole[] }) {
  const collections = useCaseCollections(number)
  if (collections.isPending) return <Spinner label="Loading scope…" />
  const hasCollection = (collections.data?.length ?? 0) > 0
  return (
    <div className="space-y-6">
      {roles.includes('records_officer') && <ScopingTrail number={number} />}
      <details open={!hasCollection} className="rounded-lg border border-line bg-surface-raised p-4">
        <summary className="cursor-pointer font-heading text-sm font-bold text-ink">
          Scope: request intent and Vault count
          {hasCollection && (
            <span className="ml-2 font-normal text-ink-muted">
              (a collection exists; open to scope again)
            </span>
          )}
        </summary>
        <div className="mt-4">
          {/* Keyed on the case so per-case form state (Vault scope) starts
              fresh when the officer hops to another case without a page load. */}
          <RequestDetail key={number} roles={roles} mode="scope" />
        </div>
      </details>
    </div>
  )
}

/** Review: the keyboard flight by default, or every record (`?view=records`). */
function ReviewStage({ number, roles }: { number: string; roles: StaffRole[] }) {
  const [params, setParams] = useSearchParams()
  const canFly = roles.includes('records_officer') || roles.includes('reviewer')
  const view = !canFly || params.get('view') === 'records' ? 'records' : 'flight'
  function choose(next: 'flight' | 'records') {
    const updated = new URLSearchParams(params)
    if (next === 'records') updated.set('view', 'records')
    else updated.delete('view')
    setParams(updated)
  }
  return (
    <div className="space-y-4">
      {canFly && (
        <div role="group" aria-label="Review view" className="flex gap-2">
          <button
            type="button"
            aria-pressed={view === 'flight'}
            onClick={() => choose('flight')}
            className={`rounded-md border px-3 py-1.5 text-sm font-semibold ${
              view === 'flight' ? 'border-flow bg-flow-soft text-ink' : 'border-line text-ink-muted'
            }`}
          >
            Review one by one
          </button>
          <button
            type="button"
            aria-pressed={view === 'records'}
            onClick={() => choose('records')}
            className={`rounded-md border px-3 py-1.5 text-sm font-semibold ${
              view === 'records' ? 'border-flow bg-flow-soft text-ink' : 'border-line text-ink-muted'
            }`}
          >
            Record list
          </button>
        </div>
      )}
      {view === 'flight'
        ? <ReviewFlight number={number} />
        : <RecordItems roles={roles} requestNumber={number} />}
    </div>
  )
}

function RedactStage({ number }: { number: string }) {
  const navigate = useNavigate()
  return (
    <RedactionProposals
      request={number}
      onOpen={(id) => navigate(
        `/requests/${number}/review?view=records&item=${encodeURIComponent(id)}`,
      )}
    />
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
  if (tab === 'intake') {
    return (
      <div className="space-y-6">
        <RequestDetail key={number} roles={roles} mode="overview" />
        <TimeEntries roles={roles} requestNumber={number} />
      </div>
    )
  }
  if (tab === 'scope') return <ScopeStage number={number} roles={roles} />
  if (tab === 'collect') {
    return (
      <div className="space-y-6">
        <CollectionCards number={number} roles={roles} />
        {/* Keyed on the case so per-case form state (attestations) starts
            fresh when the officer hops to another case without a page load;
            the route element itself is reused across :number changes. */}
        <RequestDetail key={number} roles={roles} mode="collection" />
        <Tasks roles={roles} requestNumber={number} />
        {(roles.includes('records_officer') || roles.includes('admin')) && (
          <Preservations roles={roles} requestNumber={number} />
        )}
        {/* Adding records is collection work, so it is the officer's alone. */}
        {roles.includes('records_officer') && (
          <RecordUploadPanel
            requestNumber={number}
            closed={TERMINAL_STATUSES.has(summary.legal_status)}
          />
        )}
      </div>
    )
  }
  if (tab === 'review') return <ReviewStage number={number} roles={roles} />
  if (tab === 'redact') return <RedactStage number={number} />
  if (tab === 'release') return <Installments key={number} roles={roles} requestNumber={number} />
  if (tab === 'close') {
    return (
      <div className="space-y-6">
        {roleCanOpen('releases', roles) && <ClosureReadinessCard number={number} />}
        <RequestDetail key={number} roles={roles} mode="close" />
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
  return <CaseActivityFeed number={number} />
}

export function CaseWorkspace({ roles }: { roles: StaffRole[] }) {
  const { number = '', tab: rawTab = '' } = useParams<{
    number: string
    tab: string
  }>()
  const [search] = useSearchParams()
  const legacyTarget = Object.hasOwn(LEGACY_CASE_TABS, rawTab)
    ? LEGACY_CASE_TABS[rawTab as keyof typeof LEGACY_CASE_TABS]
    : null
  const tab = isCaseTab(rawTab) ? rawTab : undefined
  const headingRef = useRef<HTMLHeadingElement>(null)
  const summary = useQuery({
    queryKey: ['case-workspace-summary', number],
    queryFn: () => api<CaseSummary>(
      `/api/staff/requests/${number}/workspace-summary/`,
      { fetchAllPages: false },
    ),
    enabled: number !== '' && tab !== undefined,
    staleTime: 5 * 60 * 1000,
  })

  useEffect(() => {
    if (tab && summary.isSuccess) headingRef.current?.focus()
  }, [tab, summary.isSuccess])

  // A pre-rail tab URL (bookmarks, old links, server-sent tab names) keeps
  // its query and lands on the stage that now holds that work.
  if (legacyTarget) {
    const params = new URLSearchParams(search)
    if (rawTab === 'records') params.set('view', 'records')
    const query = params.toString()
    return (
      <Navigate
        to={`/requests/${number}/${legacyTarget}${query ? `?${query}` : ''}`}
        replace
      />
    )
  }
  if (!tab) {
    return (
      <>
        <PageHeader title="Not found" />
        <EmptyState>This case tab does not exist.</EmptyState>
      </>
    )
  }
  if (!roleCanOpen(tab, roles)) {
    return (
      <>
        <PageHeader title="Not authorized" />
        <EmptyState>Your current staff roles do not permit this case tab.</EmptyState>
      </>
    )
  }
  if (summary.isPending) return <Spinner label="Loading case…" />
  if (summary.isError) return <ErrorNote error={summary.error} />
  const stages = summary.data.stages
  const stage = stages.find((candidate) => candidate.id === tab)
  const heading = stage?.label ?? SIDE_VIEWS.find((view) => view.id === tab)?.label ?? tab

  return (
    <>
      <CaseHeader summary={summary.data} roles={roles} />
      {roles.includes('records_officer') && <EstimateLetterCard number={number} />}
      <StageRail number={number} stages={stages} active={tab} roles={roles} />
      <section aria-labelledby={`case-tab-${tab}`}>
        {/* Screen-reader only, and never revealed. This is the section's
            accessible name and the element that takes focus on stage
            navigation (keyboard-case-inbox.spec.ts), but the effect above
            focuses it on EVERY load -- so any `focus:` reveal renders it
            permanently, floating over the content beneath. The rail's
            selected stage and aria-current already carry the visual signal. */}
        <h2
          ref={headingRef}
          id={`case-tab-${tab}`}
          tabIndex={-1}
          className="sr-only"
        >
          {heading}
        </h2>
        {stage && <StageDecision stage={stage} />}
        <div id="stage-evidence">
          <TabContent
            tab={tab}
            number={number}
            roles={roles}
            summary={summary.data}
          />
        </div>
      </section>
    </>
  )
}
