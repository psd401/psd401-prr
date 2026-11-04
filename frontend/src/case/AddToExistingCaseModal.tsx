// ABOUTME: Move an automatically opened email case's message onto the case it
// ABOUTME: actually belongs to, closing the leftover case in one action.
import { useMutation, useQuery } from '@tanstack/react-query'
import { useEffect, useState, type FormEvent } from 'react'
import { api } from '../api/client'
import {
  Button,
  ErrorNote,
  Field,
  Input,
  Modal,
  Spinner,
  Textarea,
  MIN_REASON_LENGTH,
  reasonHint,
  reasonShortfall,
  reasonTooShort,
} from '../ui'

interface ReceiptVersion {
  version: number
}

/**
 * The dialog for "Add to an existing case…".
 *
 * A requester who follows up from a fresh thread gets a brand-new case opened
 * for them, because a machine guessing which existing case an email belongs to
 * is the failure mode this system refuses. A person recognizes the real case;
 * this is where they say so.
 *
 * It is one action, not two, on purpose. Moving the message and closing the
 * leftover case are halves of one decision, and leaving the district with two
 * open cases for one request is a statutory exposure -- both would carry their
 * own five-day clock. The server does both atomically; this dialog never
 * offers half of it.
 *
 * The receipt's current version is fetched when the dialog opens rather than
 * carried on the row: it is the concurrency token the server checks, and a
 * stale one must fail loudly instead of quietly overwriting someone else's
 * correction.
 *
 * Mount this only while it is open. It holds the typed destination and reason
 * in its own state, so a fresh mount is the reset; keeping fifty closed copies
 * alive in a message list would also keep fifty other cases' numbers in the
 * DOM for no reason.
 */
export function AddToExistingCaseModal({
  initialDestination,
  open,
  sourceNumber,
  subject,
  receiptId,
  onClose,
  onRedirected,
}: {
  open: boolean
  sourceNumber: string
  subject?: string
  receiptId: string | null
  /** Prefill the destination, e.g. from a confirmed intake label. */
  initialDestination?: string
  onClose: () => void
  onRedirected: () => Promise<void>
}) {
  const [destination, setDestination] = useState(initialDestination ?? '')
  useEffect(() => {
    if (open) setDestination(initialDestination ?? '')
  }, [open, initialDestination])
  const [reason, setReason] = useState('')

  const receipt = useQuery({
    queryKey: ['inbound-receipt-version', receiptId],
    queryFn: () => api<ReceiptVersion>(
      `/api/staff/inbound-correspondence-receipts/${receiptId}/`,
    ),
    enabled: open && !!receiptId,
    staleTime: 0,
  })

  const redirect = useMutation({
    mutationFn: () => api(
      `/api/staff/inbound-correspondence-receipts/${receiptId}/redirect/`,
      {
        method: 'POST',
        body: {
          request_number: destination.trim(),
          reason: reason.trim(),
          expected_version: receipt.data?.version,
        },
      },
    ),
    onSuccess: async () => {
      await onRedirected()
    },
  })

  const shortfall = reasonShortfall(reason)
  const pending = redirect.isPending
  // isFetching, not just data: a cached version from a previous open would
  // otherwise let the officer submit against a number the server has already
  // moved past, which is the silent overwrite this token exists to prevent.
  const ready = !!receipt.data
    && !receipt.isFetching
    && !!destination.trim()
    && !reasonTooShort(reason)

  return (
    <Modal
      open={open}
      title="Add to an existing case"
      onClose={onClose}
      dismissible={!pending}
    >
      <form
        className="space-y-4"
        onSubmit={(event: FormEvent) => {
          event.preventDefault()
          if (!ready || pending) return
          redirect.mutate()
        }}
      >
        <p className="text-sm text-ink-muted">
          {`${sourceNumber} was opened automatically from an emailed message`}
          {subject ? ` (“${subject}”)` : ''}
          . Moving it keeps the message and every attachment as retained
          evidence on the case you choose, and closes {sourceNumber} as not a
          separate records request. Nothing is deleted and no requester message
          is sent.
        </p>
        <Field
          label="Case to add this message to"
          hint="The case number this message belongs to, for example PRR-2026-0042"
        >
          <Input
            required
            maxLength={20}
            value={destination}
            onChange={(event) => setDestination(event.target.value)}
          />
        </Field>
        <Field
          label="Why this message belongs to that case"
          hint={reasonHint('this is recorded in the audit trail')}
        >
          <Textarea
            minLength={MIN_REASON_LENGTH}
            maxLength={2000}
            rows={4}
            required
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </Field>
        {shortfall && (
          <p aria-live="polite" className="text-sm text-ink-muted">
            {shortfall}
          </p>
        )}
        {receipt.isPending && open && <Spinner label="Loading message…" />}
        {receipt.isError && <ErrorNote error={receipt.error} />}
        {redirect.isError && <ErrorNote error={redirect.error} />}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose} disabled={pending}>
            Cancel
          </Button>
          <Button type="submit" disabled={pending || !ready}>
            {pending ? 'Adding…' : 'Add to case'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
