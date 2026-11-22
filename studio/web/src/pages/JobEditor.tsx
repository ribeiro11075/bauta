// The job editor: a job as a form, for editing one or adding a new one. Only
// what someone changes is sent, so a setting left alone stays as the file
// writes it, or inherits from defaults: as before.

import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, type FormColumn, type JobFormData, type Written } from '../api'
import { DiffView, Failure, Loading, PageHead } from '../components/ui'

type Values = Record<string, unknown>

// NEW_JOB: the form a new job starts from: masked, with nothing set yet, and in
//   jobs.yaml where the server names no file to add it to.
const NEW_JOB: JobFormData = {
  name: '', file: 'jobs.yaml', digest: '', fields: {}, inherited: {},
  masking: { enabled: true, columns: [], defaultStrategy: null, customDefault: false, key: { source: 'none' } },
  choices: { strategies: [], connections: [], jobs: [], insertStrategies: ['swap', 'upsert', 'append', 'overwrite'] }, others: [],
}

/** The editor for the job the route names, or for a new one at /jobs/new. */
export function JobEditor() {
  const { name } = useParams()
  const creating = name === undefined
  const form = useQuery({ queryKey: ['form', name], queryFn: () => api<JobFormData>(`/jobs/${name}/form`), enabled: !creating, refetchOnWindowFocus: false })
  const choices = useQuery({ queryKey: ['form-choices'], queryFn: () => api<JobFormData['choices'] & { files: string[]; inherited: Record<string, unknown> }>('/form-choices'), enabled: creating })

  const crumb = <><Link to="/jobs">Jobs</Link> / {creating ? 'new' : <><Link to={`/jobs/${name}`}>{name}</Link> / edit</>}</>
  const title = creating ? 'New job' : 'Edit job'
  const pending = creating ? choices.isPending : form.isPending
  const error = creating ? choices.error : form.error
  if (pending) return <><PageHead title={title} crumb={crumb} /><Loading /></>
  if (error) return <><PageHead title={title} crumb={crumb} /><Failure error={error} /></>

  const data = creating ? { ...NEW_JOB, file: choices.data!.files[0] ?? 'jobs.yaml', choices: choices.data!, inherited: choices.data!.inherited } : form.data!
  return <EditorForm key={creating ? 'new' : data.name} data={data} creating={creating} files={creating ? choices.data!.files : [data.file]} crumb={crumb} />
}

/**
 * The form itself. `digest` is the file's version as last read or written: a
 * save answers with the new one, so a second save needn't wait for the form to
 * be read again.
 */
function EditorForm({ data, creating, files, crumb }: { data: JobFormData; creating: boolean; files: string[]; crumb: ReactNode }) {
  const navigate = useNavigate()
  const client = useQueryClient()
  const [changes, setChanges] = useState<Values>({})
  const [newName, setNewName] = useState('')
  const [file, setFile] = useState(data.file)
  const [masking, setMasking] = useState(data.masking)
  const [maskingChanged, setMaskingChanged] = useState(creating)
  const [keyVariable, setKeyVariable] = useState('MASKING_KEY')
  const [written, setWritten] = useState<Written | null>(null)
  const [digest, setDigest] = useState(data.digest)
  useEffect(() => { setMasking(data.masking) }, [data.masking])
  useEffect(() => { setDigest(data.digest) }, [data.digest])

  /** A field as the form shows it: changed, else as the file has it. */
  const value = (field: string): unknown => (field in changes ? changes[field] : data.fields[field])
  /** Changes one field. */
  const set = (field: string, next: unknown) => { setChanges({ ...changes, [field]: next }); setWritten(null) }
  /** Changes the masking section, which is then sent whole. */
  const changeMasking = (next: typeof masking) => { setMasking(next); setMaskingChanged(true); setWritten(null) }

  const body = useMemo(() => ({
    digest, changes,
    masking: maskingChanged ? { enabled: masking.enabled, defaultStrategy: masking.defaultStrategy, customDefault: masking.customDefault, keyVariable,
      columns: masking.columns.map((column) => ({ column: column.column, strategy: column.strategy, custom: column.custom })) } : undefined,
  }), [digest, changes, masking, maskingChanged, keyVariable])

  const save = useMutation({
    mutationFn: () => creating
      ? api<Written>('/jobs', { method: 'POST', body: { ...body, name: newName, file } })
      : api<Written>(`/jobs/${data.name}/form`, { method: 'PUT', body }),
    onSuccess: (result) => {
      client.invalidateQueries()
      if (creating) navigate(`/jobs/${newName}`)
      else { setWritten(result); setDigest(result.digest); setChanges({}); setMaskingChanged(false) }
    },
  })
  const dirty = creating || Object.keys(changes).length > 0 || maskingChanged

  return (
    <>
      <PageHead title={creating ? 'New job' : <>Edit <span className="mono">{data.name}</span></>} crumb={crumb}
        sub={<>Written into <code>{creating ? file : data.file}</code>; only what you change. Saved only if the whole configuration still validates, kept, and recorded with who made it.</>}>
        {!creating && <Link className="btn" to={`/configuration?file=${encodeURIComponent(data.file)}`}>Edit as text</Link>}
      </PageHead>

      {written && <div className="banner ok" role="status"><div><strong>Saved.</strong> The next run uses it.<DiffView diff={written.diff} /></div></div>}
      {save.error && <Failure error={save.error} />}

      <form className="stack" style={{ gap: 16 }} onSubmit={(event) => { event.preventDefault(); save.mutate() }}>
        {creating && (
          <section className="card two">
            <label className="field">Name<input required pattern="[A-Za-z_][A-Za-z0-9_.\-]*" value={newName} onChange={(event) => setNewName(event.target.value)} /></label>
            <label className="field">In file
              <select value={file} onChange={(event) => setFile(event.target.value)}>{files.map((path) => <option key={path}>{path}</option>)}</select>
            </label>
          </section>
        )}

        <section className="card stack">
          <h2 style={{ margin: 0 }}>Where from, and where to</h2>
          <div className="two">
            <Select label="Source connection" field="sourceConnection" options={data.choices.connections} value={value} set={set} inherited={data.inherited} required={creating} />
            <Select label="Target connection" field="targetConnection" options={data.choices.connections} value={value} set={set} inherited={data.inherited} required={creating} />
          </div>
          <label className="field">Source query
            <textarea className="code" style={{ minHeight: 120 }} required={creating} value={String(value('sourceQuery') ?? '')} onChange={(event) => set('sourceQuery', event.target.value)} />
          </label>
          <div className="two">
            <Text label="Target table" field="targetTableFinal" value={value} set={set} inherited={data.inherited} required={creating} />
            <Text label="Stage table, for swap and full refresh" field="targetTableStage" value={value} set={set} inherited={data.inherited} />
          </div>
        </section>

        <section className="card stack">
          <h2 style={{ margin: 0 }}>How it loads</h2>
          <div className="two">
            <Select label="Loads by" field="insertStrategy" options={data.choices.insertStrategies} value={value} set={set} inherited={data.inherited} required={creating} />
            <Text label="Incremental on (watermark column)" field="watermarkColumn" value={value} set={set} inherited={data.inherited} />
            <Text label="First watermark" field="watermarkInitial" value={value} set={set} inherited={data.inherited} />
            <Text label="Refresh, minutes between runs" field="refresh" type="number" value={value} set={set} inherited={data.inherited} />
            <Text label="Rows per chunk" field="chunkSize" type="number" value={value} set={set} inherited={data.inherited} />
            <Text label="Retries" field="retries" type="number" value={value} set={set} inherited={data.inherited} />
            <Text label="Timeout, seconds" field="timeoutSeconds" type="number" value={value} set={set} inherited={data.inherited} />
          </div>
          <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
            <legend className="small">Waits for</legend>
            <div className="row">
              {data.choices.jobs.map((job) => {
                const current = (value('predecessors') as string[] | undefined) ?? []
                return (
                  <label className="check" key={job}>
                    <input type="checkbox" checked={current.includes(job)} onChange={() => set('predecessors', current.includes(job) ? current.filter((name) => name !== job) : [...current, job])} />
                    <span className="mono">{job}</span>
                  </label>
                )
              })}
            </div>
          </fieldset>
          <Flag label="Active: runs when due" field="active" value={value} set={set} inherited={data.inherited} />
        </section>

        <MaskingSection masking={masking} change={changeMasking} strategies={data.choices.strategies} keyVariable={keyVariable} setKeyVariable={setKeyVariable}
          unmasked={value('unmasked') === true || (value('unmasked') === undefined && data.inherited.unmasked === true)} setUnmasked={(next) => set('unmasked', next)} />

        {data.others.length > 0 && <p className="small">Also set in the file, and left as they are: {data.others.join(', ')}. Change those in the text editor.</p>}
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <Link className="btn" to={creating ? '/jobs' : `/jobs/${data.name}`}>Cancel</Link>
          <button type="submit" className="btn primary" disabled={!dirty || save.isPending}>{save.isPending ? 'Validating…' : creating ? 'Validate and add' : 'Validate and save'}</button>
        </div>
      </form>
    </>
  )
}

interface FieldProps {
  label: string
  field: string
  value: (field: string) => unknown
  set: (field: string, value: unknown) => void
  inherited: Values
  required?: boolean
}

/** What a field inherits from `defaults:`, as a placeholder, or nothing. */
function inheritedText(inherited: Values, field: string): string {
  const found = inherited[field]
  return found === null || found === undefined || found === '' ? '' : `${String(found)} (inherited)`
}

/** A text or number field, showing what it inherits when left empty. */
function Text({ label, field, value, set, inherited, required, type = 'text' }: FieldProps & { type?: string }) {
  const current = value(field)
  return (
    <label className="field">{label}
      <input type={type} required={required} placeholder={inheritedText(inherited, field)} value={current === undefined || current === null ? '' : String(current)}
        onChange={(event) => set(field, event.target.value === '' ? null : event.target.value)} />
    </label>
  )
}

/**
 * A choice of `options`, required only where nothing would be inherited:
 * `defaults:` may name it.
 */
function Select({ label, field, options, value, set, inherited, required }: FieldProps & { options: string[] }) {
  const current = value(field)
  const needed = required && !(field in inherited)
  return (
    <label className="field">{label}
      <select required={needed} value={current === undefined || current === null ? '' : String(current)} onChange={(event) => set(field, event.target.value || null)}>
        <option value="">{inheritedText(inherited, field) || (needed ? 'Choose…' : '—')}</option>
        {options.map((option) => <option key={option}>{option}</option>)}
      </select>
    </label>
  )
}

/** A checkbox, ticked by what it inherits when the job doesn't say. */
function Flag({ label, field, value, set, inherited }: FieldProps) {
  const current = value(field)
  const checked = current === undefined || current === null ? inherited[field] === true : current === true
  return <label className="check"><input type="checkbox" checked={checked} onChange={(event) => set(field, event.target.checked)} />{label}</label>
}

/**
 * The job's masking: whether it masks, the key's source, each column's strategy
 * and the default for the rest, or that it copies unmasked.
 */
function MaskingSection({ masking, change, strategies, keyVariable, setKeyVariable, unmasked, setUnmasked }: {
  masking: JobFormData['masking']; change: (next: JobFormData['masking']) => void; strategies: string[]
  keyVariable: string; setKeyVariable: (next: string) => void; unmasked: boolean; setUnmasked: (next: boolean) => void
}) {
  const columns = masking.columns
  /** Replaces the column policies. */
  const setColumns = (next: FormColumn[]) => change({ ...masking, columns: next })
  const keyText = { defaults: 'from defaults: in the jobs file', variable: `from $${masking.key.variable ?? ''}`, file: 'written in the file (not shown)', none: '' }[masking.key.source]

  return (
    <section className="card stack">
      <h2 style={{ margin: 0 }}>Masking</h2>
      <label className="check"><input type="checkbox" checked={masking.enabled} onChange={(event) => change({ ...masking, enabled: event.target.checked })} />Mask this job's columns</label>
      {!masking.enabled ? (
        <label className="check"><input type="checkbox" checked={unmasked} onChange={(event) => setUnmasked(event.target.checked)} />
          Declared: copies as it stands, reviewed (<code>unmasked: true</code>). Without it, audit reports the job.</label>
      ) : (
        <>
          {masking.key.source === 'none' ? (
            <label className="field" style={{ maxWidth: 320 }}>Key, from the environment variable
              <input required pattern="[A-Za-z_][A-Za-z0-9_]*" value={keyVariable} onChange={(event) => setKeyVariable(event.target.value)} />
            </label>
          ) : <p className="small">Key {keyText}. Its value never reaches this page.</p>}
          <div className="tablebox">
            <table>
              <thead><tr><th>Column</th><th>Strategy</th><th><span className="visually-hidden">Remove</span></th></tr></thead>
              <tbody>
                {columns.map((column, index) => (
                  <tr key={index}>
                    <td><input aria-label="Column" value={column.column} onChange={(event) => setColumns(columns.map((entry, at) => (at === index ? { ...entry, column: event.target.value } : entry)))} /></td>
                    <td>{column.custom ? <span className="small">{column.summary} with options; change it as text</span> : (
                      <select aria-label={`Strategy for ${column.column}`} required value={column.strategy ?? ''}
                        onChange={(event) => setColumns(columns.map((entry, at) => (at === index ? { ...entry, strategy: event.target.value } : entry)))}>
                        <option value="">Choose…</option>
                        {strategies.map((strategy) => <option key={strategy}>{strategy}</option>)}
                      </select>
                    )}</td>
                    <td><button type="button" className="btn sm" onClick={() => setColumns(columns.filter((_, at) => at !== index))}>Remove</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="row">
            <button type="button" className="btn sm" onClick={() => setColumns([...columns, { column: '', strategy: '', custom: false }])}>Add a column</button>
            {!masking.customDefault && (
              <label className="field">Any other column
                <select value={masking.defaultStrategy ?? ''} onChange={(event) => change({ ...masking, defaultStrategy: event.target.value || null })}>
                  <option value="">none: every column must be listed</option>
                  {strategies.map((strategy) => <option key={strategy}>{strategy}</option>)}
                </select>
              </label>
            )}
          </div>
        </>
      )}
    </section>
  )
}
