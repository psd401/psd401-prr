// ABOUTME: Calendar-date helpers pinned to the district's Pacific time zone.
// ABOUTME: Compliance dates must not roll over early at the UTC date boundary.
const PACIFIC_TIME_ZONE = 'America/Los_Angeles'

const PACIFIC_DATE_FORMATTER = new Intl.DateTimeFormat('en-US', {
  timeZone: PACIFIC_TIME_ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
})

export function pacificDateString(date = new Date()): string {
  const parts = Object.fromEntries(
    PACIFIC_DATE_FORMATTER
      .formatToParts(date)
      .filter((part) => part.type !== 'literal')
      .map((part) => [part.type, part.value]),
  )
  return `${parts.year}-${parts.month}-${parts.day}`
}

export function addCalendarDays(date: string, offset: number): string {
  const [year = NaN, month = NaN, day = NaN] = date.split('-').map(Number)
  const shifted = new Date(Date.UTC(year, month - 1, day + offset))
  return shifted.toISOString().slice(0, 10)
}

const DAY_FORMAT = new Intl.DateTimeFormat('en-US', {
  timeZone: 'UTC',
  month: 'short',
  day: 'numeric',
  year: 'numeric',
})

const MOMENT_FORMAT = new Intl.DateTimeFormat('en-US', {
  timeZone: PACIFIC_TIME_ZONE,
  month: 'short',
  day: 'numeric',
  year: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
})

/**
 * One date style for staff screens: "Nov 6, 2026". A bare YYYY-MM-DD is a
 * calendar date and is shown as written; a timestamp is shown as its Pacific
 * date, so a deadline never moves a day at the UTC boundary.
 */
export function formatDay(value: string | null | undefined): string {
  if (!value) return '—'
  let day = value
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const moment = new Date(value)
    if (Number.isNaN(moment.getTime())) return value
    day = pacificDateString(moment)
  }
  const parsed = new Date(`${day}T00:00:00Z`)
  return Number.isNaN(parsed.getTime()) ? value : DAY_FORMAT.format(parsed)
}

/** A moment in time, in Pacific: "Nov 6, 2026, 6:38 AM". */
export function formatMoment(value: string | null | undefined): string {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : MOMENT_FORMAT.format(parsed)
}
