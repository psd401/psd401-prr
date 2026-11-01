// ABOUTME: Resolves public frontend assets against Vite's configured base URL.
// ABOUTME: Supports root-based local previews and /static/app/ deployments.
export function appAsset(path: string, base = import.meta.env.BASE_URL): string {
  const normalizedBase = base.endsWith('/') ? base : `${base}/`
  return `${normalizedBase}${path.replace(/^\/+/, '')}`
}
