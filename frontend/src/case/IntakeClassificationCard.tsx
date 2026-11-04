// ABOUTME: The model's suggested intake label for a new case, for a Records
// ABOUTME: Officer to confirm (opening the ordinary action) or correct.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import { api } from '../api/client'
import { Badge, Button, Card, ErrorNote, Field, Select, Spinner, Textarea } from '../ui'
import { INTAKE_LABEL_TEXT, type IntakeLabel } from './intakeLabels'

interface IntakeRun {
  id: string
  state: 'pending' | 'running' | 'complete' | 'failed'
  model_identifier: string
  proposal: {
    label?: IntakeLabel
    target_case_number?: string | null
    confidence?: number
    evidence?: string
  }
  failure_detail: string
}

interface IntakeLatest {
  enabled: boolean
  latest: IntakeRun | null
  feedback: { outcome: 'confirmed' | 'dismissed'; final_label: IntakeLabel } | null
}

/**
 * Advisory only. "Correct" records that the officer agrees and hands the
 * label to `onConfirm`, which opens the ordinary merge, close, or add-to-case
 * dialog prefilled -- nothing is merged or closed from here. "Not correct"
 * records what the officer judged it to be instead.
 */
export function IntakeClassificationCard({
  number,
  onConfirm,
}: {
  number: string
  onConfirm: (label: IntakeLabel, target: string | null) => void
}) {
  const qc = useQueryClient()
  const [correcting, setCorrecting] = useState(false)
  const [finalLabel, setFinalLabel] = useState<IntakeLabel>('new_request')
  const [reason, setReason] = useState('')

  const latest = useQuery({
    queryKey: ['advisory-intake', number],
    queryFn: () => api<IntakeLatest>(`/api/staff/requests/${number}/advisory-intake/`),
    refetchInterval: (query) => {
      const state = query.state.data?.latest?.state
      return state === 'pending' || state === 'running' ? 3000 : false
    },
  })
  const run = useMutation({
    mutationFn: () => api<IntakeRun>(
      `/api/staff/requests/${number}/advisory-intake/`,
      { method: 'POST' },
    ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['advisory-intake', number] }),
  })
  const answer = useMutation({
    mutationFn: (body: { outcome: 'confirmed' | 'dismissed'; final_label: IntakeLabel; reason: string }) =>
      api(`/api/staff/requests/${number}/advisory-intake/feedback/`, { method: 'POST', body }),
    onSuccess: () => {
      setCorrecting(false)
      qc.invalidateQueries({ queryKey: ['advisory-intake', number] })
    },
  })

  if (latest.isPending) return <Spinner />
  if (latest.isError) return <ErrorNote error={latest.error} />
  if (!latest.data.enabled) return null

  const current = latest.data.latest
  const proposal = current?.state === 'complete' ? current.proposal : null
  const label = proposal?.label
  const target = proposal?.target_case_number ?? null

  return (
    <Card title="Suggested intake label">
      <div className="space-y-3 text-sm text-pacific">
        <p className="text-xs text-pacific/80">
          Advisory only. It merges, closes, and sends nothing; you apply it through the
          ordinary action.
        </p>
        {!current && (
          <Button variant="ghost" onClick={() => run.mutate()} disabled={run.isPending}>
            Suggest a label
          </Button>
        )}
        {current && (current.state === 'pending' || current.state === 'running') && (
          <p>Classifying…</p>
        )}
        {current?.state === 'failed' && (
          <p className="text-red-700">
            The classification failed: {current.failure_detail || 'no usable result'}.
          </p>
        )}
        {label && (
          <>
            <p className="flex flex-wrap items-center gap-2">
              <Badge tone="blue">AI</Badge>
              <strong>{INTAKE_LABEL_TEXT[label]}</strong>
              {target && (
                <Link className="font-semibold underline" to={`/requests/${target}`}>
                  {target}
                </Link>
              )}
              {typeof proposal?.confidence === 'number' && (
                <span className="text-pacific/80">
                  {Math.round(proposal.confidence * 100)}% confidence
                </span>
              )}
            </p>
            {proposal?.evidence && <p className="whitespace-pre-wrap">{proposal.evidence}</p>}
            <p className="text-xs text-pacific/70">Model {current?.model_identifier}</p>
            {latest.data.feedback?.outcome === 'confirmed' ? (
              <div className="flex flex-wrap items-center gap-2">
                <p>You agreed with this label.</p>
                <Button variant="ghost" onClick={() => onConfirm(label, target)}>
                  Open the action
                </Button>
              </div>
            ) : latest.data.feedback ? (
              <p>
                You corrected this label to
                {` "${INTAKE_LABEL_TEXT[latest.data.feedback.final_label]}"`}.
              </p>
            ) : correcting ? (
              <div className="space-y-2">
                <Field label="What is it?">
                  <Select
                    value={finalLabel}
                    onChange={(event) => setFinalLabel(event.target.value as IntakeLabel)}
                  >
                    {(Object.keys(INTAKE_LABEL_TEXT) as IntakeLabel[]).map((value) => (
                      <option key={value} value={value}>{INTAKE_LABEL_TEXT[value]}</option>
                    ))}
                  </Select>
                </Field>
                <Field label="Why (optional)">
                  <Textarea value={reason} onChange={(event) => setReason(event.target.value)} rows={2} />
                </Field>
                <div className="flex gap-2">
                  <Button
                    onClick={() => answer.mutate({
                      outcome: 'dismissed', final_label: finalLabel, reason: reason.trim(),
                    })}
                    disabled={answer.isPending}
                  >
                    Save correction
                  </Button>
                  <Button variant="ghost" onClick={() => setCorrecting(false)}>Cancel</Button>
                </div>
              </div>
            ) : (
              <div className="flex flex-wrap gap-2">
                <Button
                  onClick={() => answer.mutate(
                    { outcome: 'confirmed', final_label: label, reason: '' },
                    { onSuccess: () => onConfirm(label, target) },
                  )}
                  disabled={answer.isPending}
                >
                  Correct
                </Button>
                <Button variant="ghost" onClick={() => setCorrecting(true)}>
                  Not correct
                </Button>
              </div>
            )}
            {answer.isError && <ErrorNote error={answer.error} />}
          </>
        )}
        {run.isError && <ErrorNote error={run.error} />}
      </div>
    </Card>
  )
}
