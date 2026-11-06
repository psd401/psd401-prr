// ABOUTME: One date style on staff screens, pinned to the district's zone so a
// ABOUTME: deadline never shifts a day at the UTC boundary.
import { expect, it } from 'vitest'
import { formatDay, formatMoment } from './date'

it('shows a calendar date as written', () => {
  expect(formatDay('2026-09-28')).toBe('Sep 28, 2026')
  expect(formatDay('2027-01-06')).toBe('Jan 6, 2027')
})

it('shows a timestamp as its Pacific date', () => {
  // 03:00 UTC on Nov 7 is still Nov 6 in Gig Harbor.
  expect(formatDay('2026-11-07T03:00:00Z')).toBe('Nov 6, 2026')
})

it('shows a moment in Pacific time', () => {
  expect(formatMoment('2026-11-06T14:38:01Z')).toBe('Nov 6, 2026, 6:38 AM')
})

it('says nothing is there, and passes through what it cannot read', () => {
  expect(formatDay(null)).toBe('—')
  expect(formatMoment(undefined)).toBe('—')
  expect(formatDay('not a date')).toBe('not a date')
  expect(formatMoment('not a date')).toBe('not a date')
})
