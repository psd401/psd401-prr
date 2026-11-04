// ABOUTME: Theme choice → data-theme: stored per browser, else the device's contrast
// ABOUTME: and dark settings, and never an unknown value from storage.
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  THEMES,
  THEME_STORAGE_KEY,
  applyTheme,
  readThemeChoice,
  resolveTheme,
  startTheme,
  writeThemeChoice,
} from './theme'

function mockMedia(matching: string[]) {
  const listeners: Array<() => void> = []
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: matching.includes(query),
    addEventListener: (_: string, listener: () => void) => listeners.push(listener),
    removeEventListener: vi.fn(),
  }))
  return listeners
}

afterEach(() => {
  vi.unstubAllGlobals()
  window.localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
})

describe('resolveTheme', () => {
  it('honours an explicit choice of all five themes', () => {
    for (const theme of THEMES) expect(resolveTheme(theme)).toBe(theme)
  })

  it('follows the device: contrast first, then dark, else Nexus', () => {
    mockMedia([])
    expect(resolveTheme('system')).toBe('nexus')
    mockMedia(['(prefers-color-scheme: dark)'])
    expect(resolveTheme('system')).toBe('dark')
    mockMedia(['(prefers-color-scheme: dark)', '(prefers-contrast: more)'])
    expect(resolveTheme('system')).toBe('high-contrast')
  })
})

describe('stored choice', () => {
  it('round-trips a theme and forgets "system"', () => {
    writeThemeChoice('commons-dark')
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('commons-dark')
    expect(readThemeChoice()).toBe('commons-dark')
    writeThemeChoice('system')
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBeNull()
    expect(readThemeChoice()).toBe('system')
  })

  it('ignores a value that is not a theme', () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'neon')
    expect(readThemeChoice()).toBe('system')
  })

  it('falls back to the device when storage is blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(readThemeChoice()).toBe('system')
    expect(() => writeThemeChoice('dark')).not.toThrow()
    vi.restoreAllMocks()
  })
})

describe('applying', () => {
  it('sets data-theme on <html>', () => {
    applyTheme('high-contrast')
    expect(document.documentElement.dataset.theme).toBe('high-contrast')
  })

  it('starts from storage and re-resolves when the device changes', () => {
    const listeners = mockMedia([])
    startTheme()
    expect(document.documentElement.dataset.theme).toBe('nexus')
    mockMedia(['(prefers-color-scheme: dark)'])
    listeners.forEach((listener) => listener())
    expect(document.documentElement.dataset.theme).toBe('dark')
  })

  it('starts without matchMedia', () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'commons')
    vi.stubGlobal('matchMedia', undefined)
    const stop = startTheme()
    expect(document.documentElement.dataset.theme).toBe('commons')
    expect(stop).not.toThrow()
  })
})
