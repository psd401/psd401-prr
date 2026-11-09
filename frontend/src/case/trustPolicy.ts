// ABOUTME: Trust-ladder policy and batch shapes (#39), shared by the cluster
// ABOUTME: view and the Ready-to-discard card.
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

export interface TrustPolicy {
  responsive: 'suggest' | 'batch'
  non_responsive: 'suggest' | 'batch' | 'auto'
  min_confidence: number
  batch_sample_size: number
  elusion_sample_size: number
  discard_candidates: number
}

export interface ReviewBatch {
  id: number
  kind: 'cluster' | 'discard'
  label: string
  proposed_state: 'responsive' | 'non_responsive'
  record_count: number
  samples: {
    id: string
    original_filename: string
    advisory_suggested_state: string | null
    advisory_confidence: number | null
  }[]
  outcome: 'open' | 'applied' | 'blocked'
  applied_count: number
  skipped_count: number
}

export function useTrustPolicy(request: string) {
  return useQuery({
    queryKey: ['review-trust-policy', request],
    queryFn: () => api<TrustPolicy>(`/api/staff/review-batches/policy/?request=${encodeURIComponent(request)}`),
    enabled: request !== '',
  })
}
