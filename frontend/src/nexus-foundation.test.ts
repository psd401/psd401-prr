// ABOUTME: Guards the Nexus foundation: vendored tokens/fonts pinned to a standards
// ABOUTME: commit, Tailwind mapped onto them, and no second font stack creeping back.
/// <reference types="node" />
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

const read = (path: string) => readFileSync(resolve(process.cwd(), path), 'utf8')
const stylesheet = read('src/index.css')
const entrypoint = read('src/main.tsx')
const document = read('index.html')

describe('Nexus foundation', () => {
  it('vendors tokens, components and fonts with a pinned source commit', () => {
    expect(read('src/nexus/SOURCE.txt')).toMatch(
      /psd401\/psd-dev-standards design-system\/nexus\/web\/ at commit [0-9a-f]{40}/,
    )
    expect(existsSync(resolve(process.cwd(), 'src/nexus/bundle.css'))).toBe(true)
    const tokens = read('src/nexus/tokens.css')
    for (const theme of ['commons', 'dark', 'commons-dark', 'high-contrast']) {
      expect(tokens).toContain(`[data-theme="${theme}"]`)
    }
    for (const match of tokens.matchAll(/url\('\.\/(fonts\/[^']+)'\)/g)) {
      expect(existsSync(resolve(process.cwd(), 'src/nexus', match[1] ?? ''))).toBe(true)
    }
    expect(readdirSync(resolve(process.cwd(), 'src/nexus/fonts'))).toContain('LICENSES.txt')
  })

  it('loads Nexus under Tailwind so utilities still override nx- components', () => {
    expect(stylesheet).toContain('@import "./nexus/tokens.css" layer(base);')
    expect(stylesheet).toContain('@import "./nexus/bundle.css" layer(components);')
    expect(stylesheet).toContain('@theme inline')
    expect(stylesheet).toContain('--color-ink: var(--ink);')
    expect(stylesheet).toContain('--font-heading: var(--font-sans);')
    expect(stylesheet).toContain('--font-editorial: var(--font-display);')
    // The legacy brand palette is gone; colors come only from Nexus tokens.
    expect(stylesheet).not.toMatch(/--color-(pacific|seafoam|cedar|whulge)/)
    expect(entrypoint).not.toContain('@fontsource/')
  })

  it('resolves the favicon from the configured Vite base URL', () => {
    expect(document).toContain(
      'href="%BASE_URL%brand/psd_logo-2color-emblem.png"',
    )
    expect(document).not.toContain('href="/static/app/brand/')
  })
})

describe('UI kit raw values', () => {
  // Standard 10 rule 2: no hex colors, font families or pixel values a token covers.
  const kit = readdirSync(resolve(process.cwd(), 'src/ui'))
    .filter((name) => name.endsWith('.tsx') && !name.endsWith('.test.tsx'))
  it.each(kit)('%s uses tokens, not raw values', (name) => {
    const source = read(`src/ui/${name}`).replace(/viewBox="[^"]*"|d="[^"]*"/g, '')
    expect(source).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
    expect(source).not.toMatch(/rgba?\(/)
    expect(source).not.toMatch(/\[[^\]]*\d+px[^\]]*\]/)
    expect(source).not.toMatch(/font-family|fontFamily/)
  })
})
