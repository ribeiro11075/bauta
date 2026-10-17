import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { api, ApiError, can, type Job, type Launched, type Stages } from '../api'
import { useUser } from '../App'
import { duration } from '../format'

// Status pills ---------------------------------------------------------------

const RUN_STATUS: Record<string, [string, string]> = {
  running: ['Running', 'run'],
  succeeded: ['Succeeded', 'ok'],
  failed: ['Failed', 'bad'],
  partial: ['Some failed', 'bad'],
  stopped: ['Ended without finishing', 'warn'],
}

const JOB_STATUS: Record<string, [string, string]> = {
  completed: ['Completed', 'ok'],
  failed: ['Failed', 'bad'],
  skipped: ['Skipped', 'warn'],
  running: ['Running', 'run'],
  waiting: ['Waiting', 'idle'],
}

export function Pill({ kind, children }: { kind: string; children: ReactNode }) {
  return <span className={`pill ${kind}`}>{children}</span>
}

export function RunStatus({ status, counts }: { status: string; counts?: Record<string, number> }) {
  const [label, kind] = RUN_STATUS[status] ?? [status, 'idle']
  const failed = (counts?.failed ?? 0) + (counts?.skipped ?? 0)
  return <Pill kind={kind}>{status === 'partial' && failed ? `${failed} failed` : label}</Pill>
}

export function JobStatus({ status }: { status: string }) {
  const [label, kind] = JOB_STATUS[status] ?? [status, 'idle']
  return <Pill kind={kind}>{label}</Pill>
}

// Where the time went --------------------------------------------------------

const STAGE_LABELS: [keyof Stages, string, string][] = [
  ['read', 'Reading', 's-read'],
  ['mask', 'Masking', 's-mask'],
  ['write', 'Writing', 's-write'],
  ['wait', 'Waiting on read limit', 's-wait'],
]

// Stages are busy time and overlap, so the bar shows each stage's share of
// the busy time, not of the run's duration; the title says the seconds.
export function StageBar({ stages, label }: { stages: Stages | null; label: string }) {
  if (!stages) return <div className="bar" role="img" aria-label={`${label}: no stage times recorded`} />
  const total = stages.read + stages.mask + stages.write + stages.wait || 1
  const description = STAGE_LABELS.map(([key, name]) => `${name} ${duration(stages[key])}`).join(', ')
  return (
    <div className="bar" role="img" aria-label={`${label}: ${description}`} title={description}>
      {STAGE_LABELS.map(([key, , className]) => (
        <span key={key} className={className} style={{ width: `${(stages[key] / total) * 100}%` }} />
      ))}
    </div>
  )
}

export function StageKey() {
  return (
    <div className="row" style={{ gap: 16 }}>
      {STAGE_LABELS.map(([key, name, className]) => (
        <span className="key" key={key}><span className={`sw ${className}`} />{name}</span>
      ))}
    </div>
  )
}

export function jobStages(record: { readSeconds?: number | null; maskSeconds?: number | null; writeSeconds?: number | null; throttledSeconds?: number | null }): Stages | null {
  if (record.readSeconds == null) return null
  return { read: record.readSeconds ?? 0, mask: record.maskSeconds ?? 0, write: record.writeSeconds ?? 0, wait: record.throttledSeconds ?? 0 }
}

// Text from bauta, such as an audit finding, marks commands and settings
// with backticks; they read as code.
export function Prose({ text }: { text: string }) {
  return <>{text.split(/(`[^`]+`)/).map((part, index) => part.startsWith('`') && part.endsWith('`') && part.length > 1
    ? <code key={index}>{part.slice(1, -1)}</code> : part)}</>
}

// A unified diff, additions and removals marked by more than colour: each
// line keeps its own + or -.
export function DiffView({ diff }: { diff: string }) {
  return (
    <pre className="log diff" style={{ marginTop: 10 }}>
      {diff.split('\n').map((line, index) => (
        <div key={index} className={line.startsWith('+') && !line.startsWith('+++') ? 'add' : line.startsWith('-') && !line.startsWith('---') ? 'del' : ''}>{line || ' '}</div>
      ))}
    </pre>
  )
}

// Page furniture ---------------------------------------------------------------

export function PageHead({ title, sub, crumb, children }: { title: ReactNode; sub?: ReactNode; crumb?: ReactNode; children?: ReactNode }) {
  return (
    <div>
      {crumb && <div className="crumb">{crumb}</div>}
      <div className="head">
        <div>
          <h1>{title}</h1>
          {sub && <p className="sub">{sub}</p>}
        </div>
        {children && <div className="row">{children}</div>}
      </div>
    </div>
  )
}

export function Loading() {
  return <div className="empty" role="status">Loading…</div>
}

export function Failure({ error }: { error: unknown }) {
  if (error instanceof ApiError && error.status === 409) {
    return (
      <div className="banner bad" role="alert">
        <div><strong>The configuration doesn't load, so this can't be shown.</strong><pre className="error mono" style={{ margin: '8px 0 0' }}>{error.message}</pre></div>
      </div>
    )
  }
  return <div className="banner bad" role="alert">{error instanceof Error ? error.message : 'Something went wrong.'}</div>
}

// Starting a run ---------------------------------------------------------------

interface FullRefreshProblem {
  job: string
  message: string
  fix: { stageTable: string; command: string } | null
}

// What keeps a full refresh from replacing a job, and the way to fix it: in
// place for an admin, who can make the stage table and name it in the job;
// otherwise the command whoever runs the server can.
function FullRefreshFix({ problem }: { problem: FullRefreshProblem }) {
  const user = useUser()
  const client = useQueryClient()
  const create = useMutation({
    mutationFn: () => api<{ stageTable: string; created: boolean }>(`/jobs/${problem.job}/stage-table`, { method: 'POST' }),
    onSuccess: () => client.invalidateQueries(),
  })
  return (
    <div className="banner warn" style={{ display: 'block' }}>
      <div className="small"><Prose text={problem.message.replace(/ Set it, and create the stage table with .*$/, '')} /></div>
      {problem.fix && (can(user, 'admin') ? (
        <div className="row" style={{ marginTop: 8 }}>
          <button type="button" className="btn sm" disabled={create.isPending} onClick={() => create.mutate()}>
            {create.isPending ? 'Creating…' : `Create ${problem.fix.stageTable} and use it`}
          </button>
          <span className="small">Made from the target table itself, then set as <Link to={`/jobs/${problem.job}`} className="mono">{problem.job}</Link>'s stage table.</span>
          {create.error && <span className="error small" role="alert">{create.error.message}</span>}
        </div>
      ) : (
        <div className="small" style={{ marginTop: 6 }}>An admin can fix it here, or on the server: <code>{problem.fix.command}</code>, then set <code>targetTableStage: {problem.fix.stageTable}</code> in <Link to={`/jobs/${problem.job}`}>{problem.job}</Link>.</div>
      ))}
    </div>
  )
}

export function StartRunButton({ jobs, label = 'Start a run', primary = true }: { jobs?: string[]; label?: string; primary?: boolean }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [selected, setSelected] = useState<string[]>(jobs ?? [])
  const [fullRefresh, setFullRefresh] = useState(false)
  const navigate = useNavigate()
  const client = useQueryClient()
  const allJobs = useQuery({ queryKey: ['jobs'], queryFn: () => api<{ jobs: Job[] }>('/jobs'), enabled: jobs === undefined })
  const options = useQuery({ queryKey: ['run-options'], queryFn: () => api<{ fullRefresh: { possible: boolean; problems: FullRefreshProblem[] } }>('/run-options') })
  const fullRefreshPossible = options.data?.fullRefresh.possible ?? false
  const start = useMutation({
    mutationFn: () => api<Launched>('/runs', { method: 'POST', body: { jobs: selected, fullRefresh: fullRefresh && fullRefreshPossible } }),
    onSuccess: (launched) => {
      dialog.current?.close()
      client.invalidateQueries({ queryKey: ['runs'] })
      navigate(`/launched/${launched.id}`)
    },
  })

  useEffect(() => setSelected(jobs ?? []), [jobs])

  const toggle = (name: string) => setSelected((current) => (current.includes(name) ? current.filter((job) => job !== name) : [...current, name]))

  return (
    <>
      <button type="button" className={`btn ${primary ? 'primary' : ''}`} onClick={() => dialog.current?.showModal()}>{label}</button>
      <dialog ref={dialog} aria-labelledby="start-run-title">
        <form method="dialog" onSubmit={(event) => { event.preventDefault(); start.mutate() }}>
          <h2 id="start-run-title" style={{ margin: 0 }}>Start a run</h2>
          {jobs === undefined ? (
            <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
              <legend className="small">Jobs to run. None chosen runs every job that's due, as cron would; chosen ones run now, without their predecessors.</legend>
              <div className="checks">
                {(allJobs.data?.jobs ?? []).filter((job) => job.active).map((job) => (
                  <label className="check" key={job.name}>
                    <input type="checkbox" checked={selected.includes(job.name)} onChange={() => toggle(job.name)} />
                    <span className="mono">{job.name}</span>
                  </label>
                ))}
              </div>
            </fieldset>
          ) : (
            <p>Runs <span className="mono">{jobs.join(', ')}</span> now, without waiting for its refresh window or running its predecessors.</p>
          )}
          <label className="check">
            <input type="checkbox" checked={fullRefresh && fullRefreshPossible} disabled={!fullRefreshPossible} onChange={(event) => setFullRefresh(event.target.checked)} />
            Full refresh: reload incremental jobs whole, so rows deleted from the source leave the copy
          </label>
          {options.data && !fullRefreshPossible && (
            <div className="stack" style={{ gap: 8 }}>
              <span className="small">Not possible yet:</span>
              {options.data.fullRefresh.problems.map((problem) => <FullRefreshFix key={problem.job} problem={problem} />)}
            </div>
          )}
          {start.error && <p className="error" role="alert">{start.error.message}</p>}
          <div className="row" style={{ justifyContent: 'flex-end' }}>
            <button type="button" className="btn" onClick={() => dialog.current?.close()}>Cancel</button>
            <button type="submit" className="btn primary" disabled={start.isPending}>{start.isPending ? 'Starting…' : 'Start'}</button>
          </div>
        </form>
      </dialog>
    </>
  )
}
