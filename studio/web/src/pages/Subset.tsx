import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, type Written } from '../api'
import { DiffView, Failure, PageHead, Pill } from '../components/ui'
import { count } from '../format'

interface Choices {
  connections: string[]
  files: string[]
}

interface Preview {
  mask: boolean
  text: string
  jobs: string[]
  tables: { table: string; job: string; parents: string[]; rows: number | null; targetExists: boolean; clashes: boolean; loadedBy: string[] }[]
  ignored: string[]
  createMissing: string | null
}

export function Subset() {
  const client = useQueryClient()
  const choices = useQuery({ queryKey: ['form-choices'], queryFn: () => api<Choices>('/form-choices') })
  const [request, setRequest] = useState({ connection: '', target: '', root: '', where: '', noChildren: false, ignore: '', mask: true })
  const [file, setFile] = useState('')
  const [added, setAdded] = useState<(Written & { jobs: string[] }) | null>(null)
  const tables = useQuery({
    queryKey: ['subset-tables', request.connection],
    queryFn: () => api<{ tables: string[] }>('/subset/tables', { query: { connection: request.connection } }),
    enabled: !!request.connection,
  })
  const body = (extra: Record<string, unknown> = {}) => ({
    ...request, ignore: request.ignore.split(/[\s,]+/).filter(Boolean), ...extra,
  })
  const preview = useMutation({ mutationFn: (counting: boolean) => api<Preview>('/subset/preview', { method: 'POST', body: body({ count: counting }) }),
    onSuccess: () => setAdded(null) })
  const apply = useMutation({
    mutationFn: () => api<Written & { jobs: string[] }>('/subset/apply', { method: 'POST', body: body({ file: file || choices.data?.files[0] }) }),
    onSuccess: (result) => { setAdded(result); client.invalidateQueries({ predicate: (query) => query.queryKey[0] !== 'form-choices' }) },
  })
  const set = (name: string, value: unknown) => { setRequest({ ...request, [name]: value }); preview.reset() }
  const ready = request.connection && request.target && request.root && request.where.trim()
  const result = preview.data
  const clashes = result?.tables.filter((entry) => entry.clashes) ?? []

  return (
    <>
      <PageHead title="Subset builder" sub="A slice of a source that keeps every relationship: the rows of one table you filter, the rows they reference, and the rows that reference them. Previewed first, then added as jobs that load parents before children." />

      <form className="card stack" onSubmit={(event) => { event.preventDefault(); preview.mutate(false) }}>
        <div className="two">
          <label className="field">From
            <select required value={request.connection} onChange={(event) => setRequest({ ...request, connection: event.target.value, root: '' })}>
              <option value="">Choose a source…</option>
              {choices.data?.connections.map((alias) => <option key={alias}>{alias}</option>)}
            </select>
          </label>
          <label className="field">Into
            <select required value={request.target} onChange={(event) => set('target', event.target.value)}>
              <option value="">Choose a target…</option>
              {choices.data?.connections.filter((alias) => alias !== request.connection).map((alias) => <option key={alias}>{alias}</option>)}
            </select>
          </label>
          <label className="field">Starting from the table
            <select required value={request.root} disabled={!tables.data} onChange={(event) => set('root', event.target.value)}>
              <option value="">{tables.isFetching ? 'Reading its tables…' : 'Choose a table…'}</option>
              {tables.data?.tables.map((table) => <option key={table}>{table}</option>)}
            </select>
          </label>
          <label className="field">Its rows where (SQL, run against the source as written)
            <input className="mono" required placeholder="created_at >= '2026-01-01'" value={request.where} onChange={(event) => set('where', event.target.value)} />
          </label>
        </div>
        {tables.error && <Failure error={tables.error} />}
        <div className="row">
          <label className="check"><input type="checkbox" checked={!request.noChildren} onChange={(event) => set('noChildren', !event.target.checked)} />Also copy the rows that reference them</label>
          <label className="check"><input type="checkbox" checked={request.mask} onChange={(event) => set('mask', event.target.checked)} />Propose a masking policy for each table</label>
        </div>
        <details>
          <summary className="small">Foreign keys to ignore, to break a cycle</summary>
          <label className="field" style={{ marginTop: 8 }}>table.column, separated by spaces or commas; the ignored column must be nullable or masked
            <input className="mono" value={request.ignore} onChange={(event) => set('ignore', event.target.value)} />
          </label>
        </details>
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" className="btn" disabled={!ready || preview.isPending} onClick={() => preview.mutate(true)}>Preview and count rows</button>
          <button type="submit" className="btn primary" disabled={!ready || preview.isPending}>{preview.isPending ? 'Planning…' : 'Preview'}</button>
        </div>
      </form>

      {preview.error && <Failure error={preview.error} />}
      {result && (
        <section className="stack">
          <h2 style={{ margin: 0 }}>{result.tables.length} tables, in load order</h2>
          {result.ignored.length > 0 && <div className="banner warn">Ignoring {result.ignored.join(', ')}: rows may reference ones outside the subset.</div>}
          <div className="tablebox">
            <table>
              <thead><tr><th>Table</th><th>Waits for</th><th className="num">Rows</th><th>In the target</th><th>Job</th></tr></thead>
              <tbody>
                {result.tables.map((entry) => (
                  <tr key={entry.table}>
                    <td className="mono">{entry.table}</td>
                    <td className="mono small">{entry.parents.join(', ') || '—'}</td>
                    <td className="num mono">{entry.rows === null ? <span className="muted">not counted</span> : count(entry.rows)}</td>
                    <td>{entry.targetExists ? <Pill kind="ok">There</Pill> : <Pill kind="warn">Missing</Pill>}</td>
                    <td className="mono">{entry.job}{entry.clashes && <> <Pill kind="bad">name taken</Pill></>}
                      {entry.loadedBy.length > 0 && <div className="small error" style={{ fontFamily: 'var(--sans)' }}>already loaded by {entry.loadedBy.join(', ')}</div>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {result.tables.some((entry) => entry.loadedBy.length) && (
            <div className="banner bad">Jobs already load some of these tables into {request.target}. Two jobs loading one table overwrite or duplicate each other: remove or retarget the existing ones, or choose another target.</div>
          )}
          {result.createMissing && (
            <div className="banner warn"><div>The target lacks some of these tables. Create them before the first run, on the server:<pre className="mono" style={{ margin: '6px 0 0', whiteSpace: 'pre-wrap' }}>{result.createMissing}</pre></div></div>
          )}
          <details><summary>The jobs, as they will be written</summary><pre className="log" style={{ marginTop: 8 }}>{result.text}</pre></details>
          {!result.mask && <div className="banner bad">These jobs copy every column as it stands. Propose a policy, or add one to each job, before running them.</div>}
          {added ? (
            <div className="banner ok" role="status"><div><strong>Added {added.jobs.length} jobs</strong> to <code>{added.path}</code>: {added.jobs.map((job) => <Link key={job} className="mono" to={`/jobs/${job}`} style={{ marginRight: 8 }}>{job}</Link>)}
              Review each policy before the first run.<DiffView diff={added.diff} /></div></div>
          ) : (
            <div className="row" style={{ justifyContent: 'flex-end' }}>
              <label className="field">Into the file
                <select value={file || choices.data?.files[0] || ''} onChange={(event) => setFile(event.target.value)}>
                  {choices.data?.files.map((path) => <option key={path}>{path}</option>)}
                </select>
              </label>
              <button type="button" className="btn primary" disabled={clashes.length > 0 || apply.isPending} onClick={() => apply.mutate()}>
                {apply.isPending ? 'Validating…' : `Add ${result.tables.length} jobs`}
              </button>
            </div>
          )}
          {apply.error && <Failure error={apply.error} />}
        </section>
      )}
    </>
  )
}
