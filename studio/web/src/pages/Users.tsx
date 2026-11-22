// The Users page, for admins: every account, how each signs in, its role and
// state, and adding accounts and setting passwords.

import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ROLES, type PasswordLogin, type Role, type User } from '../api'
import { useUser } from '../App'
import { Failure, Loading, PageHead, Pill } from '../components/ui'
import { ago } from '../format'

type TwoFactorPolicy = 'off' | 'admins' | 'all'

export interface UsersPage {
  users: User[]
  sso: { roles: 'studio' | 'provider' } | null
  passwordLogin: PasswordLogin
  requireTwoFactor: TwoFactorPolicy
}

type Change = { active?: boolean; role?: Role; removePassword?: boolean }

const ROLE_HELP: Record<Role, string> = {
  viewer: 'reads every screen',
  operator: 'also starts runs, and runs checks that connect to the databases',
  admin: 'also edits the jobs files and manages users',
}
const PROVIDER_ROLES_NOTE = 'provider-roles-note'

/**
 * How someone can actually sign in: a password they have counts only if the
 * policy lets their role use one.
 */
export function signsInWith(user: User, page: UsersPage): string {
  const passwordUsable = user.passwordSet && (page.passwordLogin === 'all' || (page.passwordLogin === 'admins' && user.role === 'admin'))
  const twoFactorNeeded = page.requireTwoFactor === 'all' || (page.requireTwoFactor === 'admins' && user.role === 'admin')
  const password = user.twoFactor ? 'password + 2FA' : twoFactorNeeded ? 'password, 2FA at next sign-in' : 'password'
  const ways = [...(user.sso ? ['SSO'] : []), ...(passwordUsable ? [password] : [])]
  if (ways.length) return ways.join(' or ')
  return page.sso ? 'SSO, once they first sign in' : 'nothing: set a password'
}

/** Every account as a table, with the changes an admin can make to each. */
export function Users() {
  const me = useUser()
  const client = useQueryClient()
  const users = useQuery({ queryKey: ['users'], queryFn: () => api<UsersPage>('/users') })
  const refresh = { onSuccess: () => client.invalidateQueries({ queryKey: ['users'] }) }
  const change = useMutation({ mutationFn: ({ id, body }: { id: number; body: Change }) => api<User>(`/users/${id}`, { method: 'PATCH', body }), ...refresh })
  const unlink = useMutation({ mutationFn: (id: number) => api<User>(`/users/${id}/sso`, { method: 'DELETE' }), ...refresh })
  const resetTwoFactor = useMutation({ mutationFn: (id: number) => api<User>(`/users/${id}/2fa`, { method: 'DELETE' }), ...refresh })
  const failure = change.error ?? unlink.error ?? resetTwoFactor.error
  const page = users.data
  const [settingPasswordOf, setSettingPasswordOf] = useState<User | null>(null)

  return (
    <>
      <PageHead title="Users" sub={<>Viewers {ROLE_HELP.viewer}; operators {ROLE_HELP.operator}; admins {ROLE_HELP.admin}.</>}>
        <AddUser page={page} />
      </PageHead>
      {failure && <Failure error={failure} />}
      <SetPassword user={settingPasswordOf} onClose={() => setSettingPasswordOf(null)} />
      {page?.sso?.roles === 'provider' && (
        <div className="banner info" role="status" id={PROVIDER_ROLES_NOTE}>
          <div>Roles of people who sign in with SSO are set by their groups at the identity provider, at each sign-in.</div>
        </div>
      )}
      {users.isPending ? <Loading /> : users.error ? <Failure error={users.error} /> : (
        <div className="tablebox">
          <table>
            <thead><tr><th>Email</th><th>Name</th><th>Role</th><th>Signs in with</th><th>Last signed in</th><th>Status</th><th><span className="visually-hidden">Actions</span></th></tr></thead>
            <tbody>
              {users.data.users.map((user) => {
                const roleLocked = users.data.sso?.roles === 'provider' && user.sso
                const you = user.id === me.id
                return (
                  <tr key={user.id}>
                    <td>{user.email}{you && <span className="small"> (you)</span>}</td>
                    <td>{user.name || <span className="muted">—</span>}</td>
                    <td style={{ whiteSpace: 'nowrap' }}>
                      <label className="visually-hidden" htmlFor={`role-${user.id}`}>Role of {user.email}</label>
                      <select id={`role-${user.id}`} value={user.role} disabled={roleLocked} aria-describedby={roleLocked ? PROVIDER_ROLES_NOTE : undefined}
                        onChange={(event) => change.mutate({ id: user.id, body: { role: event.target.value as Role } })}>
                        {ROLES.map((role) => <option key={role}>{role}</option>)}
                      </select>
                      {roleLocked && <span className="small"> from provider</span>}
                    </td>
                    <td>{signsInWith(user, users.data)}</td>
                    <td>{ago(user.lastLoginAt)}</td>
                    <td>{user.active ? <Pill kind="ok">Active</Pill> : <Pill kind="idle">Deactivated</Pill>}</td>
                    <td>
                      <div className="row">
                        {(page?.passwordLogin === 'all' || (page?.passwordLogin === 'admins' && user.role === 'admin')) && (
                          <button type="button" className="btn sm" onClick={() => setSettingPasswordOf(user)}>
                            {user.passwordSet ? 'Set password' : 'Give a password'}
                          </button>
                        )}
                        {user.sso && user.passwordSet && (
                          <button type="button" className="btn sm" title="They sign in only through the identity provider then, so disabling them there keeps them out"
                            onClick={() => { if (window.confirm(`Remove ${user.email}'s password? They will sign in only with SSO, and are signed out everywhere.`)) change.mutate({ id: user.id, body: { removePassword: true } }) }}>
                            Remove password
                          </button>
                        )}
                        {user.twoFactor && (
                          <button type="button" className="btn sm" title="For someone who lost their authenticator app and their recovery codes"
                            onClick={() => { if (window.confirm(`Reset ${user.email}'s two-factor sign-in? They set it up again, and are signed out everywhere.`)) resetTwoFactor.mutate(user.id) }}>
                            Reset 2FA
                          </button>
                        )}
                        {user.sso && !you && (
                          <button type="button" className="btn sm" title="For when their identity at the provider was made again: their next sign-in links them by email"
                            onClick={() => { if (window.confirm(`Unlink ${user.email} from single sign-on? They are signed out everywhere.`)) unlink.mutate(user.id) }}>
                            Unlink SSO
                          </button>
                        )}
                        <button type="button" className={`btn sm ${user.active ? 'danger' : ''}`} onClick={() => change.mutate({ id: user.id, body: { active: !user.active } })}>
                          {user.active ? 'Deactivate' : 'Reactivate'}
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

/**
 * An admin setting someone's password: for someone who forgot theirs, since
 * Studio sends no reset emails. They are signed out everywhere.
 */
function SetPassword({ user, onClose }: { user: User | null; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const client = useQueryClient()
  const [password, setPassword] = useState('')
  const [again, setAgain] = useState('')
  const mismatch = again !== '' && again !== password
  /** Clears the dialog and closes it. */
  const close = () => { setPassword(''); setAgain(''); set.reset(); dialog.current?.close(); onClose() }
  const set = useMutation({
    mutationFn: () => api<User>(`/users/${user?.id}`, { method: 'PATCH', body: { password } }),
    onSuccess: () => { void client.invalidateQueries({ queryKey: ['users'] }); close() },
  })

  useEffect(() => { if (user && !dialog.current?.open) dialog.current?.showModal() }, [user])

  return (
    <dialog ref={dialog} aria-labelledby="set-password-title" onClose={() => { if (user) close() }}>
      <form onSubmit={(event) => { event.preventDefault(); if (!mismatch) set.mutate() }}>
        <h2 id="set-password-title" style={{ margin: 0 }}>Set {user?.email}’s password</h2>
        <p className="small" style={{ margin: 0 }}>They are signed out everywhere. Give it to them another way than email, and they can change it on their Account page.</p>
        <label className="field">New password, at least 12 characters
          <input type="password" autoComplete="new-password" required minLength={12} value={password} onChange={(event) => setPassword(event.target.value)} />
        </label>
        <label className="field">Again
          <input type="password" autoComplete="new-password" required value={again} aria-invalid={mismatch} onChange={(event) => setAgain(event.target.value)} />
        </label>
        {mismatch && <p className="error" role="alert">The passwords differ.</p>}
        {set.error && <p className="error" role="alert">{set.error.message}</p>}
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" className="btn" onClick={close}>Cancel</button>
          <button type="submit" className="btn primary" disabled={set.isPending || mismatch}>Set password</button>
        </div>
      </form>
    </dialog>
  )
}

/** Adding an account: with a password, or, where single sign-on is on, for the provider alone. */
function AddUser({ page }: { page: UsersPage | undefined }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const client = useQueryClient()
  const empty = { email: '', name: '', role: 'viewer' as Role, password: '' }
  const [form, setForm] = useState(empty)
  const sso = Boolean(page?.sso)
  // A password the policy would let this role use; with none, they sign in only through the identity provider.
  const passwords = !page || page.passwordLogin === 'all' || (page.passwordLogin === 'admins' && form.role === 'admin')
  const add = useMutation({
    mutationFn: () => api<User>('/users', { method: 'POST', body: { ...form, password: passwords && form.password ? form.password : undefined } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['users'] })
      setForm(empty)
      dialog.current?.close()
    },
  })

  return (
    <>
      <button type="button" className="btn primary" onClick={() => dialog.current?.showModal()}>Add a user</button>
      <dialog ref={dialog} aria-labelledby="add-user-title">
        <form onSubmit={(event) => { event.preventDefault(); add.mutate() }}>
          <h2 id="add-user-title" style={{ margin: 0 }}>Add a user</h2>
          <label className="field">Email<input type="email" required value={form.email} onChange={(event) => setForm({ ...form, email: event.target.value })} /></label>
          <label className="field">Name<input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></label>
          <label className="field">Role
            <select value={form.role} onChange={(event) => setForm({ ...form, role: event.target.value as Role })}>
              {ROLES.map((role) => <option key={role} value={role}>{role}: {ROLE_HELP[role]}</option>)}
            </select>
          </label>
          {passwords ? (
            <label className="field">
              {sso ? 'Password, at least 12 characters; leave it empty for someone who signs in only with SSO' : 'Starting password, at least 12 characters'}
              <input type="password" autoComplete="new-password" required={!sso} minLength={12} value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} />
            </label>
          ) : <p className="small" style={{ margin: 0 }}>They sign in with SSO: the email must be the one the identity provider has for them.</p>}
          {add.error && <p className="error" role="alert">{add.error.message}</p>}
          <div className="row" style={{ justifyContent: 'flex-end' }}>
            <button type="button" className="btn" onClick={() => dialog.current?.close()}>Cancel</button>
            <button type="submit" className="btn primary" disabled={add.isPending}>Add</button>
          </div>
        </form>
      </dialog>
    </>
  )
}
