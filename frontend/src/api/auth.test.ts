import { QueryClient } from '@tanstack/react-query'
import { beforeEach, describe, expect, it } from 'vitest'
import { clearStaffSessionState } from './auth'

beforeEach(() => {
  sessionStorage.clear()
})

describe('staff session isolation', () => {
  it('clears prior staff API data and persisted message searches', () => {
    const client = new QueryClient()
    client.setQueryData(['me'], { username: 'first-officer' })
    client.setQueryData(['messages', 'PRR-2026-0001'], {
      results: [{ subject: 'Prior account case data' }],
    })
    client.setQueryData(
      ['correspondence-detail', 'PRR-2026-0001', 'message-1'],
      { received_body: 'Prior account full legal detail' },
    )
    sessionStorage.setItem(
      'message-search:first-officer:PRR-2026-0001',
      'sensitive phrase',
    )
    sessionStorage.setItem('unrelated-preference', 'preserved')

    clearStaffSessionState(client)

    expect(client.getQueryData(['me'])).toEqual({ username: 'first-officer' })
    expect(client.getQueryData(['messages', 'PRR-2026-0001'])).toBeUndefined()
    expect(client.getQueryData([
      'correspondence-detail',
      'PRR-2026-0001',
      'message-1',
    ])).toBeUndefined()
    expect(sessionStorage.getItem(
      'message-search:first-officer:PRR-2026-0001',
    )).toBeNull()
    expect(sessionStorage.getItem('unrelated-preference')).toBe('preserved')
  })
})
