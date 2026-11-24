// ABOUTME: Email: incoming mail that is not on a request yet, in one place.
// ABOUTME: No matching request, couldn't be read, and bounced letters, each a tab.
import { useQuery } from '@tanstack/react-query'
import { Link, useOutletContext, useSearchParams } from 'react-router'
import type { StaffRole } from '../api/auth'
import { api, unwrap, type Page } from '../api/client'
import { InboxRow, InboxRowList } from '../inbox/InboxRow'
import { PageHeader } from '../layout/AppLayout'
import { type WorkItemCounts, type WorkItemSummary } from '../work'
import { EmptyState, ErrorNote, FilterChip, Spinner } from '../ui'
import { InboundCorrespondenceReview } from './InboundCorrespondenceReview'

const LINK_CLASS =
  'inline-flex font-heading text-sm font-semibold text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus'


/**
 * Format a date-only API value (YYYY-MM-DD). These are already Pacific
 * calendar dates, so they must NOT be run through a timezone conversion:
 * `new Date('2026-08-31')` is UTC midnight and converting that to Pacific
 * would report August 30 and understate a statutory deadline by a day.
 */
function dayLabel(value: string | null) {
  if (!value) return null
  const [year, month, day] = value.slice(0, 10).split('-').map(Number)
  if (!year || !month || !day) return null
  return new Date(year, month - 1, day).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
}



function WorkRows({
  rows,
  empty,
  showSourceContext,
  urgencyLabel,
}: {
  rows: WorkItemSummary[]
  empty: string
  showSourceContext: boolean
  urgencyLabel?: string
}) {
  if (rows.length === 0) return <EmptyState>{empty}</EmptyState>
  return (
    <InboxRowList>
      {rows.map((row) => {
        const due = urgencyLabel ? dayLabel(row.urgency_date) : null
        return (
          <InboxRow
            key={row.id}
            title={row.next_action}
            caseNumber={row.request_number ?? 'No matched case'}
            tags={[
              row.assignee_username ?? 'Unassigned',
              urgencyLabel && due ? `${urgencyLabel}: ${due}` : null,
            ]}
            primaryAction={
              showSourceContext && row.source_url
                ? {
                    label: 'Open case',
                    to: row.source_url,
                    ariaLabel: `Open case ${row.request_number ?? 'context'}`,
                  }
                : null
            }
            secondaryActions={(
              <>
                {showSourceContext && row.request_number
                  && row.work_kind === 'request_action' && (
                  <Link
                    to={`/requests/${row.request_number}/messages?compose=acknowledgment`}
                    className={LINK_CLASS}
                    aria-label={`Prepare acknowledgment for ${row.request_number}`}
                  >
                    Prepare acknowledgment
                  </Link>
                )}
                {/* A per-row accessible name: a list of identical "Work details"
                    links is unusable with a screen reader's link list. */}
                <Link
                  to={row.detail_url}
                  className={LINK_CLASS}
                  aria-label={
                    `Open ${row.next_action} for `
                    + `${row.request_number ?? 'unmatched work'}`
                  }
                >
                  Work details
                </Link>
              </>
            )}
          />
        )
      })}
    </InboxRowList>
  )
}


type EmailTab = 'no_match' | 'unreadable' | 'bounced'

interface TabOption {
  value: EmailTab
  label: string
  countKey: keyof WorkItemCounts
  officerOnly: boolean
}

const BOUNCED: TabOption = {
  value: 'bounced', label: 'Bounced', countKey: 'inbox_delivery_exceptions', officerOnly: false,
}

const TABS: TabOption[] = [
  { value: 'no_match', label: 'No matching request', countKey: 'inbox_unmatched', officerOnly: true },
  { value: 'unreadable', label: "Couldn't be read", countKey: 'inbox_quarantined', officerOnly: true },
  BOUNCED,
]

export function Email({ roles }: { roles: StaffRole[] }) {
  const counts = useOutletContext<WorkItemCounts | undefined>()
  const [searchParams, setSearchParams] = useSearchParams()
  const isOfficer = roles.includes('records_officer')
  const tabs = TABS.filter((tab) => isOfficer || !tab.officerOnly)
  const requested = searchParams.get('tab')
  const tab = tabs.find((candidate) => candidate.value === requested) ?? tabs[0] ?? BOUNCED

  function selectTab(value: EmailTab) {
    const next = new URLSearchParams()
    next.set('tab', value)
    setSearchParams(next)
  }

  const bounced = useQuery({
    queryKey: ['email-bounced'],
    queryFn: () => api<Page<WorkItemSummary> | WorkItemSummary[]>(
      '/api/staff/work-items/?queue=inbox_delivery_exceptions',
      { fetchAllPages: false },
    ),
    enabled: tab.value === 'bounced',
  })

  return (
    <>
      <PageHeader
        title="Email"
        subtitle="Mail that came in but is not on a request yet. Requester replies on a request are on that request's Messages tab."
      />
      <div className="mb-3 flex flex-wrap items-center gap-2" role="group" aria-label="Email">
        {tabs.map((candidate) => {
          const count = counts?.[candidate.countKey]
          return (
            <FilterChip
              key={candidate.value}
              selected={candidate.value === tab.value}
              onClick={() => selectTab(candidate.value)}
              count={typeof count === 'number' ? count : undefined}
              countLabel={typeof count === 'number' ? `${count} waiting` : undefined}
            >
              {candidate.label}
            </FilterChip>
          )
        })}
      </div>
      {tab.value === 'bounced' ? (
        <>
          {bounced.isPending && <Spinner label="Loading bounced letters…" />}
          {bounced.isError && <ErrorNote error={bounced.error} />}
          {bounced.data && (
            <WorkRows
              rows={unwrap(bounced.data)}
              empty="No letters have bounced."
              showSourceContext={isOfficer}
            />
          )}
        </>
      ) : (
        <InboundCorrespondenceReview
          enabled={isOfficer}
          queue={tab.value === 'unreadable' ? 'quarantined' : 'unmatched'}
          onQueueChange={(queue) =>
            selectTab(queue === 'quarantined' ? 'unreadable' : 'no_match')}
          showQueueSelector={false}
          showReceiptLookup={false}
        />
      )}
    </>
  )
}
