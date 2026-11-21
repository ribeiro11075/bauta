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
import { ApiError, SIGNING_IN } from './api'

// A 401 anywhere means the session ended -- signed out elsewhere, expired,
// or the user deactivated: ask again who is signed in, which shows sign-in.
// Not while signing in, where a 401 is a wrong password or code: asking
// again would start the sign-in page over, and lose what it said was wrong.
function signedOut(error: unknown, meta: Record<string, unknown> | undefined, key?: readonly unknown[]) {
  if (error instanceof ApiError && error.status === 401 && key?.[0] !== 'me' && !meta?.[SIGNING_IN]) client.invalidateQueries({ queryKey: ['me'] })
}

const client: QueryClient = new QueryClient({
  queryCache: new QueryCache({ onError: (error, query) => signedOut(error, query.meta, query.queryKey) }),
  mutationCache: new MutationCache({ onError: (error, _variables, _context, mutation) => signedOut(error, mutation.meta) }),
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
