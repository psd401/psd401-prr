// ABOUTME: Public confirmation page that makes the assigned PRR number
// ABOUTME: prominent, copyable, and durable in the route query string.
import { useState } from 'react'
import { useSearchParams } from 'react-router'
import { Button } from '../ui'
import { PublicRequestLayout } from './PublicRequestLayout'

export function PublicRequestReceipt() {
  const [searchParams] = useSearchParams()
  const number = searchParams.get('number')?.trim() ?? ''
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')

  const copyNumber = async () => {
    if (!number) return
    setCopyState('idle')
    try {
      if (!navigator.clipboard) throw new Error('Clipboard API unavailable')
      await navigator.clipboard.writeText(number)
      setCopyState('copied')
    } catch {
      setCopyState('failed')
    }
  }

  return (
    <PublicRequestLayout>
      <section className="mx-auto max-w-2xl rounded-2xl border border-driftwood/70 bg-white p-6 text-center shadow-[0_1px_2px_rgba(37,66,76,0.06),0_12px_32px_rgba(37,66,76,0.06)] sm:p-10">
        {number ? (
          <>
            <div className="mx-auto flex size-14 items-center justify-center rounded-full bg-seaglass/20 text-2xl text-cedar" aria-hidden="true">
              ✓
            </div>
            <h1 className="mt-5 text-3xl font-bold sm:text-4xl">Request submitted</h1>
            <p className="mx-auto mt-3 max-w-xl text-pacific/80">
              Keep this request number. It is the best way to identify your request when contacting the district.
            </p>
            <div className="my-7 rounded-xl border border-ocean/40 bg-skylight px-4 py-5">
              <p className="font-heading text-xs font-semibold uppercase tracking-[0.08em] text-cedar">Request number</p>
              <p className="mt-1 select-all font-heading text-2xl font-bold tracking-wide text-pacific sm:text-3xl">
                {number}
              </p>
              <Button
                type="button"
                variant="ghost"
                onClick={copyNumber}
                className="mx-auto mt-3 border-ocean focus-visible:outline-whulge"
              >
                {copyState === 'copied' ? 'Copied' : 'Copy request number'}
              </Button>
              {copyState === 'failed' && (
                <p role="status" className="mt-3 text-sm font-semibold text-pacific">
                  Copying is not available. Select the request number and copy it manually.
                </p>
              )}
            </div>
            <div className="space-y-3 text-left text-sm text-pacific/80 sm:text-base">
              <p>A confirmation email is on its way to the address you provided.</p>
              <p>
                The district will provide its formal response within five business days. That response
                may include the records, an estimated delivery date, a request for clarification, or a
                lawful reason records cannot be disclosed.
              </p>
            </div>
          </>
        ) : (
          <>
            <h1 className="text-3xl font-bold">Request number unavailable</h1>
            <p className="mt-3 text-pacific/80">
              This confirmation page was opened without a request number. If you submitted a request,
              contact the district so staff can help locate it.
            </p>
          </>
        )}
        <a
          href="/request"
          className="mt-8 inline-block rounded-md font-heading font-semibold text-whulge underline decoration-ocean/60 underline-offset-4 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-whulge"
        >
          Submit another request
        </a>
      </section>
    </PublicRequestLayout>
  )
}
