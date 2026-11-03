// ABOUTME: Auth hooks — current-user query plus login/logout mutations that
// ABOUTME: drive the session-gated app shell.
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from '@tanstack/react-query'
import { api } from './client'

export interface StaffUser {
  authenticated: true
  username: string
  email: string
  first_name: string
  last_name: string
  is_superuser: boolean
  roles: StaffRole[]
}

export type StaffRole =
  | 'records_officer'
  | 'intake_clerk'
  | 'liaison'
  | 'reviewer'
  | 'admin'

export function authBootstrap() {
  const root = document.getElementById('root')
  return {
    mode: root?.dataset.authMode === 'google' ? 'google' : 'local',
    googleLoginUrl:
      root?.dataset.googleLoginUrl ?? '/api/staff/auth/oidc/authenticate/',
  } as const
}

// The OIDC view keeps a same-site ``next`` in the session and the callback
// returns there, so a deep link opened while signed out survives sign-in.
export function googleLoginHref(
  base: string,
  location: Pick<Location, 'pathname' | 'search' | 'hash'>,
) {
  const params = new URLSearchParams(location.search)
  params.delete('login')
  const search = params.toString()
  const next = `${location.pathname}${search ? `?${search}` : ''}${location.hash}`
  if (next === '/') return base
  const separator = base.includes('?') ? '&' : '?'
  return `${base}${separator}next=${encodeURIComponent(next)}`
}

export function useMe() {
  return useQuery<StaffUser>({
    queryKey: ['me'],
    queryFn: () => api<StaffUser>('/api/staff/auth/me'),
    retry: false,
    staleTime: 5 * 60 * 1000,
  })
}

export function clearStaffSessionState(qc: QueryClient) {
  qc.removeQueries({
    predicate: (query) => query.queryKey[0] !== 'me',
  })
  for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
    const key = sessionStorage.key(index)
    if (key?.startsWith('message-search:')) sessionStorage.removeItem(key)
  }
}

export function useLogin() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (creds: { username: string; password: string }) =>
      api<StaffUser>('/api/staff/auth/login', { method: 'POST', body: creds }),
    onSuccess: (user) => {
      clearStaffSessionState(qc)
      qc.setQueryData(['me'], user)
    },
  })
}

export function useLogout() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => api('/api/staff/auth/logout', { method: 'POST' }),
    onSuccess: async () => {
      clearStaffSessionState(qc)
      await qc.resetQueries({ queryKey: ['me'], exact: true })
    },
  })
}
