// ABOUTME: Records that are not PDFs still reach a lawful, cited decision —
// ABOUTME: conversion, timed media redaction, or a reasoned opaque disposition.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../ui'
import { RecordHandlingPanel, type HandledRecord } from './RecordHandlingPanel'

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as Response
}

function page(results: unknown[]) {
  return { count: results.length, next: null, previous: null, results }
}

const EXEMPTION = {
  id: 7,
  citation: 'RCW 42.56.230(1)',
  short_name: 'Student personal information',
}

function record(overrides: Partial<HandledRecord> = {}): HandledRecord {
  return {
    id: 'aaaaaaaa-1111-4111-8111-111111111111',
    original_filename: 'memo.docx',
    content_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    review_state: 'unreviewed',
    handling: 'convert',
    handling_label: 'Needs conversion',
    handling_detail: 'A paginated rendition is needed before this can be read.',
    derivative_state: 'none',
    derivative_detail: '',
    reviewable: false,
    ...overrides,
  }
}

function renderPanel(value: HandledRecord, canReview = true) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <RecordHandlingPanel record={value} canReview={canReview} />
      </ToastProvider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('records that are not PDFs', () => {
  it('offers to build the rendition a reviewer can actually read', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => response({}))
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderPanel(record())

    await user.click(screen.getByRole('button', { name: 'Prepare for review' }))

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([input]) =>
        String(input).includes('/build-derivative/'),
      )).toBe(true)
    })
  })

  it('says why a conversion failed rather than showing a blank reviewer', async () => {
    vi.stubGlobal('fetch', vi.fn(async (_i: RequestInfo | URL, _o?: RequestInit) => response({})))
    renderPanel(record({
      derivative_state: 'unsupported',
      derivative_detail: 'Outlook .msg files need a dedicated parser.',
    }))
    expect(
      screen.getByText(/Outlook \.msg files need a dedicated parser\./),
    ).toBeTruthy()
  })

  it('explains that redactions burn into the rendition, not the original', () => {
    vi.stubGlobal('fetch', vi.fn(async (_i: RequestInfo | URL, _o?: RequestInit) => response({})))
    renderPanel(record())
    // The reason matters: releasing the original .docx would ship tracked
    // changes and author metadata no redaction box ever covered.
    expect(screen.getByText(/tracked changes/)).toBeTruthy()
  })

  it('hides every control from someone without review authority', () => {
    vi.stubGlobal('fetch', vi.fn(async (_i: RequestInfo | URL, _o?: RequestInit) => response({})))
    renderPanel(record(), false)
    expect(screen.queryByRole('button', { name: 'Prepare for review' })).toBeNull()
  })
})

describe('audio and video review', () => {
  const media = record({
    handling: 'media',
    handling_label: 'Audio or video',
    original_filename: 'bus-camera.mp4',
    content_type: 'video/mp4',
    review_state: 'responsive',
  })

  function stubMedia(rows: unknown[] = []) {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input)
      if (url.startsWith('/api/staff/exemptions/')) return response(page([EXEMPTION]))
      if (url.startsWith('/api/staff/media-redactions/')) return response(page(rows))
      return response({})
    })
    vi.stubGlobal('fetch', fetchMock)
    return fetchMock
  }

  it('marks an interval with its exemption citation', async () => {
    const fetchMock = stubMedia()
    const user = userEvent.setup()
    renderPanel(media)

    await user.type(screen.getByLabelText(/^Starts at/), '12.5')
    await user.type(screen.getByLabelText(/^Ends at/), '19.25')
    await user.selectOptions(screen.getByLabelText(/^Treatment/), 'mute_audio')
    await user.selectOptions(screen.getByLabelText(/^Exemption/), '7')
    await user.click(screen.getByRole('button', { name: 'Mark interval' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (entry) => String(entry[0]) === '/api/staff/media-redactions/',
      )
      expect(call).toBeTruthy()
      const body = JSON.parse(String(call![1]!.body))
      expect(body.starts_at).toBe(12.5)
      expect(body.ends_at).toBe(19.25)
      expect(body.treatment).toBe('mute_audio')
      expect(body.exemption).toBe(7)
    })
  })

  it('will not mark an interval without an exemption', async () => {
    stubMedia()
    const user = userEvent.setup()
    renderPanel(media)
    await user.type(screen.getByLabelText(/^Starts at/), '1')
    await user.type(screen.getByLabelText(/^Ends at/), '2')
    // The RCW 42.56.210(3) log is generated from these citations, so an
    // uncited withholding cannot be recorded at all.
    expect(screen.getByRole('button', { name: 'Mark interval' })).toHaveProperty(
      'disabled', true,
    )
  })

  it('will not mark an interval that ends before it starts', async () => {
    stubMedia()
    const user = userEvent.setup()
    renderPanel(media)
    await user.type(screen.getByLabelText(/^Starts at/), '30')
    await user.type(screen.getByLabelText(/^Ends at/), '10')
    await user.selectOptions(screen.getByLabelText(/^Exemption/), '7')
    expect(screen.getByRole('button', { name: 'Mark interval' })).toHaveProperty(
      'disabled', true,
    )
  })

  it('refuses to produce a released copy before a responsive verdict', async () => {
    stubMedia([{
      id: 'r1', starts_at: 1, ends_at: 2, treatment: 'mute_audio',
      exemption: 7, citation: 'RCW 42.56.230(1)', explanation: '',
      voided_at: null,
    }])
    renderPanel({ ...media, review_state: 'unreviewed' })
    await screen.findByText(/1\.00s – 2\.00s/)
    expect(
      screen.getByRole('button', { name: 'Produce released copy' }),
    ).toHaveProperty('disabled', true)
    expect(screen.getByText(/Mark the record responsive/)).toBeTruthy()
  })

  it('produces the released copy once intervals are marked', async () => {
    const fetchMock = stubMedia([{
      id: 'r1', starts_at: 4, ends_at: 9, treatment: 'obscure_region',
      exemption: 7, citation: 'RCW 42.56.230(1)', explanation: '',
      voided_at: null,
    }])
    const user = userEvent.setup()
    renderPanel(media)
    await screen.findByText(/4\.00s – 9\.00s/)

    await user.click(screen.getByRole('button', { name: 'Produce released copy' }))

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([input]) =>
        String(input).includes('/render-media-redactions/'),
      )).toBe(true)
    })
  })

  it('states that nothing is detected automatically', () => {
    stubMedia()
    renderPanel(media)
    expect(screen.getByText(/Nothing is detected automatically/)).toBeTruthy()
  })
})

describe('records nothing can open', () => {
  const opaque = record({
    handling: 'opaque',
    handling_label: 'No renderer available',
    handling_detail: 'No available renderer can open this format.',
    original_filename: 'portable.swf',
    content_type: 'application/x-shockwave-flash',
  })

  function stubOpaque() {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      if (String(input).startsWith('/api/staff/exemptions/')) {
        return response(page([EXEMPTION]))
      }
      return response({})
    })
    vi.stubGlobal('fetch', fetchMock)
    return fetchMock
  }

  it('requires reasoning long enough to be an actual explanation', async () => {
    stubOpaque()
    const user = userEvent.setup()
    renderPanel(opaque)
    await user.selectOptions(screen.getByLabelText(/^Basis/), 'not_inspectable')
    await user.selectOptions(screen.getByLabelText(/^Outcome/), 'non_responsive')
    await user.type(screen.getByLabelText(/^Reasoning/), 'cannot open it')
    // "cannot open" is not a defensible basis for deciding a public record.
    expect(screen.getByRole('button', { name: 'Record decision' })).toHaveProperty(
      'disabled', true,
    )
    expect(screen.getByText(/more characters needed/)).toBeTruthy()
  })

  it('records a decision with its basis and reasoning', async () => {
    const fetchMock = stubOpaque()
    const user = userEvent.setup()
    renderPanel(opaque)
    const reasoning =
      'Opened on an isolated workstation with a standalone Flash projector. '
      + 'Contains 2017 compliance training slides with no student identifiers.'
    await user.selectOptions(screen.getByLabelText(/^Basis/), 'opened_elsewhere')
    await user.type(screen.getByLabelText(/^Reasoning/), reasoning)
    await user.selectOptions(screen.getByLabelText(/^Outcome/), 'responsive')
    await user.click(screen.getByRole('button', { name: 'Record decision' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (entry) => String(entry[0]).includes('/opaque-disposition/'),
      )
      expect(call).toBeTruthy()
      const body = JSON.parse(String(call![1]!.body))
      expect(body.basis).toBe('opened_elsewhere')
      expect(body.review_state).toBe('responsive')
      expect(body.reasoning).toContain('isolated workstation')
    })
  })

  it('demands an exemption before withholding in full', async () => {
    stubOpaque()
    const user = userEvent.setup()
    renderPanel(opaque)
    await user.selectOptions(screen.getByLabelText(/^Basis/), 'not_inspectable')
    await user.type(screen.getByLabelText(/^Reasoning/), 'z'.repeat(120))
    await user.selectOptions(screen.getByLabelText(/^Outcome/), 'withheld')
    expect(screen.getByRole('button', { name: 'Record decision' })).toHaveProperty(
      'disabled', true,
    )
    await user.selectOptions(await screen.findByLabelText(/^Exemption/), '7')
    expect(screen.getByRole('button', { name: 'Record decision' })).toHaveProperty(
      'disabled', false,
    )
  })
})
