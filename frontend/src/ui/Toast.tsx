// ABOUTME: Minimal toast system — success/error notices in the corner, auto-
// ABOUTME: dismissing, so no action outcome is silent.
import {
  useCallback,
  useState,
  type ReactNode,
} from 'react'

import { ToastContext, type ToastTone } from './toastContext'

interface ToastMessage {
  id: number
  tone: ToastTone
  text: string
}

let nextId = 1

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastMessage[]>([])

  const push = useCallback((tone: ToastTone, text: string) => {
    const id = nextId++
    setToasts((t) => [...t, { id, tone, text }])
    setTimeout(() => setToasts((t) => t.filter((m) => m.id !== id)), 5000)
  }, [])

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex max-w-[calc(100vw-2rem)] flex-col items-end gap-2">
        {toasts.map((t) => (
          <div
            key={t.id}
            role="status"
            // An error toast keeps the danger palette rather than Nexus's
            // inverse toast, so a failure never reads like a confirmation.
            className={`nx-toast pointer-events-auto min-w-0 ${
              t.tone === 'success'
                ? ''
                : 'border border-danger bg-danger-soft text-danger-ink'
            }`}
          >
            <span aria-hidden="true">{t.tone === 'success' ? '✓' : '!'}</span>
            {t.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}
