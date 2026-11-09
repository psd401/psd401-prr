// ABOUTME: Records-Officer review queue for inbound RFC822 correspondence.
// ABOUTME: Rows name sender and subject; officers dismiss non-requests, one or many.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router'
import {
  type ClarificationCycle,
  humanFileSize,
  type InboundReceiptDetail,
  type InboundReceiptSummary,
  scanStatusLabel,
} from '../correspondence'
import { api, unwrap, type Page } from '../api/client'
import { InboxRow, InboxRowList } from '../inbox/InboxRow'
import { invalidateBoundedInfiniteQueries } from '../messages/boundedInfiniteCache'
import { NoActionModal } from '../NoActionModal'
import {
  Badge,
  Button,
  EmptyState,
  ErrorNote,
  Field,
  Input,
  Modal,
  Select,
  Spinner,
  Table,
  Textarea,
  useToast,
  MIN_REASON_LENGTH,
  reasonHint,
  reasonTooShort,
} from '../ui'
import { formatDay } from '../date'

export type ReviewQueue = 'unmatched' | 'quarantined'

function idempotencyKey(action: string) {
  const random = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `inbound-review:${action}:${random}`
}

/** What an officer reads first: the subject, or what happened if none. */
function rowName(receipt: InboundReceiptSummary): string {
  return receipt.received_subject?.trim() || receiptTitle(receipt)
}

function matchingTone(
  receipt: InboundReceiptSummary,
): 'neutral' | 'green' | 'blue' | 'amber' | 'red' {
  if (receipt.quarantined || receipt.matching_status === 'quarantined') {
    return 'red'
  }
  if (receipt.matching_status === 'matched') return 'green'
  if (receipt.matching_status === 'candidate') return 'blue'
  return 'amber'
}

/**
 * What an officer needs to know about an email that did not land in a case,
 * said the way they would say it. The underlying values -- `unmatched`,
 * `candidate`, `quarantined`, and raw reason codes -- are transport
 * vocabulary, and printing them on the screen made staff ask what their own
 * inbox meant.
 */
/**
 * A whole-message hold: the parser could not read the email, so no case was
 * opened or matched. Distinct from an attachment the virus scanner is holding.
 */
function heldUnread(receipt: InboundReceiptSummary): boolean {
  return receipt.parsing_status === 'quarantined' || receipt.parsing_status === 'failed'
}

function receiptTitle(receipt: InboundReceiptSummary): string {
  if (heldUnread(receipt)) {
    return 'Email held: PRR could not read it, so no case was opened'
  }
  if (receipt.quarantined || receipt.matching_status === 'quarantined') {
    return 'Email held: an attachment failed the virus scan'
  }
  if (receipt.parsing_error_code) {
    return 'Email we could not read'
  }
  if (receipt.candidate_request_number || receipt.matched_request_number) {
    return 'Email that may belong to an existing case'
  }
  return 'Email we could not match to a case'
}

function receiptDetail(receipt: InboundReceiptSummary): string {
  const suggested =
    receipt.candidate_request_number || receipt.matched_request_number
  if (heldUnread(receipt)) {
    return 'If it is a records request it is already on the clock. Open it, and ask an officer to re-parse it.'
  }
  if (receipt.quarantined || receipt.matching_status === 'quarantined') {
    return 'Nothing can be downloaded until the scan clears. Open it to see what arrived.'
  }
  if (receipt.parsing_error_code) {
    return 'The message could not be parsed, so it was kept as-is for a person to read.'
  }
  if (suggested) {
    return `The system thinks this belongs to ${suggested}. Open it to confirm or correct that.`
  }
  return 'Open it to say which case it belongs to, or that it is not a records request.'
}

function queueReasons(receipt: InboundReceiptSummary) {
  const reasons = [
    receipt.quarantine_reason_code,
    receipt.parsing_error_code,
  ].filter((value, index, values) => value && values.indexOf(value) === index)
  return reasons.length > 0 ? reasons : ['—']
}

interface AssociationInput {
  receipt: InboundReceiptDetail
  requestNumber: string
  reason: string
}

interface ClarificationAssociationInput {
  receipt: InboundReceiptDetail
  clarificationCycleId: string
  reason: string
}

export function InboundCorrespondenceReview({
  enabled,
  queue: controlledQueue,
  onQueueChange,
  showQueueSelector = true,
}: {
  enabled: boolean
  queue?: ReviewQueue
  onQueueChange?: (queue: ReviewQueue) => void
  showQueueSelector?: boolean
}) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [searchParams, setSearchParams] = useSearchParams()
  const [localQueue, setLocalQueue] = useState<ReviewQueue>('unmatched')
  const queue = controlledQueue ?? localQueue
  const [lookupId, setLookupId] = useState('')
  const [requestNumber, setRequestNumber] = useState('')
  const [reason, setReason] = useState('')
  const [clarificationCycleId, setClarificationCycleId] = useState('')
  const [clarificationReason, setClarificationReason] = useState('')
  // Rows chosen for one shared no-action decision, by receipt id. Only rows
  // with an open work item can be chosen.
  const [chosen, setChosen] = useState<Set<string>>(new Set())
  const [dismissTargets, setDismissTargets] = useState<InboundReceiptSummary[]>([])
  const [noActionCategory, setNoActionCategory] = useState('not_a_records_request')
  const [noActionReason, setNoActionReason] = useState('')
  const selectedId = searchParams.get('inbound_receipt')?.trim() || ''
  const queuePath = (
    '/api/staff/inbound-correspondence-receipts/'
    + `?queue=${encodeURIComponent(queue)}`
  )

  const list = useQuery({
    queryKey: ['inbound-correspondence-receipts', queue],
    queryFn: () =>
      api<Page<InboundReceiptSummary> | InboundReceiptSummary[]>(
        queuePath,
      ),
    enabled,
  })

  const receipt = useQuery({
    queryKey: ['inbound-correspondence-receipt', selectedId],
    queryFn: () =>
      api<InboundReceiptDetail>(
        `/api/staff/inbound-correspondence-receipts/${selectedId}/`,
      ),
    enabled: enabled && selectedId.length > 0,
  })
  const matchedRequestNumber = receipt.data?.matched_request_number || ''
  const clarificationCycles = useQuery({
    queryKey: [
      'clarification-cycles',
      matchedRequestNumber,
      'open-association',
    ],
    queryFn: () =>
      api<Page<ClarificationCycle> | ClarificationCycle[]>(
        (
          '/api/staff/clarification-cycles/'
          + `?request=${encodeURIComponent(matchedRequestNumber)}&state=open`
        ),
      ),
    enabled: (
      enabled
      && receipt.data?.matching_status === 'matched'
      && receipt.data?.correspondence_kind === 'general'
      && matchedRequestNumber.length > 0
    ),
  })

  useEffect(() => {
    if (!receipt.data) return
    setRequestNumber(
      receipt.data.matched_request_number
      || receipt.data.candidate_request_number
      || '',
    )
    setReason('')
    setClarificationCycleId('')
    setClarificationReason('')
  }, [receipt.data])

  const association = useMutation({
    mutationFn: ({
      receipt,
      requestNumber,
      reason,
    }: AssociationInput) => {
      const body = {
        request_number: requestNumber,
        reason,
        expected_version: receipt.version,
      }
      if (receipt.matching_status === 'matched') {
        return api<InboundReceiptDetail>(
          `/api/staff/inbound-correspondence-receipts/${receipt.id}/rematch/`,
          { method: 'POST', body },
        )
      }
      return api<InboundReceiptDetail>(
        `/api/staff/inbound-correspondence-receipts/${receipt.id}/match/`,
        { method: 'POST', body },
      )
    },
    onSuccess: (updated) => {
      queryClient.setQueryData(
        ['inbound-correspondence-receipt', updated.id],
        updated,
      )
      queryClient.invalidateQueries({
        queryKey: ['inbound-correspondence-receipts'],
      })
      queryClient.invalidateQueries({
        queryKey: ['correspondence-operations'],
      })
      void invalidateBoundedInfiniteQueries(
        queryClient,
        ['correspondence', 'transport'],
      )
      queryClient.invalidateQueries({
        queryKey: ['correspondence-detail'],
      })
      queryClient.invalidateQueries({
        queryKey: ['clarification-cycles', 'open'],
      })
      toast(
        'success',
        updated.match_method === 'manual'
          ? 'Inbound correspondence association saved.'
          : 'Inbound correspondence reviewed.',
      )
      closeReceipt()
    },
  })

  /**
   * One decision applied to each chosen row through the registered per-item
   * work-item primitive, so every message keeps its own audit entry. Rows are
   * sent one at a time; any that fail stay in the queue and are reported.
   */
  const dismiss = useMutation({
    mutationFn: async (targets: InboundReceiptSummary[]) => {
      const failed: InboundReceiptSummary[] = []
      for (const target of targets) {
        try {
          await api(`/api/staff/work-items/${target.work_item_id}/no-action-required/`, {
            method: 'POST',
            body: {
              category: noActionCategory,
              reason: noActionReason.trim(),
              expected_version: target.work_item_version,
              idempotency_key: idempotencyKey('no-action'),
            },
          })
        } catch {
          failed.push(target)
        }
      }
      return { done: targets.length - failed.length, failed }
    },
    onSettled: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['inbound-correspondence-receipts'] }),
        queryClient.invalidateQueries({ queryKey: ['work-item-counts'] }),
      ])
    },
    onSuccess: ({ done, failed }) => {
      setChosen(new Set(failed.map((target) => target.id)))
      if (failed.length === 0) {
        setDismissTargets([])
        setNoActionReason('')
        toast('success', done === 1
          ? 'Marked no action.'
          : `${done} messages marked no action.`)
      } else {
        setDismissTargets(failed)
        toast('error', `${failed.length} of ${done + failed.length} could not be marked; they are still selected.`)
      }
    },
  })

  function openDismiss(targets: InboundReceiptSummary[]) {
    dismiss.reset()
    setNoActionCategory('not_a_records_request')
    setDismissTargets(targets)
  }

  const clarificationAssociation = useMutation({
    mutationFn: ({
      receipt,
      clarificationCycleId,
      reason,
    }: ClarificationAssociationInput) =>
      api<InboundReceiptDetail>(
        `/api/staff/inbound-correspondence-receipts/${receipt.id}/associate-clarification/`,
        {
          method: 'POST',
          body: {
            clarification_cycle_id: clarificationCycleId,
            reason,
            expected_version: receipt.version,
          },
        },
      ),
    onSuccess: (updated) => {
      queryClient.setQueryData(
        ['inbound-correspondence-receipt', updated.id],
        updated,
      )
      queryClient.invalidateQueries({
        queryKey: ['inbound-correspondence-receipts'],
      })
      queryClient.invalidateQueries({
        queryKey: ['correspondence-operations'],
      })
      void invalidateBoundedInfiniteQueries(
        queryClient,
        ['correspondence', 'transport'],
      )
      queryClient.invalidateQueries({
        queryKey: ['correspondence-detail'],
      })
      queryClient.invalidateQueries({
        queryKey: ['clarification-cycles'],
      })
      toast(
        'success',
        'Clarification response association saved for officer resolution.',
      )
      setClarificationCycleId('')
      setClarificationReason('')
    },
  })

  const download = useMutation({
    mutationFn: (attachmentId: string) =>
      api<{ url: string; expires_in: number }>(
        `/api/staff/correspondence-attachments/${attachmentId}/download/`,
        { method: 'POST' },
      ),
    onSuccess: ({ url }) => window.location.assign(url),
  })

  function openReceipt(id: string) {
    association.reset()
    clarificationAssociation.reset()
    download.reset()
    const next = new URLSearchParams(searchParams)
    next.set('inbound_receipt', id)
    setSearchParams(next)
  }

  function closeReceipt() {
    const next = new URLSearchParams(searchParams)
    next.delete('inbound_receipt')
    setSearchParams(next)
    setRequestNumber('')
    setReason('')
    setClarificationCycleId('')
    setClarificationReason('')
    association.reset()
    clarificationAssociation.reset()
    download.reset()
  }

  function selectQueue(nextQueue: ReviewQueue) {
    if (onQueueChange) onQueueChange(nextQueue)
    else setLocalQueue(nextQueue)
  }

  function submitLookup(event: FormEvent) {
    event.preventDefault()
    const id = lookupId.trim()
    if (id) openReceipt(id)
  }

  function submitAssociation(event: FormEvent) {
    event.preventDefault()
    if (
      !receipt.data
      || requestNumber.trim().length === 0
      || reasonTooShort(reason)
    ) {
      return
    }
    association.mutate({
      receipt: receipt.data,
      requestNumber: requestNumber.trim(),
      reason: reason.trim(),
    })
  }

  function submitClarificationAssociation(event: FormEvent) {
    event.preventDefault()
    if (
      !receipt.data
      || clarificationCycleId.length === 0
      || reasonTooShort(clarificationReason)
    ) {
      return
    }
    clarificationAssociation.mutate({
      receipt: receipt.data,
      clarificationCycleId,
      reason: clarificationReason.trim(),
    })
  }

  if (!enabled) {
    return (
      <EmptyState>
        Records Officer role is required to review retained inbound messages.
      </EmptyState>
    )
  }

  const rows = list.data ? unwrap(list.data) : []
  // A batch is the rows the officer can see; a selection never outlives them.
  const chosenRows = rows.filter(
    (row) => row.work_item_id && chosen.has(row.id),
  )
  const page = list.data && !Array.isArray(list.data) ? list.data : null
  const selected = receipt.data
  const isDeliveryStatus = (
    selected?.correspondence_kind === 'delivery_status_notification'
  )
  const openClarificationCycles = clarificationCycles.data
    ? unwrap(clarificationCycles.data).filter(
        (cycle) => !cycle.inbound_response_correspondence_id,
      )
    : []
  const canAssociate = Boolean(
    selected
    && selected.parsing_status === 'parsed'
    && !selected.quarantined
    && !isDeliveryStatus
  )
  const isRematch = selected?.matching_status === 'matched'

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        {showQueueSelector && (
          <div
            className="flex flex-wrap gap-2"
            role="group"
            aria-label="Inbound correspondence queue"
          >
            <Button
              variant={queue === 'unmatched' ? 'primary' : 'ghost'}
              aria-pressed={queue === 'unmatched'}
              onClick={() => selectQueue('unmatched')}
            >
              Unmatched
            </Button>
            <Button
              variant={queue === 'quarantined' ? 'primary' : 'ghost'}
              aria-pressed={queue === 'quarantined'}
              onClick={() => selectQueue('quarantined')}
            >
              Quarantined
            </Button>
          </div>
        )}
        <form
          onSubmit={submitLookup}
          className="flex flex-wrap items-end gap-2"
        >
          <Field
            label="Receipt ID"
            hint="Open a retained receipt to correct an existing match."
          >
            <Input
              aria-label="Receipt ID"
              value={lookupId}
              onChange={(event) => setLookupId(event.target.value)}
              placeholder="Receipt UUID"
            />
          </Field>
          <Button
            type="submit"
            variant="ghost"
            disabled={lookupId.trim().length === 0}
          >
            Open receipt
          </Button>
        </form>
      </div>

      {list.isPending && <Spinner label="Loading inbound review queue…" />}
      {list.isError && <ErrorNote error={list.error} />}
      {list.isSuccess && rows.length === 0 && (
        <EmptyState>No messages are waiting in this queue.</EmptyState>
      )}
      {chosenRows.length > 0 && (
        <div
          className="mb-3 flex flex-wrap items-center gap-2"
          role="group"
          aria-label="Selected messages"
        >
          <Button onClick={() => openDismiss(chosenRows)}>
            Dismiss {chosenRows.length} selected
          </Button>
          <Button variant="ghost" onClick={() => setChosen(new Set())}>
            Clear selection
          </Button>
        </div>
      )}
      {rows.length > 0 && (
        <InboxRowList>
          {rows.map((item) => {
            const received = item.received_at.slice(0, 10)
            const suggested =
              item.candidate_request_number || item.matched_request_number
            const name = rowName(item)
            const sender = item.received_sender?.trim()
            return (
              <InboxRow
                key={item.id}
                title={name}
                detail={sender
                  ? `From ${sender}. ${receiptDetail(item)}`
                  : receiptDetail(item)}
                selection={item.work_item_id
                  ? {
                      checked: chosen.has(item.id),
                      ariaLabel: `Select: ${name}`,
                      onChange: (checked) => setChosen((current) => {
                        const next = new Set(current)
                        if (checked) next.add(item.id)
                        else next.delete(item.id)
                        return next
                      }),
                    }
                  : null}
                caseNumber={suggested || null}
                timestamp={received}
                tags={[
                  suggested ? 'Suggested match' : 'No case suggested',
                  item.attachment_count > 0
                    ? `${item.attachment_count} attachment${item.attachment_count === 1 ? '' : 's'}`
                    : 'No attachments',
                  item.quarantined_attachment_count > 0
                    ? `${item.quarantined_attachment_count} held by the virus scanner`
                    : null,
                  // The sanitized diagnostic code stays visible while scanning:
                  // the plain-language title says what happened, this says
                  // precisely which rule fired, which is what staff quote when
                  // they escalate.
                  ...queueReasons(item).filter((code) => code !== '—'),
                ]}
                alert={
                  heldUnread(item)
                    ? { label: 'Could not read', tone: 'red' }
                    : item.quarantined || item.matching_status === 'quarantined'
                      ? { label: 'Attachment held', tone: 'red' }
                      : null
                }
                secondaryActions={(
                  <>
                    <Button
                      variant="ghost"
                      aria-label={`Review email received ${received}`}
                      onClick={() => openReceipt(item.id)}
                    >
                      Review
                    </Button>
                    {item.work_item_id && (
                      <Button
                        variant="ghost"
                        aria-label={`Not a request: ${name}`}
                        onClick={() => openDismiss([item])}
                      >
                        Not a request…
                      </Button>
                    )}
                  </>
                )}
              />
            )
          })}
        </InboxRowList>
      )}
      {page && page.count > 0 && (
        <p
          className="mt-3 text-sm text-ink-muted"
          aria-live="polite"
        >
          Showing {rows.length} of {page.count} messages
        </p>
      )}

      <NoActionModal
        open={dismissTargets.length > 0}
        onClose={() => setDismissTargets([])}
        onSubmit={() => dismiss.mutate(dismissTargets)}
        category={noActionCategory}
        onCategoryChange={setNoActionCategory}
        reason={noActionReason}
        onReasonChange={setNoActionReason}
        pending={dismiss.isPending}
        error={dismiss.isError ? dismiss.error : null}
      />

      <Modal
        open={selectedId.length > 0}
        title="Review inbound correspondence"
        onClose={closeReceipt}
      >
        {receipt.isPending && <Spinner label="Loading retained message…" />}
        {receipt.isError && (
          <div className="space-y-3">
            <ErrorNote error={receipt.error} />
            <div className="flex justify-end">
              <Button type="button" variant="ghost" onClick={closeReceipt}>
                Close
              </Button>
            </div>
          </div>
        )}
        {selected && (
          <div className="max-h-[75vh] space-y-5 overflow-y-auto pr-1">
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={matchingTone(selected)}>
                  {selected.matching_status}
                </Badge>
                <span className="text-xs text-ink-muted">
                  Receipt {selected.id}
                </span>
              </div>
              <h3 className="font-heading text-base font-bold text-ink">
                {selected.received_subject || '(No subject)'}
              </h3>
              <p className="text-xs text-ink-muted">
                From {selected.received_sender || selected.envelope_sender || 'unknown sender'}
              </p>
              <div className="max-h-56 overflow-y-auto whitespace-pre-wrap rounded-md border border-line bg-surface-raised p-3 text-sm text-ink">
                {selected.received_body || '(No plain-text body)'}
              </div>
            </div>

            <section
              aria-labelledby={`matching-evidence-${selected.id}`}
              className="space-y-3"
            >
              <h3
                id={`matching-evidence-${selected.id}`}
                className="font-heading text-sm font-bold text-ink"
              >
                Matching evidence
              </h3>
              <dl className="grid gap-3 rounded-md border border-line bg-surface-raised p-3 text-sm sm:grid-cols-2">
                <div>
                  <dt className="font-heading text-xs font-bold text-ink-muted">
                    Match method
                  </dt>
                  <dd className="break-all text-ink">
                    {selected.match_method || 'No match evidence'}
                  </dd>
                </div>
                <div>
                  <dt className="font-heading text-xs font-bold text-ink-muted">
                    Current request
                  </dt>
                  <dd className="break-all text-ink">
                    {selected.matched_request_number || 'No current match'}
                  </dd>
                </div>
                <div>
                  <dt className="font-heading text-xs font-bold text-ink-muted">
                    Candidate request
                  </dt>
                  <dd className="break-all text-ink">
                    {selected.candidate_request_number || 'No candidate'}
                  </dd>
                </div>
                <div>
                  <dt className="font-heading text-xs font-bold text-ink-muted">
                    Provider
                  </dt>
                  <dd className="break-all text-ink">
                    {selected.provider || 'Not supplied'}
                  </dd>
                </div>
                <div>
                  <dt className="font-heading text-xs font-bold text-ink-muted">
                    Provider account
                  </dt>
                  <dd className="break-all text-ink">
                    {selected.provider_account || 'Not supplied'}
                  </dd>
                </div>
                <div>
                  <dt className="font-heading text-xs font-bold text-ink-muted">
                    Provider thread
                  </dt>
                  <dd className="break-all text-ink">
                    {selected.provider_thread_id || 'Not supplied'}
                  </dd>
                </div>
                <div>
                  <dt className="font-heading text-xs font-bold text-ink-muted">
                    Message-ID
                  </dt>
                  <dd className="break-all text-ink">
                    {selected.rfc_message_id || 'Not supplied'}
                  </dd>
                </div>
                <div>
                  <dt className="font-heading text-xs font-bold text-ink-muted">
                    In-Reply-To
                  </dt>
                  <dd className="break-all text-ink">
                    {selected.in_reply_to || 'Not supplied'}
                  </dd>
                </div>
                <div>
                  <dt className="font-heading text-xs font-bold text-ink-muted">
                    References
                  </dt>
                  <dd className="break-all whitespace-pre-wrap text-ink">
                    {selected.references.length > 0
                      ? selected.references.join('\n')
                      : 'None'}
                  </dd>
                </div>
                <div>
                  <dt className="font-heading text-xs font-bold text-ink-muted">
                    Envelope recipients
                  </dt>
                  <dd className="break-all whitespace-pre-wrap text-ink">
                    {selected.envelope_recipients.length > 0
                      ? selected.envelope_recipients.join('\n')
                      : 'None'}
                  </dd>
                </div>
                <div>
                  <dt className="font-heading text-xs font-bold text-ink-muted">
                    Envelope sender
                  </dt>
                  <dd className="break-all text-ink">
                    {selected.envelope_sender || 'Not supplied'}
                  </dd>
                </div>
              </dl>
              <div className="rounded-md border border-line bg-surface-raised p-3">
                <h4 className="font-heading text-xs font-bold text-ink-muted">
                  Safe received headers
                </h4>
                {Object.keys(selected.received_headers).length === 0 ? (
                  <p className="mt-1 text-sm text-ink">None retained.</p>
                ) : (
                  <dl className="mt-2 space-y-2 text-sm">
                    {Object.entries(selected.received_headers)
                      .sort(([left], [right]) => left.localeCompare(right))
                      .map(([name, value]) => (
                        <div key={name}>
                          <dt className="font-heading text-xs font-bold text-ink-muted">
                            {name}
                          </dt>
                          <dd className="break-all text-ink">{value}</dd>
                        </div>
                      ))}
                  </dl>
                )}
              </div>
            </section>

            {isDeliveryStatus && (
              <section className="space-y-3 rounded-md border border-warning bg-warning-soft p-3">
                <h3 className="font-heading text-sm font-bold text-ink">
                  Unverified delivery status report (advisory)
                </h3>
                <p className="text-sm text-ink-muted">
                  This unverified, advisory report does not retry delivery or
                  change request status. Its reported original Message-ID is
                  retained as opaque evidence; any replacement correspondence
                  requires a separate, audited Records Officer decision.
                </p>
                <dl className="grid gap-2 text-sm sm:grid-cols-2">
                  <div>
                    <dt className="font-heading text-xs font-bold text-ink-muted">
                      Reported original Message-ID
                    </dt>
                    <dd className="break-all text-ink">
                      {selected.delivery_status_original_message_id || 'Not available'}
                    </dd>
                  </div>
                  <div>
                    <dt className="font-heading text-xs font-bold text-ink-muted">
                      Matched delivery
                    </dt>
                    <dd className="break-all text-ink">
                      {selected.matched_delivery_id || 'No exact delivery match'}
                    </dd>
                  </div>
                </dl>
                {selected.delivery_notifications.length === 0 ? (
                  <p className="text-sm text-ink-muted">
                    No normalized recipient outcome could be associated with a
                    stable delivery.
                  </p>
                ) : (
                  <Table
                    columns={[
                      {
                        header: 'Recipient report',
                        cell: (item) => `#${item.recipient_index}`,
                      },
                      {
                        header: 'Action',
                        cell: (item) => (
                          <Badge
                            tone={
                              item.action === 'failed'
                                ? 'red'
                                : item.action === 'delayed'
                                  ? 'amber'
                                  : 'green'
                            }
                          >
                            {item.action}
                          </Badge>
                        ),
                      },
                      {
                        header: 'Enhanced status',
                        cell: (item) => item.enhanced_status,
                      },
                      {
                        header: 'Recipient match',
                        cell: (item) => item.recipient_match_status,
                      },
                    ]}
                    rows={selected.delivery_notifications}
                    rowKey={(item) => item.id}
                  />
                )}
              </section>
            )}

            <div>
              <h3 className="mb-2 font-heading text-sm font-bold text-ink">
                Attachments
              </h3>
              {selected.attachments.length === 0 ? (
                <p className="text-sm text-ink-muted">No attachments.</p>
              ) : (
                <Table
                  columns={[
                    {
                      header: 'File',
                      cell: (item) => item.original_filename,
                    },
                    {
                      header: 'Size',
                      cell: (item) => humanFileSize(item.size),
                    },
                    {
                      header: 'Scan',
                      cell: (item) => (
                        <Badge
                          tone={
                            item.is_downloadable
                              ? 'green'
                              : item.scan_status === 'pending'
                                ? 'amber'
                                : 'red'
                          }
                        >
                          {scanStatusLabel(item.scan_status)}
                        </Badge>
                      ),
                    },
                    {
                      header: 'Availability',
                      cell: (item) => item.is_downloadable ? (
                        <Button
                          variant="ghost"
                          aria-label={`Download attachment ${item.original_filename}`}
                          disabled={download.isPending}
                          onClick={() => download.mutate(item.id)}
                        >
                          Download
                        </Button>
                      ) : 'Not downloadable',
                    },
                  ]}
                  rows={selected.attachments}
                  rowKey={(item) => item.id}
                />
              )}
              {download.isError && <ErrorNote error={download.error} />}
            </div>

            {isRematch
              && selected.correspondence_kind === 'general'
              && (
                <section className="space-y-3 rounded-md border border-flow bg-flow-soft p-3">
                  <h3 className="font-heading text-sm font-bold text-ink">
                    Open clarification association
                  </h3>
                  <p className="text-sm text-ink-muted">
                    Use this only after confirming that this matched message
                    answers a specific open clarification. It will not resolve
                    the cycle or change request status.
                  </p>
                  {clarificationCycles.isPending && (
                    <Spinner label="Loading open clarification cycles…" />
                  )}
                  {clarificationCycles.isError && (
                    <ErrorNote error={clarificationCycles.error} />
                  )}
                  {clarificationCycles.isSuccess
                    && openClarificationCycles.length === 0
                    && (
                      <p className="text-sm text-ink-muted">
                        No eligible open clarification cycle exists for this
                        request.
                      </p>
                    )}
                  {openClarificationCycles.length > 0 && (
                    <form
                      onSubmit={submitClarificationAssociation}
                      className="space-y-3"
                    >
                      <Field label="Open clarification cycle">
                        <Select
                          aria-label="Open clarification cycle"
                          value={clarificationCycleId}
                          onChange={(event) =>
                            setClarificationCycleId(event.target.value)}
                          required
                        >
                          <option value="">Select a cycle</option>
                          {openClarificationCycles.map((cycle) => (
                            <option key={cycle.id} value={cycle.id}>
                              {`Opened ${formatDay(cycle.opened_at)} — awaiting response`}
                            </option>
                          ))}
                        </Select>
                      </Field>
                      <Field
                        label="Clarification association reason"
                        hint={reasonHint("this is written to the append-only audit trail")}
                      >
                        <Textarea
                          aria-label="Clarification association reason"
                          rows={3}
                          minLength={MIN_REASON_LENGTH}
                          value={clarificationReason}
                          onChange={(event) =>
                            setClarificationReason(event.target.value)}
                          required
                        />
                      </Field>
                      {clarificationAssociation.isError && (
                        <ErrorNote error={clarificationAssociation.error} />
                      )}
                      <div className="flex justify-end">
                        <Button
                          type="submit"
                          disabled={
                            clarificationAssociation.isPending
                            || clarificationCycleId.length === 0
                            || reasonTooShort(clarificationReason)
                          }
                        >
                          {clarificationAssociation.isPending
                            ? 'Saving…'
                            : 'Associate clarification response'}
                        </Button>
                      </div>
                    </form>
                  )}
                </section>
              )}

            {isDeliveryStatus ? (
              <div className="flex justify-end">
                <Button type="button" variant="ghost" onClick={closeReceipt}>
                  Close
                </Button>
              </div>
            ) : canAssociate ? (
              <form onSubmit={submitAssociation} className="space-y-4">
                <Field label="Request number">
                  <Input
                    value={requestNumber}
                    onChange={(event) => setRequestNumber(event.target.value)}
                    required
                  />
                </Field>
                <Field
                  label="Substantive reason"
                  hint={reasonHint("this is written to the append-only audit trail")}
                >
                  <Textarea
                    aria-label="Substantive reason"
                    rows={3}
                    minLength={MIN_REASON_LENGTH}
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                    required
                  />
                </Field>
                {association.isError && (
                  <ErrorNote error={association.error} />
                )}
                <div className="flex justify-end gap-2">
                  <Button type="button" variant="ghost" onClick={closeReceipt}>
                    Cancel
                  </Button>
                  <Button
                    type="submit"
                    disabled={
                      association.isPending
                      || requestNumber.trim().length === 0
                      || reasonTooShort(reason)
                    }
                  >
                    {association.isPending
                      ? 'Saving…'
                      : isRematch
                        ? 'Rematch message'
                        : 'Match message'}
                  </Button>
                </div>
              </form>
            ) : (
              <div className="space-y-3">
                <p className="rounded-md border border-danger bg-danger-soft px-3 py-2 text-sm text-danger-ink">
                  This message cannot be matched until parsing and quarantine
                  checks complete successfully. Reason code:{' '}
                  {queueReasons(selected).join(', ')}
                </p>
                <div className="flex justify-end">
                  <Button type="button" variant="ghost" onClick={closeReceipt}>
                    Close
                  </Button>
                </div>
              </div>
            )}
          </div>
        )}
      </Modal>
    </>
  )
}
