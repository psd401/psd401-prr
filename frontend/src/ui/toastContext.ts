// ABOUTME: Shared toast context and hook, separated from the provider so
// ABOUTME: React Fast Refresh sees component-only exports in Toast.tsx.
import { createContext, useContext } from 'react'

export type ToastTone = 'success' | 'error'

export const ToastContext = createContext<(tone: ToastTone, text: string) => void>(
  () => {},
)

export function useToast() {
  return useContext(ToastContext)
}
