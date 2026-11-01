import { describe, expect, it } from 'vitest'

import { scopeCountNotice } from './scopeCountNotice'

const complete = {
  state: 'complete' as const,
  total_count: 29380,
  completed_at: '2026-09-22T17:05:00Z',
  trigger: 'automatic' as const,
}

describe('scopeCountNotice', () => {
  it('says nothing when the letter matches the latest count', () => {
    expect(scopeCountNotice('29380', complete)).toBeNull()
  })

  it('names both numbers when the letter disagrees with the latest count', () => {
    expect(scopeCountNotice('4200', complete)).toBe(
      'This letter says 4,200 items. The latest automatic count found 29,380. '
      + 'Sending is not blocked; check the number before you approve.',
    )
  })

  it('warns that a count still running may change the number', () => {
    expect(
      scopeCountNotice('4200', { ...complete, state: 'running', total_count: null }),
    ).toBe(
      'A count is still running for this request, so the number in this letter '
      + 'may not match its result.',
    )
  })

  it('says nothing without a count or a usable number to compare', () => {
    expect(scopeCountNotice('4200', null)).toBeNull()
    expect(scopeCountNotice('', complete)).toBeNull()
    expect(scopeCountNotice('4200', { ...complete, state: 'failed', total_count: null }))
      .toBeNull()
  })

  it('names an officer-run count as the officer count', () => {
    expect(scopeCountNotice('1', { ...complete, trigger: 'manual' })).toContain(
      'The latest officer count found 29,380.',
    )
  })
})
