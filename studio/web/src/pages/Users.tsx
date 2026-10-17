import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ROLES, type Role, type User } from '../api'
import { useUser } from '../App'
import { Failure, Loading, PageHead, Pill } from '../components/ui'
import { ago } from '../format'

const ROLE_HELP: Record<Role, string> = {
  viewer: 'reads every screen',
  operator: 'also starts runs, and runs checks that connect to the databases',
  admin: 'also edits the jobs files and manages users',
}

export function Users() {
  const me = useUser()
  const client = useQueryClient()
  const users = useQuery({ queryKey: ['users'], queryFn: () => api<{ users: User[] }>('/users') })
  const change = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Partial<User> & { password?: string } }) => api<User>(`/users/${id}`, { method: 'PATCH', body }),
    onSuccess: () => client.invalidateQueries({ queryKey: ['users'] }),
  })

  return (
    <>
      <PageHead title="Users" sub={<>Viewers {ROLE_HELP.viewer}; operators {ROLE_HELP.operator}; admins {ROLE_HELP.admin}.</>}>
        <AddUser />
      </PageHead>
      {change.error && <Failure error={change.error} />}
      {users.isPending ? <Loading /> : users.error ? <Failure error={users.error} /> : (
        <div className="tablebox">
          <table>
            <thead><tr><th>Email</th><th>Name</th><th>Role</th><th>Last signed in</th><th>Status</th><th><span className="visually-hidden">Actions</span></th></tr></thead>
            <tbody>
              {users.data.users.map((user) => (
                <tr key={user.id}>
                  <td>{user.email}{user.id === me.id && <span className="small"> (you)</span>}</td>
                  <td>{user.name || <span className="muted">—</span>}</td>
                  <td>
                    <label className="visually-hidden" htmlFor={`role-${user.id}`}>Role of {user.email}</label>
                    <select id={`role-${user.id}`} value={user.role} onChange={(event) => change.mutate({ id: user.id, body: { role: event.target.value as Role } })}>
                      {ROLES.map((role) => <option key={role}>{role}</option>)}
                    </select>
                  </td>
                  <td>{ago(user.lastLoginAt)}</td>
                  <td>{user.active ? <Pill kind="ok">Active</Pill> : <Pill kind="idle">Deactivated</Pill>}</td>
                  <td>
                    <button type="button" className={`btn sm ${user.active ? 'danger' : ''}`} onClick={() => change.mutate({ id: user.id, body: { active: !user.active } })}>
                      {user.active ? 'Deactivate' : 'Reactivate'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

function AddUser() {
  const dialog = useRef<HTMLDialogElement>(null)
  const client = useQueryClient()
  const [form, setForm] = useState({ email: '', name: '', role: 'viewer' as Role, password: '' })
  const add = useMutation({
    mutationFn: () => api<User>('/users', { method: 'POST', body: form }),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ['users'] })
      setForm({ email: '', name: '', role: 'viewer', password: '' })
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
          <label className="field">Starting password, at least 12 characters; tell them to change it
            <input type="password" autoComplete="new-password" required minLength={12} value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} />
          </label>
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
