/**
 * Google Vault sources in plain words: Gmail, Drive, and Chat.
 *
 * Mirrors `source_count_summary()` in `apps/records/vault_counts.py` and the
 * Vault collection task kinds in `apps/records/models.py`. Each source is
 * counting, counted with a number, or unavailable with a plain reason; a
 * source is never silently left out.
 */

export type VaultSource = 'mail' | 'drive' | 'chat'

export interface VaultSourceCount {
  source: VaultSource
  label: string
  state: 'counting' | 'counted' | 'unavailable'
  count: number | null
  reason: string
}

const ITEM_NOUNS: Record<VaultSource, [string, string]> = {
  mail: ['message', 'messages'],
  drive: ['file', 'files'],
  chat: ['message', 'messages'],
}

/** What one source's count says, in plain words. */
export function sourceCountText(row: VaultSourceCount): string {
  if (row.state === 'counted' && row.count !== null) {
    const [one, many] = ITEM_NOUNS[row.source] ?? ['item', 'items']
    return `${new Intl.NumberFormat().format(row.count)} ${row.count === 1 ? one : many}`
  }
  if (row.state === 'unavailable') {
    return row.reason ? `Not available: ${row.reason}` : 'Not available'
  }
  return 'Counting…'
}

/**
 * The headline noun for a count's total. A count from before Drive and Chat
 * searched Gmail only and keeps saying so.
 */
export function countHeadline(sources: VaultSource[] | undefined): string {
  if (!sources || (sources.length === 1 && sources[0] === 'mail')) {
    return 'possible Gmail messages'
  }
  return 'possible items across Gmail, Drive, and Chat'
}

/** Collection task kinds that are Vault exports, one per source. */
export const VAULT_TASK_KINDS = ['vault_mail', 'vault_drive', 'vault_chat'] as const

export function isVaultKind(kind: string): boolean {
  return (VAULT_TASK_KINDS as readonly string[]).includes(kind)
}

const SOURCE_SYSTEM_LABELS: Record<string, string> = {
  google_vault_gmail: 'Gmail (Vault)',
  google_vault_drive: 'Drive (Vault)',
  google_vault_chat: 'Chat (Vault)',
}

/** A record's source in plain words; other systems show as stored. */
export function sourceSystemLabel(sourceSystem: string): string {
  return SOURCE_SYSTEM_LABELS[sourceSystem] ?? sourceSystem
}
