// ABOUTME: Shared pieces for drawing redaction boxes on a page preview: the
// ABOUTME: record shapes and the pointer-to-box math.
import type { PointerEvent as ReactPointerEvent } from 'react'

export interface NormalizedBox {
  x0: number
  y0: number
  x1: number
  y1: number
}

export interface Exemption {
  id: number
  citation: string
  short_name: string
  requires_second_review: boolean
  active: boolean
}

export interface Redaction {
  id: number
  record_item: string
  page_number: number
  x0: number
  y0: number
  x1: number
  y1: number
  exemption: number
  citation: string
  exemption_name: string
  requires_second_review: boolean
  explanation: string
  created_by_username: string | null
  created_at: string
  voided_at: string | null
  void_reason: string
}

export interface PreviewMetadata {
  page_count: number
}

export interface RedactionProposalDecision {
  outcome: 'accepted' | 'rejected'
  decided_by_username: string
  reason: string
  redaction: number | null
  decided_at: string
}

export interface RedactionProposal extends NormalizedBox {
  id: string
  advisory_run: string
  record_item: string
  page_number: number
  exemption: number
  citation: string
  exemption_name: string
  requires_second_review: boolean
  confidence: number
  rationale: string
  source_span_ids: string[]
  source_text_sha256: string
  created_at: string
  decision: RedactionProposalDecision | null
}

function clamp(value: number): number {
  return Math.max(0, Math.min(1, value))
}

export function pointerPosition(event: ReactPointerEvent<HTMLElement>) {
  const bounds = event.currentTarget.getBoundingClientRect()
  return {
    x: clamp((event.clientX - bounds.left) / bounds.width),
    y: clamp((event.clientY - bounds.top) / bounds.height),
  }
}

export function normalizedBox(start: { x: number; y: number }, end: { x: number; y: number }) {
  const round = (value: number) => Number(value.toFixed(4))
  return {
    x0: round(Math.min(start.x, end.x)),
    y0: round(Math.min(start.y, end.y)),
    x1: round(Math.max(start.x, end.x)),
    y1: round(Math.max(start.y, end.y)),
  }
}

/** A box too small to mean anything: a stray click, not a redaction. */
export function tooSmall(box: NormalizedBox): boolean {
  return box.x1 - box.x0 < 0.005 || box.y1 - box.y0 < 0.005
}
