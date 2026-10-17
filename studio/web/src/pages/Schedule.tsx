import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, can, type ScheduleEntry, type Staleness } from '../api'
import { useUser } from '../App'
import { Failure, Loading, PageHead, Pill } from '../components/ui'
import { ago, when } from '../format'

const STATES: Record<ScheduleEntry['state'], [string, string]> = {
  due: ['Due', 'run'],
  throttled: ['Waiting for its window', 'idle'],
  inactive: ['Inactive', 'idle'],
}

function refreshText(minutes: number | null): string {
  if (!minutes) return 'every run'
  if (minutes % 1440 === 0) return `every ${minutes / 1440} d`
  if (minutes % 60 === 0) return `every ${minutes / 60} h`
  return `every ${minutes} min`
}

export function Schedule() {
  const user = useUser()
  const schedule = useQuery({ queryKey: ['schedule'], queryFn: () => api<{ jobs: ScheduleEntry[]; staleness: Staleness }>('/schedule'), refetchInterval: 30000 })

  return (
    <>
      <PageHead title="Schedule" sub={<>Where each job stands for the next run cron starts, as <code>bauta jobs</code> sees it, and which have gone stale: an active job that hasn't completed within its hours.</>} />
      {schedule.isPending ? <Loading /> : schedule.error ? <Failure error={schedule.error} /> : (
        <>
          <div className="tablebox">
            <table>
              <thead><tr><th>Job</th><th>Now</th><th>Refresh</th><th>Last completed</th><th>Next due</th><th>Stale after</th><th>Freshness</th></tr></thead>
              <tbody>
                {schedule.data.jobs.map((entry) => (
                  <tr key={entry.job}>
                    <td><Link className="mono" to={`/jobs/${entry.job}`}>{entry.job}</Link>
                      {entry.waitsFor.length > 0 && <div className="small">after {entry.waitsFor.join(', ')}</div>}</td>
                    <td><Pill kind={STATES[entry.state][1]}>{STATES[entry.state][0]}</Pill></td>
                    <td>{refreshText(entry.refreshMinutes)}</td>
                    <td title={entry.lastCompletedAt ?? ''}>{entry.lastCompletedAt ? ago(entry.lastCompletedAt) : <span className="muted">never</span>}</td>
                    <td>{entry.state === 'inactive' ? '—' : entry.nextDueAt ? when(entry.nextDueAt) : 'next run'}</td>
                    <td>{entry.staleAfterHours} h{entry.ownThreshold && <span className="small"> (its own)</span>}</td>
                    <td>{entry.state === 'inactive' ? <span className="muted">—</span> : entry.stale
                      ? <Pill kind="bad">{entry.neverCompleted ? 'Never completed' : 'Stale'}</Pill> : <Pill kind="ok">Fresh</Pill>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="small">The schedule itself is cron's, or your orchestrator's: Studio shows what bauta will do when it runs, and starts runs only when someone asks.</p>
          {can(user, 'admin') && <StalenessForm current={schedule.data.staleness} jobs={schedule.data.jobs.map((entry) => entry.job)} />}
        </>
      )}
    </>
  )
}

function StalenessForm({ current, jobs }: { current: Staleness; jobs: string[] }) {
  const client = useQueryClient()
  const [defaultHours, setDefaultHours] = useState(String(current.defaultHours))
  const [own, setOwn] = useState<Record<string, string>>({})
  useEffect(() => {
    setDefaultHours(String(current.defaultHours))
    setOwn(Object.fromEntries(Object.entries(current.jobs).map(([job, hours]) => [job, String(hours)])))
  }, [current])
  const save = useMutation({
    mutationFn: () => api('/schedule/staleness', { method: 'PUT', body: {
      defaultHours: Number(defaultHours),
      jobs: Object.fromEntries(Object.entries(own).filter(([, hours]) => hours.trim()).map(([job, hours]) => [job, Number(hours)])),
    } }),
    onSuccess: () => client.invalidateQueries({ queryKey: ['schedule'] }),
  })

  return (
    <form className="card stack" onSubmit={(event) => { event.preventDefault(); save.mutate() }}>
      <h2 style={{ margin: 0 }}>When a job goes stale</h2>
      <label className="field" style={{ maxWidth: 260 }}>Every job, after this many hours without completing
        <input type="number" min="0.1" step="any" required value={defaultHours} onChange={(event) => setDefaultHours(event.target.value)} />
      </label>
      <details>
        <summary>A job's own threshold</summary>
        <div className="stats" style={{ marginTop: 10 }}>
          {jobs.map((job) => (
            <label className="field" key={job}><span className="mono">{job}</span>
              <input type="number" min="0.1" step="any" placeholder={`${defaultHours} (every job's)`} value={own[job] ?? ''}
                onChange={(event) => setOwn({ ...own, [job]: event.target.value })} />
            </label>
          ))}
        </div>
      </details>
      {save.error && <p className="error" role="alert">{save.error.message}</p>}
      <div className="row"><button type="submit" className="btn primary" disabled={save.isPending}>Save</button>{save.isSuccess && <span className="small" role="status">Saved.</span>}</div>
    </form>
  )
}
