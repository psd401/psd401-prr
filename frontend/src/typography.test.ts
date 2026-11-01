// ABOUTME: Guards the staff workspace's legible, self-hosted typography system.
// ABOUTME: Prevents display fonts from drifting back into dense operational UI.
/// <reference types="node" />
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

const stylesheet = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf8')
const entrypoint = readFileSync(resolve(process.cwd(), 'src/main.tsx'), 'utf8')
const document = readFileSync(resolve(process.cwd(), 'index.html'), 'utf8')

describe('workspace typography', () => {
  it('uses Source Sans for operational UI and Source Serif as a restrained accent', () => {
    expect(stylesheet).toContain('--font-heading: "Source Sans 3"')
    expect(stylesheet).toContain('--font-body: "Source Sans 3"')
    expect(stylesheet).toContain('--font-editorial: "Source Serif 4"')

    expect(entrypoint).toContain("@fontsource/source-sans-3/400.css")
    expect(entrypoint).toContain("@fontsource/source-sans-3/600.css")
    expect(entrypoint).toContain("@fontsource/source-sans-3/700.css")
    expect(entrypoint).toContain("@fontsource/source-serif-4/400.css")
    expect(entrypoint).not.toContain('@fontsource/josefin')
  })

  it('resolves the favicon from the configured Vite base URL', () => {
    expect(document).toContain(
      'href="%BASE_URL%brand/psd_logo-2color-emblem.png"',
    )
    expect(document).not.toContain('href="/static/app/brand/')
  })
})
