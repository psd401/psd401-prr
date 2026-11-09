// ABOUTME: What Today says about a case: its one next step and how soon it
// ABOUTME: is due, in the words a person would use.
import { pacificDateString } from './date'
import type { CaseListItem } from './pages/Inbox'

export interface CaseStage {
  id: 'intake' | 'scope' | 'collect' | 'review' | 'redact' | 'release' | 'close'
  label: string
  state: 'done' | 'current' | 'blocked' | 'upcoming'
  open_count: number
  summary: string
  reason: string
}

export type TodayCase = CaseListItem & { stage: CaseStage | null }

export interface NextStep {
  label: string
  path: string
  /** True when the case waits on the requester, not on staff. */
  waiting: boolean
}

export function plural(count: number, one: string, many: string) {
  return `${count.toLocaleString('en-US')} ${count === 1 ? one : many}`
}

/** The one thing to do on a case, and where it is done. */
export function nextStep(row: Pick<TodayCase, 'number' | 'matter_kind' | 'stage'>): NextStep {
  const stage = row.stage
  const at = (tab: string, label: string, waiting = false) => ({
    label,
    path: `/requests/${row.number}/${tab}`,
    waiting,
  })
  if (!stage || stage.state === 'done') return at('intake', 'View case')
  const blocked = stage.state === 'blocked'
  switch (stage.id) {
    case 'intake':
      // Only a public records case owes an acknowledgment (rule 13); the
      // approve-and-send step lives in Messages.
      return row.matter_kind === 'public_records'
        ? at('messages', 'Send acknowledgment')
        : at('intake', 'Triage')
    case 'scope':
      return blocked ? at('scope', 'Waiting on requester', true) : at('scope', 'Set the scope')
    case 'collect':
      return at('collect', 'Finish collection')
    case 'review':
      return at('review', `Review ${plural(stage.open_count, 'record', 'records')}`)
    case 'redact':
      return at('redact', `Decide ${plural(stage.open_count, 'redaction', 'redactions')}`)
    case 'release':
      return blocked ? at('release', 'Waiting on deposit', true) : at('release', 'Release records')
    case 'close':
      return at('close', 'Close the case')
  }
}

export interface DueLabel {
  text: string
  tone: 'late' | 'soon' | 'calm' | 'none'
}

function daysBetween(from: string, to: string) {
  const [fy = 0, fm = 1, fd = 1] = from.split('-').map(Number)
  const [ty = 0, tm = 1, td = 1] = to.split('-').map(Number)
  return Math.round((Date.UTC(ty, tm - 1, td) - Date.UTC(fy, fm - 1, fd)) / 86_400_000)
}

/** A due date as a person says it: "3 days late", "Due tomorrow". */
export function dueLabel(due: string | null): DueLabel {
  if (!due) return { text: 'No due date', tone: 'none' }
  const today = pacificDateString()
  const days = daysBetween(today, due.slice(0, 10))
  if (days < 0) return { text: `${plural(-days, 'day', 'days')} late`, tone: 'late' }
  if (days === 0) return { text: 'Due today', tone: 'soon' }
  if (days === 1) return { text: 'Due tomorrow', tone: 'soon' }
  if (days <= 7) return { text: `Due in ${days} days`, tone: 'calm' }
  const [year = 0, month = 1, day = 1] = due.slice(0, 10).split('-').map(Number)
  const sameYear = year === Number(today.slice(0, 4))
  const date = new Date(year, month - 1, day).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    ...(sameYear ? {} : { year: 'numeric' }),
  })
  return { text: `Due ${date}`, tone: 'calm' }
}

/** Pill colors for each due tone. */
export const DUE_TONE = {
  late: 'bg-danger-soft text-danger-ink',
  soon: 'bg-warning-soft text-warning-ink',
  calm: 'bg-surface-sunken text-ink',
  none: 'bg-surface-sunken text-ink-muted',
} as const
