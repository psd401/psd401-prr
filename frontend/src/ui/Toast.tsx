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
      <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2">
        {toasts.map((t) => (
          <div
            key={t.id}
            role="status"
            className={`pointer-events-auto rounded-md border px-4 py-3 text-sm shadow-lg ${
              t.tone === 'success'
                ? 'border-seaglass bg-white text-cedar'
                : 'border-red-300 bg-red-50 text-red-900'
            }`}
          >
            {t.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}
