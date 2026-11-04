# PRR Staff Frontend

React SPA for district records staff. It is a plain client of the staff DRF
API (`/api/staff/...`, session-authenticated); it holds no business logic
and no compliance rules. Stack: bun, Vite, React, TypeScript, Tailwind v4
over the Nexus design system, TanStack Query, react-router.

```bash
bun install
bun run dev     # Vite on :5173, /api proxied to Django on :8000
bun run build   # production bundle served by Django at /
bunx vitest run # tests
```

Screens are wired in `src/nav.ts` and `src/App.tsx`. Each resource screen
maps to one DRF resource in `apps/api/resources/`.

## Design system (Nexus)

Styles come from Nexus ([psd-dev-standards standard 10](https://github.com/psd401/psd-dev-standards/blob/main/standards/10-design-system.md)),
adopted CSS-first because its React bundle targets React 18 and its preset
Tailwind v3.

- `src/nexus/` vendors `tokens.css`, `bundle.css` and `fonts/` at the commit
  in `src/nexus/SOURCE.txt`. Never edit them; to update, copy a newer
  `design-system/nexus/web/` export and change the pinned commit.
- `src/index.css` maps the tokens to Tailwind colors (`text-ink`,
  `bg-surface-raised`, `border-line`, `bg-warning-soft`, …). Use those or the
  `nx-` classes; no raw hex, font or pixel values where a token exists.
- Themes switch through `data-theme` on `<html>` (`src/theme.ts`): the
  Appearance picker's per-browser choice, else the device's contrast and dark
  settings. Every theme must pass the `accessibility-audit` e2e flow.
- Every staff screen carries the `TrustFooter` (from `AppLayout`).

