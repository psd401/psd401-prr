// ABOUTME: One row shape for every Inbox queue, whatever kind of work it is.
// ABOUTME: The filter decides which rows you see, never what a row looks like.

import type { ReactNode } from 'react'
import { Link } from 'react-router'
import { Badge } from '../ui'

/**
 * What every Inbox row says, regardless of what it is underneath.
 *
 * The Inbox used to render three unrelated designs on one screen: dense
 * message rows, tall "Respond to request" cards, and a table of receipt UUIDs
 * and reason codes. Switching queues changed the entire visual language, so an
 * officer had to relearn the list every time they moved between filters.
 *
 * A row is a piece of work. It answers four questions in the same order every
 * time: what is it, which case, what is owed and when, what can I do about it.
 */
export interface InboxRowProps {
  /** What this is, in the words an officer would use. Never a status code. */
  title: string
  /** The line under the title: a message snippet, or nothing. */
  detail?: string | null
  /** Replaces `detail` when the row is expanded. */
  expandedContent?: ReactNode
  onToggleExpanded?: () => void
  expanded?: boolean
  /** Case number, or null when nothing is matched to a case yet. */
  caseNumber?: string | null
  /**
   * Makes the row selectable for a batch action. Absent on every row that no
   * batch can act on, so a checkbox never appears next to work it cannot do.
   */
  selection?: {
    checked: boolean
    onChange: (checked: boolean) => void
    ariaLabel: string
  } | null
  /** Right-aligned timestamp. */
  timestamp?: string | null
  /**
   * Small plain-text facts at the bottom of the row: kind, legal status,
   * attachment count. Tiny and last, because they help you scan but they are
   * not the thing you are reading.
   */
  tags?: (string | null | undefined)[]
  /** The one fact that decides whether to open the row, when it is unsettled. */
  alert?: { label: string; tone: 'amber' | 'red' | 'green' | 'neutral' } | null
  /** The single obvious action. Everything else is secondary. */
  primaryAction?: { label: string; to: string; ariaLabel: string } | null
  /** Secondary controls, already named for this row. */
  secondaryActions?: ReactNode
  /**
   * Row-scoped dialogs and error notes. They are mounted only where their
   * trigger exists: a closed dialog still holds its text in the DOM, and
   * fifty rows each carrying two of them would put fifty copies of another
   * case's number and subject on the page.
   */
  children?: ReactNode
}

const PRIMARY_LINK_CLASS =
  'inline-flex items-center rounded-lg bg-cedar px-3.5 py-2 font-heading text-sm '
  + 'font-semibold text-white transition-colors hover:bg-pacific '
  + 'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-whulge'

export function InboxRow({
  title,
  detail,
  expandedContent,
  onToggleExpanded,
  expanded = false,
  caseNumber,
  selection,
  timestamp,
  tags = [],
  alert,
  primaryAction,
  secondaryActions,
  children,
}: InboxRowProps) {
  const shownTags = tags.filter(Boolean) as string[]
  const indent = onToggleExpanded ? 'ml-6' : ''

  return (
    <li className="rounded-lg border border-driftwood/60 bg-white px-3 py-2 shadow-sm">
      <div className="flex items-baseline gap-2">
        {selection && (
          <input
            type="checkbox"
            className="mt-1 size-4 shrink-0 accent-cedar"
            checked={selection.checked}
            aria-label={selection.ariaLabel}
            onChange={(event) => selection.onChange(event.target.checked)}
          />
        )}
        {onToggleExpanded && (
          <button
            type="button"
            aria-expanded={expanded}
            aria-label={`${expanded ? 'Collapse' : 'Expand'}: ${title}`}
            onClick={onToggleExpanded}
            className="shrink-0 rounded px-1 font-heading text-xs text-pacific/80 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-whulge"
          >
            <span aria-hidden="true">{expanded ? '▾' : '▸'}</span>
          </button>
        )}
        <p
          className={
            'min-w-0 flex-1 font-heading text-sm font-bold text-pacific '
            + (expanded ? '' : 'truncate')
          }
        >
          {title}
        </p>
        {/* pacific/80, not /70: at 12px on white /70 is 4.44:1 and fails AA. */}
        {timestamp && (
          <span className="shrink-0 text-xs text-pacific/80">{timestamp}</span>
        )}
      </div>

      {expanded && expandedContent ? (
        <div className={`${indent} mt-1`}>{expandedContent}</div>
      ) : (
        detail && <p className={`${indent} truncate text-sm text-pacific/80`}>{detail}</p>
      )}

      <div className={`${indent} mt-1 flex flex-wrap items-center justify-between gap-x-3 gap-y-1`}>
        <p className="min-w-0 break-words text-xs text-pacific/80">
          {caseNumber && (
            <span className="font-semibold text-pacific">{caseNumber}</span>
          )}
          {shownTags.length > 0
            ? `${caseNumber ? ' · ' : ''}${shownTags.join(' · ')}`
            : ''}
        </p>
        <div className="flex min-w-0 flex-wrap items-center gap-1.5">
          {alert && <Badge tone={alert.tone}>{alert.label}</Badge>}
          {primaryAction && (
            <Link
              to={primaryAction.to}
              className={PRIMARY_LINK_CLASS}
              aria-label={primaryAction.ariaLabel}
            >
              {primaryAction.label}
            </Link>
          )}
          {secondaryActions}
        </div>
      </div>
      {children}
    </li>
  )
}

export function InboxRowList({ children }: { children: ReactNode }) {
  return <ol className="space-y-2">{children}</ol>
}
