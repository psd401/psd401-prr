// ABOUTME: Records Officer upload of files onto a case as candidate records,
// ABOUTME: with a per-file outcome from the server's scan-and-retain path.
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api, ApiError } from '../api/client'
import { Badge, Button, Field, Input, Modal } from '../ui'

type UploadOutcome =
  | 'added'
  | 'duplicate'
  | 'empty'
  | 'infected'
  | 'scanner_unavailable'
  | 'case_closed'
  | 'not_authorized'

interface UploadResult {
  filename: string
  outcome: UploadOutcome
  detail: string
  record_item_id: string | null
  sha256: string
  size: number
}

interface UploadResponse {
  added_count: number
  results: UploadResult[]
}

const OUTCOME_LABELS: Record<UploadOutcome, string> = {
  added: 'Added',
  duplicate: 'Already on this case',
  empty: 'Rejected: empty file',
  infected: 'Rejected: malware detected',
  scanner_unavailable: 'Rejected: scanning unavailable',
  case_closed: 'Not added: case was closed',
  not_authorized: 'Not added: no longer authorized',
}

const OUTCOME_TONES: Record<UploadOutcome, 'green' | 'neutral' | 'red' | 'amber'> = {
  added: 'green',
  duplicate: 'neutral',
  empty: 'amber',
  infected: 'red',
  scanner_unavailable: 'amber',
  case_closed: 'amber',
  not_authorized: 'amber',
}

/** The refusal an officer should read, without the envelope's field names. */
function uploadErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.data && typeof error.data === 'object') {
    const data = error.data as Record<string, unknown>
    if (Array.isArray(data.files)) return data.files.join(' ')
    if (typeof data.detail === 'string') return data.detail
  }
  return error instanceof Error ? error.message : 'The upload failed.'
}

export function RecordUploadPanel({
  requestNumber,
  closed,
}: {
  requestNumber: string
  closed: boolean
}) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [files, setFiles] = useState<File[]>([])
  const [results, setResults] = useState<UploadResult[] | null>(null)

  const upload = useMutation({
    mutationFn: (selected: File[]) => {
      const body = new FormData()
      for (const file of selected) body.append('files', file)
      return api<UploadResponse>(`/api/staff/requests/${requestNumber}/record-uploads/`, { method: 'POST', body })
    },
    onSuccess: (response) => {
      setResults(response.results)
      setFiles([])
    },
    // Refreshed whichever way it went: a failure part-way through a batch can
    // still have added the files before it.
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['record-items'] })
      qc.invalidateQueries({ queryKey: ['record-item-facets'] })
      qc.invalidateQueries({ queryKey: ['case-workspace-summary', requestNumber] })
      qc.invalidateQueries({ queryKey: ['case-closure-readiness', requestNumber] })
    },
  })

  function close() {
    if (upload.isPending) return
    setOpen(false)
    setFiles([])
    setResults(null)
    upload.reset()
  }

  return (
    <div className="flex flex-wrap items-center justify-end gap-3">
      {closed && (
        <p className="text-sm text-pacific/80">
          This case is closed. Reopen it to add records.
        </p>
      )}
      <Button
        type="button"
        variant="secondary"
        disabled={closed}
        onClick={() => setOpen(true)}
      >
        Upload records
      </Button>
      <Modal
        open={open}
        title="Upload records to this case"
        onClose={close}
        dismissible={!upload.isPending}
      >
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            if (files.length) upload.mutate(files)
          }}
        >
          <p className="text-sm text-pacific/80">
            Each file is scanned for malware and stored unchanged before it is
            added as an unreviewed record. Infected files, and any file that
            cannot be scanned, are refused.
          </p>
          <Field label="Files">
            <Input
              aria-label="Files"
              type="file"
              multiple
              disabled={upload.isPending}
              onChange={(event) => {
                setFiles(Array.from(event.target.files ?? []))
                setResults(null)
                upload.reset()
              }}
            />
          </Field>
          {upload.isError && (
            <p role="alert" className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900">
              {uploadErrorMessage(upload.error)}
            </p>
          )}
          {results && (
            <ul aria-label="Upload results" className="space-y-2">
              {results.map((result, index) => (
                <li
                  key={`${result.filename}-${index}`}
                  className="rounded-md border border-driftwood/60 bg-white p-3 text-sm"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="break-all font-heading font-bold text-pacific">
                      {result.filename}
                    </span>
                    <Badge tone={OUTCOME_TONES[result.outcome] ?? 'neutral'}>
                      {OUTCOME_LABELS[result.outcome] ?? result.outcome}
                    </Badge>
                  </div>
                  {result.outcome !== 'added' && result.detail && (
                    <p className="mt-1 text-xs text-pacific/80">{result.detail}</p>
                  )}
                </li>
              ))}
            </ul>
          )}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={close}
              disabled={upload.isPending}
            >
              {results ? 'Done' : 'Cancel'}
            </Button>
            <Button type="submit" disabled={!files.length || upload.isPending}>
              {upload.isPending ? 'Uploading and scanning…' : 'Upload'}
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  )
}
