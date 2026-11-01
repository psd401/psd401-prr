// ABOUTME: The case table's sort vocabulary, read from and written to the URL.
// ABOUTME: A key the server does not whitelist must never reach a request.
import { describe, expect, it } from 'vitest'
import { caseSortParam, nextCaseSort, parseCaseSort } from './caseSort'

describe('parseCaseSort', () => {
  it('reads an ascending and a descending column', () => {
    expect(parseCaseSort('received')).toEqual({ key: 'received', direction: 'asc' })
    expect(parseCaseSort('-received')).toEqual({ key: 'received', direction: 'desc' })
  })

  it('is empty when nothing is sorted', () => {
    expect(parseCaseSort(null)).toBeNull()
    expect(parseCaseSort('')).toBeNull()
  })

  it('drops a key the server does not whitelist', () => {
    // A stale bookmark or a hand-edited URL must not leave the header
    // claiming an order the server never applied.
    expect(parseCaseSort('requester__email')).toBeNull()
    expect(parseCaseSort('-description')).toBeNull()
    expect(parseCaseSort('-')).toBeNull()
  })
})

describe('caseSortParam', () => {
  it('round-trips through the ordering parameter', () => {
    expect(caseSortParam(parseCaseSort('-due'))).toBe('-due')
    expect(caseSortParam(parseCaseSort('due'))).toBe('due')
    expect(caseSortParam(null)).toBe('')
  })
})

describe('nextCaseSort', () => {
  it('cycles ascending, descending, then back to the view default', () => {
    expect(nextCaseSort(null, 'due')).toBe('due')
    expect(nextCaseSort({ key: 'due', direction: 'asc' }, 'due')).toBe('-due')
    // The third state is what makes the flagged-first default reachable
    // again after an officer has sorted by something else.
    expect(nextCaseSort({ key: 'due', direction: 'desc' }, 'due')).toBe('')
  })

  it('starts a different column ascending', () => {
    expect(nextCaseSort({ key: 'due', direction: 'desc' }, 'status')).toBe('status')
  })
})
