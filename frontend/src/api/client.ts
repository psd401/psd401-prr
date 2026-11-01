// ABOUTME: Fetch wrapper for the staff API — same-origin session auth with
// ABOUTME: Django CSRF header, JSON handling, and normalized errors.

export class ApiError extends Error {
  status: number
  data: unknown

  constructor(status: number, data: unknown) {
    super(ApiError.messageFrom(status, data))
    this.status = status
    this.data = data
  }

  static messageFrom(status: number, data: unknown): string {
    if (data && typeof data === 'object') {
      const d = data as Record<string, unknown>
      if (typeof d.detail === 'string') return d.detail
      const fieldErrors = Object.entries(d)
        .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(' ') : String(v)}`)
      if (fieldErrors.length) return fieldErrors.join('; ')
    }
    return `Request failed (${status})`
  }
}

function getCookie(name: string): string | undefined {
  return document.cookie
    .split('; ')
    .find((c) => c.startsWith(name + '='))
    ?.split('=')[1]
}

export async function api<T>(
  path: string,
  options: {
    method?: string
    body?: unknown
    fetchAllPages?: boolean
    signal?: AbortSignal
  } = {},
): Promise<T> {
  const method = options.method ?? 'GET'
  const headers: Record<string, string> = { Accept: 'application/json' }
  const isFormData = options.body instanceof FormData
  if (options.body !== undefined && !isFormData) {
    headers['Content-Type'] = 'application/json'
  }
  if (method !== 'GET') headers['X-CSRFToken'] = getCookie('csrftoken') ?? ''
  let requestBody: BodyInit | undefined
  if (options.body !== undefined) {
    requestBody = isFormData
      ? options.body as FormData
      : JSON.stringify(options.body)
  }

  const request = async (requestPath: string): Promise<unknown> => {
    const response = await fetch(requestPath, {
      method,
      headers,
      credentials: 'same-origin',
      body: requestBody,
      signal: options.signal,
    })

    const text = await response.text()
    let data: unknown = null
    if (text) {
      try {
        data = JSON.parse(text)
      } catch {
        data = { detail: `Non-JSON response (HTTP ${response.status})` }
      }
    }
    if (!response.ok) throw new ApiError(response.status, data)
    return data
  }

  const first = await request(path)
  if (method !== 'GET' || !isPage(first) || options.fetchAllPages === false) {
    return first as T
  }

  const results = [...first.results]
  const visited = new Set<string>([path])
  let next = first.next
  while (next) {
    if (visited.has(next)) throw new Error('Paginated API response contains a cycle.')
    visited.add(next)
    const nextPage = await request(next)
    if (!isPage(nextPage)) {
      throw new Error('Paginated API response changed shape between pages.')
    }
    results.push(...nextPage.results)
    next = nextPage.next
  }
  return { ...first, results, next: null, previous: null } as T
}

/** DRF page envelope. */
export interface Page<T> {
  count: number
  next: string | null
  previous: string | null
  results: T[]
}

function isPage(value: unknown): value is Page<unknown> {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Partial<Page<unknown>>
  return (
    typeof candidate.count === 'number'
    && Array.isArray(candidate.results)
    && (typeof candidate.next === 'string' || candidate.next === null)
    && (typeof candidate.previous === 'string' || candidate.previous === null)
  )
}

/** Accepts a paginated or bare-array list response. */
export function unwrap<T>(payload: Page<T> | T[]): T[] {
  return Array.isArray(payload) ? payload : payload.results
}
