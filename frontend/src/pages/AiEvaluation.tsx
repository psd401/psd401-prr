// ABOUTME: Oversight screen for model-comparison runs (#32): each candidate
// ABOUTME: model's accuracy, safety recall, cost, and latency side by side.
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { PageHeader } from '../layout/AppLayout'
import { Card, EmptyState, ErrorNote, Spinner, Table } from '../ui'

interface ModelResult {
  items: number
  errors: number
  accuracy: number | null
  safety_recall: number | null
  target_accuracy: number | null
  confusion: Record<string, Record<string, number>>
  input_tokens_mean: number | null
  output_tokens_mean: number | null
  cost_per_item_usd: number | null
  latency_p50_ms: number | null
  latency_p95_ms: number | null
}

interface EvaluationRun {
  id: string
  kind: 'intake' | 'responsiveness'
  items: number
  label_counts: Record<string, number>
  model_identifiers: string[]
  results: Record<string, ModelResult>
  created_by: string | null
  created_at: string
}

const KIND_TEXT: Record<EvaluationRun['kind'], string> = {
  intake: 'Intake classification',
  responsiveness: 'Record responsiveness',
}

const SAFETY_TEXT: Record<EvaluationRun['kind'], string> = {
  intake: 'Real requests kept',
  responsiveness: 'Responsive records kept',
}

function percent(value: number | null): string {
  return value === null ? '—' : `${Math.round(value * 1000) / 10}%`
}

function confusionText(confusion: ModelResult['confusion']): string {
  return Object.entries(confusion)
    .flatMap(([gold, predicted]) =>
      Object.entries(predicted)
        .filter(([label]) => label !== gold)
        .map(([label, count]) => `${gold} → ${label}: ${count}`),
    )
    .join('; ') || 'none'
}

function RunCard({ run }: { run: EvaluationRun }) {
  const rows = run.model_identifiers.map((model) => ({ model, ...run.results[model]! }))
  return (
    <Card
      title={`${KIND_TEXT[run.kind]} · ${run.items} examples · ${new Date(run.created_at).toLocaleString()}`}
      className="mb-5"
    >
      <p className="mb-3 text-sm text-pacific/80">
        Labels:{' '}
        {Object.entries(run.label_counts).map(([label, count]) => `${label} ${count}`).join(', ')}
        {run.created_by ? ` · run by ${run.created_by}` : ''}
      </p>
      <Table
        label={`${KIND_TEXT[run.kind]} model comparison`}
        columns={[
          { header: 'Model', cell: (r: (typeof rows)[number]) => <span className="font-semibold">{r.model}</span> },
          { header: 'Accuracy', cell: (r) => percent(r.accuracy) },
          { header: SAFETY_TEXT[run.kind], cell: (r) => percent(r.safety_recall) },
          ...(run.kind === 'intake'
            ? [{ header: 'Right case', cell: (r: (typeof rows)[number]) => percent(r.target_accuracy) }]
            : []),
          { header: 'Errors', cell: (r) => r.errors },
          {
            header: 'Cost / item',
            cell: (r) => (r.cost_per_item_usd === null ? 'no price' : `$${r.cost_per_item_usd.toFixed(4)}`),
          },
          { header: 'Tokens in / out', cell: (r) => `${r.input_tokens_mean ?? '—'} / ${r.output_tokens_mean ?? '—'}` },
          { header: 'p50 / p95', cell: (r) => `${r.latency_p50_ms ?? '—'} / ${r.latency_p95_ms ?? '—'} ms` },
          { header: 'Disagreements', cell: (r) => confusionText(r.confusion) },
        ]}
        rows={rows}
        rowKey={(r) => r.model}
      />
    </Card>
  )
}

export function AiEvaluation() {
  const runs = useQuery({
    queryKey: ['advisory-evaluations'],
    queryFn: () => api<EvaluationRun[]>('/api/staff/advisory-evaluations/'),
  })
  return (
    <>
      <PageHeader
        title="AI evaluation"
        subtitle="Candidate models scored on decisions staff already made. Read-only; runs start from manage.py evaluate_models."
      />
      {runs.isPending && <Spinner />}
      {runs.isError && <ErrorNote error={runs.error} />}
      {runs.data && runs.data.length === 0 && (
        <Card><EmptyState>No evaluation runs yet.</EmptyState></Card>
      )}
      {runs.data?.map((run) => <RunCard key={run.id} run={run} />)}
    </>
  )
}
