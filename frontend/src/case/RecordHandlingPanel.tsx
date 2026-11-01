// ABOUTME: The review surface for records that are not PDFs — conversion,
// ABOUTME: audio/video timelines, and decisions on files nothing can open.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api, unwrap, type Page } from '../api/client'
import {
  Badge,
  Button,
  EmptyState,
  ErrorNote,
  Field,
  Input,
  Select,
  Spinner,
  Textarea,
  useToast,
} from '../ui'

export interface HandledRecord {
  id: string
  original_filename: string
  content_type: string
  review_state: string
  handling: 'preview' | 'convert' | 'media' | 'opaque'
  handling_label: string
  handling_detail: string
  derivative_state: string
  derivative_detail: string
  reviewable: boolean
}

interface Exemption {
  id: number
  citation: string
  short_name: string
}

interface MediaRedactionRow {
  id: string
  starts_at: number
  ends_at: number
  treatment: string
  exemption: number
  citation: string
  explanation: string
  voided_at: string | null
}

const TREATMENTS = [
  { value: 'obscure_region', label: 'Obscure a region of the frame' },
  { value: 'obscure_frame', label: 'Obscure the whole frame' },
  { value: 'mute_audio', label: 'Mute the audio' },
  { value: 'cut', label: 'Remove the interval entirely' },
]

const OPAQUE_BASES = [
  { value: 'opened_elsewhere', label: 'Opened outside the application and inspected' },
  { value: 'described_by_custodian', label: 'Described by the custodian who holds it' },
  { value: 'not_inspectable', label: 'Could not be inspected by any available means' },
]

const REVIEW_OUTCOMES = [
  { value: 'responsive', label: 'Responsive' },
  { value: 'non_responsive', label: 'Not responsive' },
  { value: 'withheld', label: 'Withheld in full' },
]

/** The reasoning is the entire evidentiary basis for deciding an unopenable
 *  record, so the server refuses anything shorter. */
const MIN_OPAQUE_REASONING = 80

export function RecordHandlingPanel({
  record,
  canReview,
}: {
  record: HandledRecord
  canReview: boolean
}) {
  const qc = useQueryClient()
  const toast = useToast()
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['record-items'] })
    qc.invalidateQueries({ queryKey: ['record-item-target'] })
  }

  const convert = useMutation({
    mutationFn: () => api(
      `/api/staff/record-items/${record.id}/build-derivative/`,
      { method: 'POST' },
    ),
    onSuccess: () => {
      toast('success', 'Rendition requested.')
      invalidate()
    },
    onError: (error) => toast('error', String(error)),
  })

  if (record.handling === 'convert') {
    const failed = record.derivative_state === 'failed'
      || record.derivative_state === 'unsupported'
    return (
      <section className="space-y-3">
        <Badge tone={failed ? 'red' : 'amber'}>{record.handling_label}</Badge>
        <p className="text-sm text-pacific">
          {record.derivative_detail || record.handling_detail}
        </p>
        <p className="text-xs text-pacific/80">
          The original file is kept exactly as collected. Redactions are burned
          into the rendition, which is the copy a requester receives — releasing
          the original would ship tracked changes, comments, and author details
          that no redaction box ever covered.
        </p>
        {canReview && (
          <Button onClick={() => convert.mutate()} disabled={convert.isPending}>
            {record.derivative_state === 'pending'
              ? 'Retry preparing for review'
              : 'Prepare for review'}
          </Button>
        )}
      </section>
    )
  }

  if (record.handling === 'media') {
    return <MediaReviewPanel record={record} canReview={canReview} />
  }

  if (record.handling === 'opaque') {
    return <OpaquePanel record={record} canReview={canReview} />
  }

  return null
}

function MediaReviewPanel({
  record,
  canReview,
}: {
  record: HandledRecord
  canReview: boolean
}) {
  const qc = useQueryClient()
  const toast = useToast()
  const [startsAt, setStartsAt] = useState('')
  const [endsAt, setEndsAt] = useState('')
  const [treatment, setTreatment] = useState('mute_audio')
  const [exemption, setExemption] = useState('')
  const [explanation, setExplanation] = useState('')

  const exemptions = useQuery({
    queryKey: ['exemptions', 'active', 'media-review'],
    queryFn: () => api<Page<Exemption> | Exemption[]>(
      '/api/staff/exemptions/?active=true',
    ),
  })
  const redactions = useQuery({
    queryKey: ['media-redactions', record.id],
    queryFn: () => api<Page<MediaRedactionRow> | MediaRedactionRow[]>(
      `/api/staff/media-redactions/?record_item=${record.id}`,
    ),
  })

  const mark = useMutation({
    mutationFn: () => api('/api/staff/media-redactions/', {
      method: 'POST',
      body: {
        record_item: record.id,
        starts_at: Number(startsAt),
        ends_at: Number(endsAt),
        treatment,
        exemption: Number(exemption),
        explanation,
      },
    }),
    onSuccess: () => {
      toast('success', 'Interval marked.')
      setStartsAt('')
      setEndsAt('')
      setExplanation('')
      qc.invalidateQueries({ queryKey: ['media-redactions', record.id] })
    },
    onError: (error) => toast('error', String(error)),
  })

  const render = useMutation({
    mutationFn: () => api(
      `/api/staff/record-items/${record.id}/render-media-redactions/`,
      { method: 'POST' },
    ),
    onSuccess: () => {
      toast('success', 'Released copy produced.')
      qc.invalidateQueries({ queryKey: ['record-items'] })
      qc.invalidateQueries({ queryKey: ['record-item-target'] })
    },
    onError: (error) => toast('error', String(error)),
  })

  const active = redactions.data
    ? unwrap(redactions.data).filter((row) => row.voided_at === null)
    : []
  const complete = startsAt !== '' && endsAt !== '' && exemption !== ''
    && Number(endsAt) > Number(startsAt)

  return (
    <section className="space-y-4">
      <div>
        <Badge tone="blue">{record.handling_label}</Badge>
        <p className="mt-2 text-sm text-pacific">{record.handling_detail}</p>
      </div>

      <video
        controls
        preload="metadata"
        className="max-h-[50vh] w-full rounded-md border border-driftwood bg-black"
        src={`/api/staff/record-items/${record.id}/stream/`}
      >
        <track kind="captions" />
      </video>

      <p className="text-xs text-pacific/80">
        Mark every interval that must be withheld, then produce the released
        copy. Nothing is detected automatically: an automated blur that misses
        one frame discloses the person it was meant to protect, and no reviewer
        can verify that across thousands of frames.
      </p>

      {canReview && (
        <form
          className="grid gap-3 rounded-md border border-driftwood p-3 sm:grid-cols-2"
          onSubmit={(event) => {
            event.preventDefault()
            mark.mutate()
          }}
        >
          <Field label="Starts at (seconds)">
            <Input
              type="number" min="0" step="0.01" value={startsAt}
              onChange={(event) => setStartsAt(event.target.value)}
            />
          </Field>
          <Field label="Ends at (seconds)">
            <Input
              type="number" min="0" step="0.01" value={endsAt}
              onChange={(event) => setEndsAt(event.target.value)}
            />
          </Field>
          <Field label="Treatment">
            <Select
              value={treatment}
              onChange={(event) => setTreatment(event.target.value)}
            >
              {TREATMENTS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </Select>
          </Field>
          <Field
            label="Exemption"
            hint="Required. The RCW 42.56.210(3) log is built from these citations."
          >
            <Select
              value={exemption}
              onChange={(event) => setExemption(event.target.value)}
            >
              <option value="">Select an exemption…</option>
              {(exemptions.data ? unwrap(exemptions.data) : []).map((row) => (
                <option key={row.id} value={row.id}>
                  {row.citation} — {row.short_name}
                </option>
              ))}
            </Select>
          </Field>
          <div className="sm:col-span-2">
            <Field label="Explanation">
              <Textarea
                rows={2} value={explanation}
                onChange={(event) => setExplanation(event.target.value)}
              />
            </Field>
          </div>
          <div className="sm:col-span-2">
            <Button type="submit" disabled={!complete || mark.isPending}>
              Mark interval
            </Button>
          </div>
        </form>
      )}

      {redactions.isPending && <Spinner label="Loading marked intervals…" />}
      {redactions.isError && <ErrorNote error={redactions.error} />}
      {redactions.isSuccess && (
        active.length === 0 ? (
          <EmptyState>No intervals marked yet.</EmptyState>
        ) : (
          <ul className="space-y-2">
            {active.map((row) => (
              <li
                key={row.id}
                className="rounded-md border border-driftwood px-3 py-2 text-sm text-pacific"
              >
                <span className="font-mono">
                  {row.starts_at.toFixed(2)}s – {row.ends_at.toFixed(2)}s
                </span>
                {' · '}
                {TREATMENTS.find((t) => t.value === row.treatment)?.label ?? row.treatment}
                {' · '}
                <Badge tone="neutral">{row.citation}</Badge>
              </li>
            ))}
          </ul>
        )
      )}

      {canReview && (
        <div className="space-y-1">
          <Button
            onClick={() => render.mutate()}
            disabled={
              active.length === 0
              || record.review_state !== 'responsive'
              || render.isPending
            }
          >
            Produce released copy
          </Button>
          {record.review_state !== 'responsive' && (
            <p className="text-xs text-pacific/80">
              Mark the record responsive before producing a released copy.
            </p>
          )}
        </div>
      )}
    </section>
  )
}

function OpaquePanel({
  record,
  canReview,
}: {
  record: HandledRecord
  canReview: boolean
}) {
  const qc = useQueryClient()
  const toast = useToast()
  const [basis, setBasis] = useState('')
  const [reasoning, setReasoning] = useState('')
  const [outcome, setOutcome] = useState('')
  const [exemption, setExemption] = useState('')

  const exemptions = useQuery({
    queryKey: ['exemptions', 'active', 'opaque-review'],
    queryFn: () => api<Page<Exemption> | Exemption[]>(
      '/api/staff/exemptions/?active=true',
    ),
  })

  const decide = useMutation({
    mutationFn: () => api(
      `/api/staff/record-items/${record.id}/opaque-disposition/`,
      {
        method: 'POST',
        body: {
          basis,
          reasoning,
          review_state: outcome,
          withholding_exemption: exemption ? Number(exemption) : null,
        },
      },
    ),
    onSuccess: () => {
      toast('success', 'Decision recorded.')
      qc.invalidateQueries({ queryKey: ['record-items'] })
      qc.invalidateQueries({ queryKey: ['record-item-target'] })
    },
    onError: (error) => toast('error', String(error)),
  })

  const shortfall = MIN_OPAQUE_REASONING - reasoning.trim().length
  const complete = basis !== '' && outcome !== '' && shortfall <= 0
    && (outcome !== 'withheld' || exemption !== '')

  return (
    <section className="space-y-3">
      <Badge tone="red">{record.handling_label}</Badge>
      <p className="text-sm text-pacific">{record.handling_detail}</p>
      <p className="text-xs text-pacific/80">
        This record still needs a decision. Record what was attempted and what
        was concluded — that reasoning is the whole evidentiary basis, because
        nobody could open the file itself.
      </p>

      {canReview && (
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault()
            decide.mutate()
          }}
        >
          <Field label="Basis">
            <Select value={basis} onChange={(event) => setBasis(event.target.value)}>
              <option value="">Select a basis…</option>
              {OPAQUE_BASES.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </Select>
          </Field>
          <Field
            label="Reasoning"
            hint={
              shortfall > 0
                ? `${shortfall} more characters needed.`
                : 'Recorded permanently against this record.'
            }
          >
            <Textarea
              rows={4} value={reasoning}
              onChange={(event) => setReasoning(event.target.value)}
            />
          </Field>
          <Field label="Outcome">
            <Select value={outcome} onChange={(event) => setOutcome(event.target.value)}>
              <option value="">Select an outcome…</option>
              {REVIEW_OUTCOMES.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </Select>
          </Field>
          {outcome === 'withheld' && (
            <Field
              label="Exemption"
              hint="Withholding requires a citation, exactly as it does for a record that could be read."
            >
              <Select
                value={exemption}
                onChange={(event) => setExemption(event.target.value)}
              >
                <option value="">Select an exemption…</option>
                {(exemptions.data ? unwrap(exemptions.data) : []).map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.citation} — {row.short_name}
                  </option>
                ))}
              </Select>
            </Field>
          )}
          <Button type="submit" disabled={!complete || decide.isPending}>
            Record decision
          </Button>
        </form>
      )}
    </section>
  )
}
