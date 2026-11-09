import { describe, expect, it } from 'vitest'

import {
  countHeadline,
  isVaultKind,
  sourceCountText,
  sourceSystemLabel,
  type VaultSourceCount,
} from './vaultSources'

const gmail: VaultSourceCount = {
  source: 'mail', label: 'Gmail', state: 'counted', count: 1842, reason: '',
}
const drive: VaultSourceCount = {
  source: 'drive', label: 'Drive', state: 'counting', count: null, reason: '',
}
const chat: VaultSourceCount = {
  source: 'chat',
  label: 'Chat',
  state: 'unavailable',
  count: null,
  reason: 'Staff organizational unit not set.',
}

describe('sourceCountText', () => {
  it('names the items of each source', () => {
    expect(sourceCountText(gmail)).toBe('1,842 messages')
    expect(sourceCountText({ ...drive, state: 'counted', count: 1 })).toBe('1 file')
    expect(sourceCountText({ ...drive, state: 'counted', count: 3 })).toBe('3 files')
  })

  it('adds the note on a counted total that Google reported', () => {
    expect(
      sourceCountText({
        ...drive,
        state: 'counted',
        count: 6057,
        reason: "Google's reported total; its count did not finish.",
      }),
    ).toBe("6,057 files (Google's reported total; its count did not finish.)")
  })

  it('says counting and not available plainly', () => {
    expect(sourceCountText(drive)).toBe('Counting…')
    expect(sourceCountText(chat)).toBe('Not available: Staff organizational unit not set.')
    expect(sourceCountText({ ...chat, reason: '' })).toBe('Not available')
  })
})

describe('countHeadline', () => {
  it('keeps a Gmail-only count Gmail', () => {
    expect(countHeadline(undefined)).toBe('possible Gmail messages')
    expect(countHeadline(['mail'])).toBe('possible Gmail messages')
  })

  it('names every source otherwise', () => {
    expect(countHeadline(['mail', 'drive', 'chat'])).toBe(
      'possible items across Gmail, Drive, and Chat',
    )
  })
})

describe('isVaultKind', () => {
  it('knows every Vault source kind and nothing else', () => {
    expect(isVaultKind('vault_mail')).toBe(true)
    expect(isVaultKind('vault_drive')).toBe(true)
    expect(isVaultKind('vault_chat')).toBe(true)
    expect(isVaultKind('manual')).toBe(false)
  })
})

describe('sourceSystemLabel', () => {
  it('names each Vault source plainly and leaves others alone', () => {
    expect(sourceSystemLabel('google_vault_gmail')).toBe('Gmail (Vault)')
    expect(sourceSystemLabel('google_vault_drive')).toBe('Drive (Vault)')
    expect(sourceSystemLabel('google_vault_chat')).toBe('Chat (Vault)')
    expect(sourceSystemLabel('upload')).toBe('upload')
  })
})
