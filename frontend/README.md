# PRR Staff Frontend

React SPA for district records staff. It is a plain client of the staff DRF
API (`/api/staff/...`, session-authenticated); it holds no business logic
and no compliance rules. Stack: bun, Vite, React, TypeScript, Tailwind v4
with PSD brand tokens, TanStack Query, react-router.

```bash
bun install
bun run dev     # Vite on :5173, /api proxied to Django on :8000
bun run build   # production bundle served by Django at /
bunx vitest run # tests
```

Screens are wired in `src/nav.ts` and `src/App.tsx`. Each resource screen
maps to one DRF resource in `apps/api/resources/`.
