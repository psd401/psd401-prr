// ABOUTME: Picks the Nexus theme (data-theme on <html>) from a per-device choice or the
// ABOUTME: device's own contrast and dark-mode settings, and keeps it current.

export const THEMES = ['nexus', 'commons', 'dark', 'commons-dark', 'high-contrast'] as const
export type Theme = (typeof THEMES)[number]
export type ThemeChoice = Theme | 'system'

export const THEME_LABELS: Record<ThemeChoice, string> = {
  system: 'Match this device',
  nexus: 'Nexus',
  commons: 'Commons',
  dark: 'Nexus dark',
  'commons-dark': 'Commons dark',
  'high-contrast': 'High contrast',
}

/** localStorage key. A display preference for this browser only, never a district setting. */
export const THEME_STORAGE_KEY = 'prr-theme'

function isChoice(value: unknown): value is ThemeChoice {
  return value === 'system' || (THEMES as readonly unknown[]).includes(value)
}

export function readThemeChoice(): ThemeChoice {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
    return isChoice(stored) ? stored : 'system'
  } catch {
    // Storage can be blocked (private windows, policy); the device setting still applies.
    return 'system'
  }
}

export function writeThemeChoice(choice: ThemeChoice) {
  try {
    if (choice === 'system') window.localStorage.removeItem(THEME_STORAGE_KEY)
    else window.localStorage.setItem(THEME_STORAGE_KEY, choice)
  } catch {
    // Not remembered, but still applied for this page.
  }
}

function matches(query: string) {
  return typeof window.matchMedia === 'function' && window.matchMedia(query).matches
}

/** Nexus is the family for this data-heavy workspace; the device picks contrast and dark. */
export function resolveTheme(choice: ThemeChoice): Theme {
  if (choice !== 'system') return choice
  if (matches('(prefers-contrast: more)')) return 'high-contrast'
  if (matches('(prefers-color-scheme: dark)')) return 'dark'
  return 'nexus'
}

export function applyTheme(choice: ThemeChoice) {
  document.documentElement.setAttribute('data-theme', resolveTheme(choice))
}

/**
 * Apply the stored choice now and follow the device's settings while the
 * choice is "system". Returns the cleanup for the media listeners.
 */
export function startTheme(): () => void {
  applyTheme(readThemeChoice())
  if (typeof window.matchMedia !== 'function') return () => {}
  const queries = ['(prefers-contrast: more)', '(prefers-color-scheme: dark)'].map(
    (query) => window.matchMedia(query),
  )
  const update = () => applyTheme(readThemeChoice())
  queries.forEach((query) => query.addEventListener('change', update))
  return () => queries.forEach((query) => query.removeEventListener('change', update))
}
