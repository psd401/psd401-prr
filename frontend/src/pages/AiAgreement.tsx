// ABOUTME: Oversight screen for stored AI-human agreement (#38): how often the
// ABOUTME: responsiveness suggestion matched the reviewer, by label and band.
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useSearchParams } from 'react-router'
import { api } from '../api/client'
import { PageHeader } from '../layout/AppLayout'
import { Button, Card, EmptyState, ErrorNote, Field, Input, Spinner, Table } from '../ui'

interface AgreementRow {
  label: string
  band: string
  decisions: number
  agreed: number | null
  agreement: number | null
}

interface AgreementReport {
  request: string | null
  decisions: number
  scored: number
  agreed: number
  agreement: number | null
  uncertain: number
  without_suggestion: number
  rows: AgreementRow[]
}

const LABEL_TEXT: Record<string, string> = {
  responsive: 'Responsive',
  non_responsive: 'Non-responsive',
  uncertain: 'Uncertain',
}

function percent(value: number | null): string {
  return value === null ? '—' : `${Math.round(value * 1000) / 10}%`
}

export function AiAgreement() {
  const [params, setParams] = useSearchParams()
  const caseNumber = params.get('case') ?? ''
  const [draft, setDraft] = useState(caseNumber)
  const report = useQuery({
    queryKey: ['advisory-agreement', caseNumber],
    queryFn: () =>
      api<AgreementReport>(
        caseNumber
          ? `/api/staff/advisory-agreement/?request=${encodeURIComponent(caseNumber)}`
          : '/api/staff/advisory-agreement/',
      ),
  })
  const data = report.data
  return (
    <>
      <PageHeader
        title="AI agreement"
        subtitle="How often the AI's responsiveness suggestion matched the reviewer's decision. Only each record's latest decision counts."
      />
      <form
        className="mb-5 flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault()
          const value = draft.trim()
          setParams(value ? { case: value } : {})
        }}
      >
        <Field label="Case number" hint="Leave blank for every case.">
          <Input value={draft} onChange={(event) => setDraft(event.target.value)} />
        </Field>
        <Button type="submit">Show</Button>
      </form>
      {report.isPending && <Spinner />}
      {report.isError && <ErrorNote error={report.error} />}
      {data && (
        <Card title={data.request ? `Case ${data.request}` : 'District-wide'}>
          <p className="mb-3 text-sm">
            <span className="font-semibold">{percent(data.agreement)}</span> agreement on{' '}
            {data.scored} decisions ({data.agreed} matched).{' '}
            {data.uncertain} decided after an uncertain suggestion;{' '}
            {data.without_suggestion} decided with no suggestion shown.
          </p>
          {data.rows.length === 0 ? (
            <EmptyState>No review decisions with an AI suggestion yet.</EmptyState>
          ) : (
            <Table
              label="Agreement by suggested label and confidence"
              columns={[
                { header: 'AI suggested', cell: (r: AgreementRow) => LABEL_TEXT[r.label] ?? r.label },
                { header: 'Confidence', cell: (r) => r.band },
                { header: 'Decisions', cell: (r) => r.decisions },
                { header: 'Matched', cell: (r) => r.agreed ?? '—' },
                { header: 'Agreement', cell: (r) => percent(r.agreement) },
              ]}
              rows={data.rows}
              rowKey={(r) => `${r.label}:${r.band}`}
            />
          )}
        </Card>
      )}
    </>
  )
}
