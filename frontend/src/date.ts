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
