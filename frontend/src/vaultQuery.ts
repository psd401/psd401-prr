// ABOUTME: Builds the proposed Google Vault query from an intent proposal's terms.
// ABOUTME: Mirrors apps/records/vault_counts.vault_query_from_intent; both read vaultQueryCases.json.

function quotedVaultTerms(terms: string[] | undefined) {
  return Array.from(new Set((terms ?? [])
    .map((term) => term.trim().replace(/^"+|"+$/g, ''))
    .filter(Boolean)
    .map((term) => `"${term.replaceAll('"', '')}"`)))
}

// Required terms are AND anchors; alternatives become one parenthesized OR
// group. The automatic intake count runs the same query server-side, so a
// change here must be matched there (the shared case file enforces it).
export function proposedVaultQuery(
  requiredTerms: string[] | undefined,
  alternativeTerms: string[] | undefined,
) {
  const required = quotedVaultTerms(requiredTerms)
  const requiredKeys = new Set(required.map((term) => term.toLowerCase()))
  const alternatives = quotedVaultTerms(alternativeTerms)
    .filter((term) => !requiredKeys.has(term.toLowerCase()))
  const parts = [...required]
  const [onlyAlternative] = alternatives
  if (alternatives.length === 1 && onlyAlternative !== undefined) parts.push(onlyAlternative)
  if (alternatives.length > 1) parts.push(`(${alternatives.join(' OR ')})`)
  return parts.join(' AND ')
}
