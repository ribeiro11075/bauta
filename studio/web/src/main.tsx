import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'
import '@fontsource/ibm-plex-sans/400.css'
import '@fontsource/ibm-plex-sans/500.css'
import '@fontsource/ibm-plex-sans/600.css'
import '@fontsource/ibm-plex-mono/400.css'
import '@fontsource/ibm-plex-mono/500.css'
import './styles.css'
import { App } from './App'
import { ApiError } from './api'

// A 401 anywhere means the session ended -- signed out elsewhere, expired,
// or the user deactivated: ask again who is signed in, which shows sign-in.
function signedOut(error: unknown, key?: readonly unknown[]) {
  if (error instanceof ApiError && error.status === 401 && key?.[0] !== 'me') client.invalidateQueries({ queryKey: ['me'] })
}

const client: QueryClient = new QueryClient({
  queryCache: new QueryCache({ onError: (error, query) => signedOut(error, query.queryKey) }),
  mutationCache: new MutationCache({ onError: (error) => signedOut(error) }),
  defaultOptions: {
    queries: {
      // A 401 or 403 won't pass on a retry; anything else may.
      retry: (failures, error) => !(error instanceof ApiError && [401, 403, 404, 409].includes(error.status)) && failures < 2,
      refetchOnWindowFocus: true,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
