import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, can, type Launched, type Run } from '../api'
import { useUser } from '../App'
import { Failure, Loading, PageHead, StartRunButton } from '../components/ui'
import { ago } from '../format'
import { RunTable } from './Overview'

interface RunsData {
  historyKept: boolean
  runs: Run[]
  launched: Launched[]
}

export function Runs() {
  const user = useUser()
  const [status, setStatus] = useState('')
  const [startedBy, setStartedBy] = useState('')
  const runs = useQuery({ queryKey: ['runs'], queryFn: () => api<RunsData>('/runs'), refetchInterval: 3000 })

  const shown = useMemo(() => (runs.data?.runs ?? []).filter((run) =>
    (!status || (status === 'failed' ? ['failed', 'partial', 'stopped'].includes(run.status) : run.status === status))
    && (!startedBy || (startedBy === 'schedule' ? !run.startedBy : !!run.startedBy))), [runs.data, status, startedBy])

  return (
    <>
      <PageHead title="Runs" sub="Every run, from the schedule or from Studio, as run history records it. Running ones update as they go.">
        {can(user, 'operator') && <StartRunButton />}
      </PageHead>

      {runs.isPending ? <Loading /> : runs.error ? <Failure error={runs.error} /> : (
        <>
          {!runs.data.historyKept && (
            <div className="banner warn" role="status">Run history isn't kept: add <code>history:</code> to jobs.yaml to see finished runs here.</div>
          )}
          {runs.data.launched.filter((launched) => !launched.runId).map((launched) => (
            <div className="banner info" key={launched.id} role="status">
              <span>{launched.finishedAt ? 'A run started' : 'Starting a run'} from Studio by {launched.email} {ago(launched.startedAt)}
                {launched.finishedAt ? ` ended before its first cycle (exit ${launched.exitCode ?? 'unknown'}).` : '…'}</span>
              <Link to={`/launched/${launched.id}`}>See its log</Link>
            </div>
          ))}
          <form className="card row" style={{ alignItems: 'flex-end' }} aria-label="Filter runs" onSubmit={(event) => event.preventDefault()}>
            <label className="field">Status
              <select value={status} onChange={(event) => setStatus(event.target.value)}>
                <option value="">Any status</option>
                <option value="running">Running</option>
                <option value="succeeded">Succeeded</option>
                <option value="failed">Failed or stopped</option>
              </select>
            </label>
            <label className="field">Started by
              <select value={startedBy} onChange={(event) => setStartedBy(event.target.value)}>
                <option value="">Anyone</option>
                <option value="schedule">The schedule</option>
                <option value="studio">Someone in Studio</option>
              </select>
            </label>
            {(status || startedBy) && <button type="button" className="btn" onClick={() => { setStatus(''); setStartedBy('') }}>Clear</button>}
          </form>
          <RunTable runs={shown} />
          <p className="muted">Showing {shown.length} of the {runs.data.runs.length} most recent runs.</p>
        </>
      )}
    </>
  )
}
