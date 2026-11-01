// ABOUTME: Anonymous public-intake client using the endpoint's multipart and
// ABOUTME: same-origin CSRF contract without importing staff API behavior.

export interface PublicIntakeValues {
  description: string
  email: string
  name: string
  phone: string
  street_address: string
  city: string
  state: string
  postal_code: string
  company: string
  website: string
}

export interface PublicIntakeResponse {
  number: string
}

const PUBLIC_ATTACHMENT_MIME_BY_EXTENSION: Record<string, string> = {
  '.pdf': 'application/pdf',
  '.rtf': 'application/rtf',
  '.doc': 'application/msword',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.xls': 'application/vnd.ms-excel',
  '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  '.ppt': 'application/vnd.ms-powerpoint',
  '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  '.gif': 'image/gif',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.tif': 'image/tiff',
  '.tiff': 'image/tiff',
  '.eml': 'message/rfc822',
  '.csv': 'text/csv',
  '.txt': 'text/plain',
}
const PUBLIC_ATTACHMENT_MIME_TYPES = new Set(
  Object.values(PUBLIC_ATTACHMENT_MIME_BY_EXTENSION),
)
export const PUBLIC_ATTACHMENT_ACCEPT = Object.keys(PUBLIC_ATTACHMENT_MIME_BY_EXTENSION).join(',')

function attachmentMimeFromName(filename: string): string | undefined {
  const lowerName = filename.toLowerCase()
  const extension = Object.keys(PUBLIC_ATTACHMENT_MIME_BY_EXTENSION).find((candidate) => (
    lowerName.endsWith(candidate)
  ))
  return extension ? PUBLIC_ATTACHMENT_MIME_BY_EXTENSION[extension] : undefined
}

export function isSupportedPublicAttachment(file: File): boolean {
  return (
    PUBLIC_ATTACHMENT_MIME_TYPES.has(file.type.toLowerCase())
    || attachmentMimeFromName(file.name) !== undefined
  )
}

function normalizePublicAttachment(file: File): File {
  if (PUBLIC_ATTACHMENT_MIME_TYPES.has(file.type.toLowerCase())) return file
  const contentType = attachmentMimeFromName(file.name)
  if (!contentType) return file
  return new File([file], file.name, {
    type: contentType,
    lastModified: file.lastModified,
  })
}

export class PublicIntakeError extends Error {
  status: number
  data: unknown

  constructor(status: number, data: unknown) {
    super(publicIntakeErrorMessage(status, data))
    this.status = status
    this.data = data
  }
}

function detailFrom(data: unknown): string | undefined {
  if (!data || typeof data !== 'object') return undefined
  const detail = (data as Record<string, unknown>).detail
  return typeof detail === 'string' ? detail : undefined
}

function publicIntakeErrorMessage(status: number, data: unknown): string {
  if (status === 429) {
    return 'Too many submissions have been received. Please wait and try again.'
  }
  if (status === 413) {
    return 'The selected attachments exceed the upload limits. Please choose fewer or smaller files.'
  }
  if (status === 409) {
    return detailFrom(data) ?? 'This form has already been submitted. Reload the form before trying again.'
  }
  if (status === 503) {
    return 'The request could not be processed right now. Your form is still here so you can try again.'
  }
  return detailFrom(data) ?? 'The request could not be submitted. Please review the form and try again.'
}

function readCookie(name: string): string {
  const prefix = `${name}=`
  const value = document.cookie
    .split('; ')
    .find((cookie) => cookie.startsWith(prefix))
    ?.slice(prefix.length)
  return value ? decodeURIComponent(value) : ''
}

async function responseData(response: Response): Promise<unknown> {
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text) as unknown
  } catch {
    return { detail: `The server returned an unreadable response (HTTP ${response.status}).` }
  }
}

export async function submitPublicIntake(
  values: PublicIntakeValues,
  attachments: File[],
): Promise<PublicIntakeResponse> {
  const body = new FormData()
  for (const [field, value] of Object.entries(values)) body.append(field, value)
  for (const attachment of attachments) {
    body.append('attachments', normalizePublicAttachment(attachment))
  }

  const response = await fetch('/api/public/requests/', {
    method: 'POST',
    headers: {
      Accept: 'application/json',
      'X-CSRFToken': readCookie('csrftoken'),
    },
    credentials: 'same-origin',
    body,
  })
  const data = await responseData(response)
  if (!response.ok) throw new PublicIntakeError(response.status, data)
  if (
    !data
    || typeof data !== 'object'
    || typeof (data as Record<string, unknown>).number !== 'string'
  ) {
    throw new PublicIntakeError(502, {
      detail: 'The server accepted the request but did not return a request number.',
    })
  }
  return data as PublicIntakeResponse
}
