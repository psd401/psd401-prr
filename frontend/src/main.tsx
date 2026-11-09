// ABOUTME: Entry point — sets the Nexus theme, then mounts the app with React Query
// ABOUTME: and toasts. Nexus fonts are self-hosted through index.css.
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import './index.css'
import App from './App.tsx'
import { startTheme } from './theme'
import { ToastProvider } from './ui'

// Before the first render, so no screen paints in the wrong theme.
startTheme()

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <App />
      </ToastProvider>
    </QueryClientProvider>
  </StrictMode>,
)
