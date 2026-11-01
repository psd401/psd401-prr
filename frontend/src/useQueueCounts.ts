// ABOUTME: Lightweight role-scoped shell count polling.
// ABOUTME: Polling pauses while hidden and refreshes when the page returns or gains focus.
import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import type { StaffRole } from './api/auth'
import { api } from './api/client'
import type { WorkItemCounts } from './work'

const COUNT_ROLES: StaffRole[] = ['records_officer', 'liaison', 'reviewer', 'admin']

export function useQueueCounts(roles: StaffRole[]) {
  const [visible, setVisible] = useState(() => document.visibilityState === 'visible')
  const enabled = roles.some((role) => COUNT_ROLES.includes(role))
  const query = useQuery<WorkItemCounts>({
    queryKey: ['work-item-counts'],
    queryFn: () => api<WorkItemCounts>('/api/staff/work-items/counts/'),
    enabled,
    refetchInterval: visible ? 30_000 : false,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
  })
  const { refetch } = query

  useEffect(() => {
    function updateVisibility() {
      const nextVisible = document.visibilityState === 'visible'
      setVisible(nextVisible)
      if (nextVisible && enabled) void refetch()
    }
    document.addEventListener('visibilitychange', updateVisibility)
    return () => document.removeEventListener('visibilitychange', updateVisibility)
  }, [enabled, refetch])

  useEffect(() => {
    function refreshOnFocus() {
      if (document.visibilityState === 'visible' && enabled) void refetch()
    }
    window.addEventListener('focus', refreshOnFocus)
    return () => window.removeEventListener('focus', refreshOnFocus)
  }, [enabled, refetch])

  return query
}
