// ABOUTME: Today: every open case, most urgent first, each with its one next
// ABOUTME: step. The home screen; one search box reaches every case.
import { useQuery } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { Link } from 'react-router'
import { api, unwrap, type Page } from '../api/client'
import { pacificDateString } from '../date'
import { dueLabel, nextStep, plural, type TodayCase } from '../today'
import { PageHeader } from '../layout/AppLayout'
import { ErrorNote, Spinner } from '../ui'
import type { WorkItemCounts } from '../work'

const TONE = {
  late: 'bg-danger-soft text-danger-ink',
  soon: 'bg-warning-soft text-warning-ink',
  calm: 'bg-surface-sunken text-ink',
  none: 'bg-surface-sunken text-ink-muted',
} as const

function CaseRow({ row }: { row: TodayCase }) {
  const due = dueLabel(row.next_due)
  const step = nextStep(row)
  const detail = row.stage?.state === 'blocked' ? row.stage.reason : row.stage?.summary
  return (
    <li className="flex flex-col gap-3 rounded-xl border border-line bg-surface-raised p-4 shadow-sm sm:flex-row sm:items-center sm:gap-5">
      <span
        className={`w-fit shrink-0 rounded-full px-3 py-1 text-sm font-semibold sm:w-36 sm:text-center ${TONE[due.tone]}`}
      >
        {due.text}
      </span>
      <div className="min-w-0 flex-1">
        <Link
          to={`/requests/${row.number}/${row.stage?.id ?? 'intake'}`}
          className="block truncate font-heading text-lg font-bold text-ink hover:text-accent"
        >
          {row.subject}
        </Link>
        <p className="mt-0.5 truncate text-sm text-ink-muted">
          {row.number}
          {row.requester_name || row.requester_email
            ? ` · ${row.requester_name || row.requester_email}`
            : ''}
          {detail ? ` · ${detail}` : ''}
        </p>
      </div>
      <Link
        to={step.path}
        className={`nx-btn ${step.waiting ? 'nx-btn--secondary' : 'nx-btn--primary'} h-auto min-h-10 shrink-0 justify-center whitespace-normal py-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus`}
      >
        {step.label}
      </Link>
    </li>
  )
}

export function Today() {
  const [draft, setDraft] = useState('')
  const [search, setSearch] = useState('')
  const query = search
    ? `search=${encodeURIComponent(search)}&ordering=due`
    : 'status=open&ordering=due'
  const cases = useQuery({
    queryKey: ['today', search],
    queryFn: () => api<Page<TodayCase> | TodayCase[]>(`/api/staff/requests/?${query}`),
  })
  const decisions = useQuery({
    queryKey: ['decision-batches', 'proposed'],
    queryFn: () => api<Page<unknown> | unknown[]>('/api/staff/decision-batches/?state=proposed'),
  })
  const counts = useQuery({
    queryKey: ['work-item-counts'],
    queryFn: () => api<WorkItemCounts>('/api/staff/work-items/counts/'),
  })
  const rows = cases.data ? unwrap(cases.data) : []
  const waitingDecisions = decisions.data ? unwrap(decisions.data).length : 0
  // Work that belongs to no case yet, beside the cases themselves.
  const needsYou = [
    { count: counts.data?.inbox_unmatched ?? 0, one: 'email needs a case', many: 'emails need a case', to: '/inbox?view=unmatched' },
    { count: counts.data?.inbox_quarantined ?? 0, one: 'email held for review', many: 'emails held for review', to: '/inbox?view=quarantined' },
    { count: counts.data?.inbox_delivery_exceptions ?? 0, one: 'message did not deliver', many: 'messages did not deliver', to: '/inbox?view=delivery_exceptions' },
    { count: waitingDecisions, one: 'decision waiting', many: 'decisions waiting', to: '/decisions' },
  ].filter((item) => item.count > 0)
  const lateCount = rows.filter((row) => dueLabel(row.next_due).tone === 'late').length

  function submit(event: FormEvent) {
    event.preventDefault()
    setSearch(draft.trim())
  }

  const today = new Date(`${pacificDateString()}T12:00:00`).toLocaleDateString('en-US', {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
  })

  return (
    <>
      <PageHeader
        title="Today"
        subtitle={search
          ? `Results for “${search}”`
          : cases.data
            ? `${plural(rows.length, 'open case', 'open cases')}${lateCount ? ` · ${lateCount} late` : ''}`
            : today}
      />
      {needsYou.length > 0 && (
        <nav aria-label="Needs you" className="mb-4 flex flex-wrap gap-2">
          {needsYou.map((item) => (
            <Link
              key={item.to}
              to={item.to}
              className="rounded-full bg-warning-soft px-3 py-1 text-sm font-semibold text-warning-ink hover:underline"
            >
              {plural(item.count, item.one, item.many)}
            </Link>
          ))}
        </nav>
      )}
      <form role="search" onSubmit={submit} className="mb-6 flex gap-2">
        <input
          type="search"
          aria-label="Search cases"
          placeholder="Find any case by number, requester, or words in the request"
          value={draft}
          onChange={(event) => {
            setDraft(event.target.value)
            if (!event.target.value) setSearch('')
          }}
          className="nx-input min-h-11 flex-1 rounded-lg border border-line bg-surface-raised px-4 text-base text-ink"
        />
      </form>
      {cases.isPending && <Spinner label="Loading cases…" />}
      {cases.isError && <ErrorNote error={cases.error} />}
      {cases.data && rows.length === 0 && (
        <p className="rounded-xl border border-line bg-surface-raised p-8 text-center font-editorial text-lg text-ink-muted">
          {search ? 'No case matches that search.' : 'Nothing open. Enjoy the quiet.'}
        </p>
      )}
      {rows.length > 0 && (
        <ul className="flex flex-col gap-3">
          {rows.map((row) => <CaseRow key={row.number} row={row} />)}
        </ul>
      )}
    </>
  )
}
