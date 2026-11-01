// ABOUTME: Compares a scope letter's typed count with the request's latest Vault count.
// ABOUTME: Informs the officer only; the district chose not to block sending on a mismatch.

export interface LatestVaultCount {
  state: 'pending' | 'running' | 'complete' | 'failed'
  total_count: number | null
  completed_at: string | null
  trigger: 'manual' | 'automatic'
}

const formatCount = (value: number) => value.toLocaleString('en-US')

/**
 * The officer's number is the one the letter states, and the officer may have
 * good reason for it (a narrower re-run, a hand count). So this never refuses;
 * it only says out loud when the letter and the latest count disagree.
 */
export function scopeCountNotice(
  matchCount: string,
  latest: LatestVaultCount | null | undefined,
): string | null {
  if (!latest) return null
  if (latest.state === 'pending' || latest.state === 'running') {
    return 'A count is still running for this request, so the number in this letter '
      + 'may not match its result.'
  }
  const typed = matchCount.trim()
  if (latest.state !== 'complete' || latest.total_count === null || !/^\d+$/.test(typed)) {
    return null
  }
  const stated = Number(typed)
  if (stated === latest.total_count) return null
  const source = latest.trigger === 'automatic' ? 'automatic' : 'officer'
  return `This letter says ${formatCount(stated)} items. The latest ${source} count `
    + `found ${formatCount(latest.total_count)}. Sending is not blocked; check the `
    + 'number before you approve.'
}
