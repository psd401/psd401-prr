import type { InfiniteData, QueryClient, QueryKey } from '@tanstack/react-query'

function isInfiniteData(value: unknown): value is InfiniteData<unknown> {
  return Boolean(
    value
    && typeof value === 'object'
    && Array.isArray((value as InfiniteData<unknown>).pages)
    && Array.isArray((value as InfiniteData<unknown>).pageParams),
  )
}

export async function invalidateBoundedInfiniteQueries(
  queryClient: QueryClient,
  queryKey: QueryKey,
) {
  await queryClient.cancelQueries({ queryKey })
  queryClient.setQueriesData({ queryKey }, (current) => {
    if (!isInfiniteData(current) || current.pages.length <= 1) return current
    return {
      ...current,
      pages: current.pages.slice(0, 1),
      pageParams: current.pageParams.slice(0, 1),
    }
  })
  await queryClient.invalidateQueries({ queryKey })
}

export function markBoundedInfiniteQueriesStale(
  queryClient: QueryClient,
  queryKey: QueryKey,
) {
  void queryClient.cancelQueries({ queryKey })
  queryClient.setQueriesData({ queryKey }, (current) => {
    if (!isInfiniteData(current) || current.pages.length <= 1) return current
    return {
      ...current,
      pages: current.pages.slice(0, 1),
      pageParams: current.pageParams.slice(0, 1),
    }
  })
  void queryClient.invalidateQueries({ queryKey, refetchType: 'none' })
}
