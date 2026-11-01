// ABOUTME: Polite, non-disruptive announcements for refreshed shell queue counts.
// ABOUTME: Initial counts remain silent so first render does not create screen-reader noise.
import { useEffect, useRef, useState } from 'react'
import type { WorkItemCounts } from './work'

function announcedCounts(counts: WorkItemCounts) {
  return [
    counts.personal,
    counts.team,
    counts.review,
    counts.inbox_needs_attention,
    counts.inbox_delivery_exceptions,
  ].join(':')
}

function countAnnouncement(counts: WorkItemCounts) {
  return `Queue counts updated. ${counts.personal} personal, ${counts.team} team, ${counts.review} review, ${counts.inbox_needs_attention} Inbox needs attention, and ${counts.inbox_delivery_exceptions} delivery exceptions.`
}

export function QueueCountStatus({ counts }: { counts?: WorkItemCounts }) {
  const previous = useRef<string | null>(null)
  const [message, setMessage] = useState('')

  useEffect(() => {
    if (!counts) return
    const current = announcedCounts(counts)
    if (previous.current !== null && previous.current !== current) {
      setMessage(countAnnouncement(counts))
    }
    previous.current = current
  }, [counts])

  return (
    <span
      role="status"
      aria-label="Queue count updates"
      aria-live="polite"
      aria-atomic="true"
      className="sr-only"
    >
      {message}
    </span>
  )
}
