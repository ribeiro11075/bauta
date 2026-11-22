// The web app's root: who is signed in, the sign-in page for no one, and for
// everyone else the navigation and each page's route, an admin's pages only for
// admins.

import { createContext, useContext, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { api, can, signInOptions, type User } from './api'
import { Loading } from './components/ui'
import { SignIn } from './pages/SignIn'
import { Overview } from './pages/Overview'
import { Runs } from './pages/Runs'
import { RunPage, LaunchedPage } from './pages/RunDetail'
import { Jobs, JobPage } from './pages/Jobs'
import { Proof } from './pages/Proof'
import { Connections } from './pages/Connections'
import { Configuration } from './pages/Configuration'
import { Users } from './pages/Users'
import { Account } from './pages/Account'
import { ActivityPage } from './pages/Activity'
import { Schedule } from './pages/Schedule'
import { Discovery } from './pages/Discovery'
import { JobEditor } from './pages/JobEditor'
import { Subset } from './pages/Subset'
import { Compare } from './pages/Compare'

const UserContext = createContext<User | null>(null)

/** The signed-in user, for any page inside the app. */
export function useUser(): User {
  const user = useContext(UserContext)
  if (!user) throw new Error('useUser outside a signed-in page')
  return user
}

/** Asks who is signed in, and shows the sign-in page or the app accordingly. */
export function App() {
  const me = useQuery({ queryKey: ['me'], queryFn: () => api<User>('/auth/me'), retry: false })

  if (me.isPending) return <Loading />
  if (me.error) return <SignIn options={signInOptions(me.error)} />

  return (
    <UserContext.Provider value={me.data}>
      <Shell>
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/runs" element={<Runs />} />
          <Route path="/runs/compare" element={<Compare />} />
          <Route path="/runs/:runId" element={<RunPage />} />
          <Route path="/launched/:launchedId" element={<LaunchedPage />} />
          <Route path="/jobs" element={<Jobs />} />
          {can(me.data, 'admin') && <Route path="/jobs/new" element={<JobEditor />} />}
          <Route path="/jobs/:name" element={<JobPage />} />
          {can(me.data, 'admin') && <Route path="/jobs/:name/edit" element={<JobEditor />} />}
          <Route path="/schedule" element={<Schedule />} />
          <Route path="/discovery" element={<Discovery />} />
          {can(me.data, 'admin') && <Route path="/subset" element={<Subset />} />}
          <Route path="/proof" element={<Proof />} />
          <Route path="/connections" element={<Connections />} />
          <Route path="/configuration" element={<Configuration />} />
          <Route path="/account" element={<Account />} />
          {can(me.data, 'admin') && <Route path="/users" element={<Users />} />}
          {can(me.data, 'admin') && <Route path="/activity" element={<ActivityPage />} />}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Shell>
    </UserContext.Provider>
  )
}

/**
 * The navigation beside the page, with the signed-in user and sign-out. Signing
 * out keeps nothing of the session: a fresh page, on the sign-in form.
 */
function Shell({ children }: { children: ReactNode }) {
  const user = useUser()
  const client = useQueryClient()
  const signOut = useMutation({
    mutationFn: () => api('/auth/logout', { method: 'POST' }),
    // Nothing of the session kept: a fresh page, on the sign-in form.
    onSettled: () => { client.clear(); window.location.replace('/') },
  })

  return (
    <div className="app">
      <nav className="side" aria-label="Main">
        <div className="brand"><img src="/favicon.svg" alt="" />bauta <span>Studio</span></div>
        <NavLink className="navlink" to="/" end>Overview</NavLink>
        <div className="navgroup">Watch</div>
        <NavLink className="navlink" to="/runs">Runs</NavLink>
        <NavLink className="navlink" to="/schedule">Schedule</NavLink>
        <NavLink className="navlink" to="/proof">Proof</NavLink>
        <div className="navgroup">Configure</div>
        <NavLink className="navlink" to="/jobs">Jobs</NavLink>
        <NavLink className="navlink" to="/discovery">Discovery review</NavLink>
        {can(user, 'admin') && <NavLink className="navlink" to="/subset">Subset builder</NavLink>}
        <NavLink className="navlink" to="/connections">Connections</NavLink>
        <NavLink className="navlink" to="/configuration">Configuration</NavLink>
        {can(user, 'admin') && (
          <>
            <div className="navgroup">Admin</div>
            <NavLink className="navlink" to="/users">Users</NavLink>
            <NavLink className="navlink" to="/activity">Activity</NavLink>
          </>
        )}
        <div className="who">
          <NavLink className="navlink" to="/account">{user.name || user.email}<br /><span className="small">{user.role}</span></NavLink>
          <button type="button" className="btn link" onClick={() => signOut.mutate()}>Sign out</button>
        </div>
      </nav>
      <main className="main">{children}</main>
    </div>
  )
}
