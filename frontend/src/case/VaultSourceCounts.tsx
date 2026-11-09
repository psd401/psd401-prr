// ABOUTME: One Google Vault count shown per source (Gmail, Drive, Chat):
// ABOUTME: counting, counted with a number, or not available with a reason.
import { sourceCountText, type VaultSourceCount } from '../vaultSources'

export function VaultSourceCounts({ rows }: { rows: VaultSourceCount[] | undefined }) {
  if (!rows || rows.length === 0) return null
  return (
    <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-3" aria-label="Count by source">
      {rows.map((row) => (
        <div key={row.source}>
          <dt className="text-ink-muted">{row.label}</dt>
          <dd
            className={
              row.state === 'unavailable'
                ? 'text-warning-ink'
                : 'font-heading font-bold text-ink'
            }
          >
            {sourceCountText(row)}
          </dd>
        </div>
      ))}
    </dl>
  )
}
