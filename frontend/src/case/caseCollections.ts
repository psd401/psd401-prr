// ABOUTME: A case's collection tasks with their source and record counts,
// ABOUTME: shared by the Collect stage cards and the Scope step (#45).
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

export interface CollectionCardTask {
  id: number
  kind: string
  state: string
  source_label: string
  records_created: number
  records_unreviewed: number
  search_terms: string
  created_at: string
  completed_at: string | null
}

interface Page<T> { results: T[] }

export function collectionTasksQueryKey(number: string) {
  return ['collection-tasks', 'case', number] as const
}

/** The case's collections, shared by the Collect cards and the Scope step. */
export function useCaseCollections(number: string, enabled = true) {
  return useQuery({
    queryKey: collectionTasksQueryKey(number),
    queryFn: async () => {
      const data = await api<Page<CollectionCardTask> | CollectionCardTask[]>(
        `/api/staff/collection-tasks/?request=${encodeURIComponent(number)}`,
      )
      return Array.isArray(data) ? data : data.results
    },
    enabled,
  })
}
