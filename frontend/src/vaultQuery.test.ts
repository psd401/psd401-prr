import { describe, expect, it } from 'vitest'

import cases from './vaultQueryCases.json'
import { proposedVaultQuery } from './vaultQuery'

// The backend suite reads the same file against vault_query_from_intent, so
// the officer's Count button and the automatic intake count cannot drift.
describe('proposedVaultQuery', () => {
  it.each(cases)('$name', ({ required, alternative, expected }) => {
    expect(proposedVaultQuery(required, alternative)).toBe(expected)
  })
})
