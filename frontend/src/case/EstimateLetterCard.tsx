// ABOUTME: Case-header card for the system-prepared interpretation-and-estimate
// ABOUTME: letter (#42): preview it and approve and send it in one action.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router'
import { api } from '../api/client'
import { Button, Card, ErrorNote, useToast } from '../ui'

interface EstimateLetter {
  correspondence: {
    id: string
    version: number
    status: string
    system_prepared: boolean
    subject: string
    body: string
  } | null
  estimate: { id: string; as_of: string } | null
}

export function EstimateLetterCard({ number }: { number: string }) {
  const qc = useQueryClient()
  const toast = useToast()
  const send = useMutation({
    mutationFn: (draft: NonNullable<EstimateLetter['correspondence']>) =>
      api(`/api/staff/correspondence/${draft.id}/approve-and-send/`, {
        method: 'POST',
        body: { expected_version: draft.version },
      }),
    onSuccess: () => {
      toast('success', 'Interpretation and estimate letter approved and sent to the requester.')
      qc.invalidateQueries({ queryKey: ['estimate-letter', number] })
      qc.invalidateQueries({ queryKey: ['case-workspace-summary', number] })
    },
  })
  const letter = useQuery({
    queryKey: ['estimate-letter', number],
    queryFn: () => api<EstimateLetter>(`/api/staff/requests/${number}/estimate-letter/`),
  })
  const draft = letter.data?.correspondence
  if (!draft) return null
  return (
    <Card title="Interpretation and estimate letter ready" className="mb-5">
      <p className="mb-3 text-sm text-ink-muted">
        {draft.system_prepared ? 'Drafted by the system' : 'Drafted by staff'}
        {letter.data?.estimate ? ` from the estimate computed ${letter.data.estimate.as_of}` : ''}.
        Nothing has been sent. Approving sends it to the requester now.
      </p>
      <h3 className="mb-1 font-heading text-sm font-semibold text-ink">{draft.subject}</h3>
      <pre
        aria-label="Letter preview"
        className="mb-3 max-h-80 overflow-y-auto whitespace-pre-wrap rounded-lg border border-line bg-surface-sunken p-3 font-body text-sm text-ink"
      >
        {draft.body}
      </pre>
      {send.isError && <ErrorNote error={send.error} />}
      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" disabled={send.isPending} onClick={() => send.mutate(draft)}>
          Approve and send
        </Button>
        <Link
          to={`/requests/${number}/messages`}
          className="text-sm font-semibold text-accent underline-offset-2 hover:underline"
        >
          Edit in Messages
        </Link>
      </div>
    </Card>
  )
}
