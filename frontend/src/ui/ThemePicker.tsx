// ABOUTME: Appearance control: one of the five Nexus themes, or follow the device.
// ABOUTME: The choice is remembered in this browser only.
import { useId, useState } from 'react'

import {
  THEMES,
  THEME_LABELS,
  applyTheme,
  readThemeChoice,
  writeThemeChoice,
  type ThemeChoice,
} from '../theme'

const CHOICES: ThemeChoice[] = ['system', ...THEMES]

export function ThemePicker({ className = '' }: { className?: string }) {
  const id = useId()
  const [choice, setChoice] = useState<ThemeChoice>(readThemeChoice)
  return (
    <div className={className}>
      <label htmlFor={id} className="block text-xs">
        Appearance
      </label>
      <select
        id={id}
        value={choice}
        onChange={(event) => {
          const next = event.target.value as ThemeChoice
          setChoice(next)
          writeThemeChoice(next)
          applyTheme(next)
        }}
        className="nx-select mt-1 h-8 text-sm"
      >
        {CHOICES.map((value) => (
          <option key={value} value={value}>
            {THEME_LABELS[value]}
          </option>
        ))}
      </select>
    </div>
  )
}
