// ABOUTME: The model's suggested intake label for a new case, for a Records
// ABOUTME: Officer to confirm, correct, or undo when the system applied it.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import { api } from '../api/client'
import { Badge, Button, Card, ErrorNote, Field, Select, Spinner, Textarea } from '../ui'
import { INTAKE_LABEL_TEXT, intakeAction, type IntakeLabel } from './intakeLabels'

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

interface AutoApplied {
  label: IntakeLabel
  confidence: number
  applied_at: string
  in_effect: boolean
}

interface IntakeLatest {
  enabled: boolean
  latest: IntakeRun | null
  feedback: { outcome: 'confirmed' | 'dismissed'; final_label: IntakeLabel } | null
  auto_applied: AutoApplied | null
}

/**
 * Advisory only. "Correct" records that the officer agrees and hands the
 * label to `onConfirm`, which opens the ordinary merge, close, or add-to-case
 * dialog prefilled -- nothing is merged or closed from here. "Not correct"
 * records what the officer judged it to be instead.
 *
 * Under the trust ladder (#34) the system may itself close a spam or
 * non-request email case. The card then says so and offers Undo, which is the
 * ordinary reopen (back to received), and Keep, which records agreement.
 * On a closed case the card shows only that.
 */
export function IntakeClassificationCard({
  number,
  emailAutoCreated,
  onConfirm,
  closed = false,
}: {
  number: string
  emailAutoCreated: boolean
  onConfirm: (label: IntakeLabel, target: string | null) => void
  closed?: boolean
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
  const undo = useMutation({
    mutationFn: (label: IntakeLabel) => api(`/api/staff/requests/${number}/reopen/`, {
      method: 'POST',
      body: {
        to_status: 'received',
        reason: `Undid the automatic closure as ${INTAKE_LABEL_TEXT[label].toLowerCase()}.`,
      },
    }),
    // The same refreshes as the case page's own Reopen.
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['advisory-intake', number] })
      qc.invalidateQueries({ queryKey: ['request', number] })
      qc.invalidateQueries({ queryKey: ['case-workspace-summary', number] })
      qc.invalidateQueries({ queryKey: ['case-activity', number] })
    },
  })

  if (latest.isPending) return <Spinner />
  if (latest.isError) return <ErrorNote error={latest.error} />

  // A closure the system already made is shown even with advisory AI off now.
  const auto = latest.data.auto_applied
  if (closed && !auto?.in_effect) return null
  if (auto?.in_effect) {
    return (
      <Card title="Closed automatically">
        <div className="space-y-3 text-sm text-pacific">
          <p className="flex flex-wrap items-center gap-2">
            <Badge tone="amber">Auto</Badge>
            <strong>{INTAKE_LABEL_TEXT[auto.label]}</strong>
            <span className="text-pacific/80">
              {Math.round(auto.confidence * 100)}% confidence
            </span>
          </p>
          <p>
            The system closed this case as not a records request because the
            label met the district&apos;s automatic closure floor. Nothing was sent
            to the sender. Undo reopens it as received.
          </p>
          {latest.data.feedback?.outcome === 'confirmed' ? (
            <p>You kept this closure.</p>
          ) : (
            <div className="flex flex-wrap gap-2">
              <Button onClick={() => undo.mutate(auto.label)} disabled={undo.isPending}>
                Undo
              </Button>
              {!latest.data.feedback && (
                <Button
                  variant="ghost"
                  onClick={() => answer.mutate({
                    outcome: 'confirmed', final_label: auto.label, reason: '',
                  })}
                  disabled={answer.isPending}
                >
                  Keep
                </Button>
              )}
            </div>
          )}
          {undo.isError && <ErrorNote error={undo.error} />}
          {answer.isError && <ErrorNote error={answer.error} />}
        </div>
      </Card>
    )
  }

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
                {intakeAction(label, target, emailAutoCreated) && (
                  <Button variant="ghost" onClick={() => onConfirm(label, target)}>
                    Open the action
                  </Button>
                )}
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
