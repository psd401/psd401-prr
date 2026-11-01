import { QueryClient } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import {
  invalidateBoundedInfiniteQueries,
  markBoundedInfiniteQueriesStale,
} from './boundedInfiniteCache'

describe('invalidateBoundedInfiniteQueries', () => {
  it('keeps only the first loaded page before refreshing matching infinite queries', async () => {
    const client = new QueryClient()
    const key = ['messages', 'PRR-2026-0001', 'messages'] as const
    client.setQueryData(key, {
      pages: [{ results: ['one'] }, { results: ['two'] }, { results: ['three'] }],
      pageParams: [null, 'page-2', 'page-3'],
    })
    client.setQueryData(['messages', 'PRR-2026-0002'], {
      pages: [{ results: ['other-one'] }, { results: ['other-two'] }],
      pageParams: [null, 'other-page-2'],
    })
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    const cancel = vi.spyOn(client, 'cancelQueries')

    await invalidateBoundedInfiniteQueries(client, ['messages', 'PRR-2026-0001'])

    expect(client.getQueryData(key)).toEqual({
      pages: [{ results: ['one'] }],
      pageParams: [null],
    })
    expect(client.getQueryData(['messages', 'PRR-2026-0002'])).toEqual({
      pages: [{ results: ['other-one'] }, { results: ['other-two'] }],
      pageParams: [null, 'other-page-2'],
    })
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ['messages', 'PRR-2026-0001'],
    })
    expect(cancel).toHaveBeenCalledWith({
      queryKey: ['messages', 'PRR-2026-0001'],
    })
  })

  it('marks a trimmed inactive scope stale without replaying loaded cursors', () => {
    const client = new QueryClient()
    const key = ['message-threads', 'PRR-2026-0001'] as const
    client.setQueryData(key, {
      pages: [{ results: ['one'] }, { results: ['two'] }, { results: ['three'] }],
      pageParams: [null, 'page-2', 'page-3'],
    })
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    const cancel = vi.spyOn(client, 'cancelQueries')

    markBoundedInfiniteQueriesStale(client, key)

    expect(client.getQueryData(key)).toEqual({
      pages: [{ results: ['one'] }],
      pageParams: [null],
    })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: key, refetchType: 'none' })
    expect(cancel).toHaveBeenCalledWith({ queryKey: key })
    expect(client.getQueryState(key)?.isInvalidated).toBe(true)
  })
})
