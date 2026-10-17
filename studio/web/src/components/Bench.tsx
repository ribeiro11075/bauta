import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, can } from '../api'
import { useUser } from '../App'
import { Pill } from './ui'
import { ago, count, duration } from '../format'

export interface BenchCheck {
  id: number
  at: string
  by: string
  job: string
  rows: number
  status: 'running' | 'done' | 'failed'
  problem?: string
  report?: { masker: string; jobs: { job: string; rows: number; complete: boolean; connectSeconds: number; readSeconds?: number; maskSeconds?: number;
    throttledSeconds?: number; readRowsPerSecond?: number | null; maskRowsPerSecond?: number | null; pace?: string | null; error: string | null }[] }
}

function rate(value: number | null | undefined): string {
  return value ? `${count(Math.round(value))} rows/s` : '—'
}

// Measuring a job reads up to `rows` of its query from the real source and
// masks them, as a run would, writing nothing: how fast reading and masking
// go, and which would set a run's pace.
export function BenchCard({ job, bench }: { job: string; bench: BenchCheck | null }) {
  const user = useUser()
  const client = useQueryClient()
  const [rows, setRows] = useState('100000')
  const start = useMutation({
    mutationFn: () => api(`/jobs/${job}/bench`, { method: 'POST', body: { rows: Number(rows) } }),
    onSuccess: () => client.invalidateQueries({ queryKey: ['job', job] }),
  })
  const measured = bench?.report?.jobs[0]

  return (
    <section className="card stack">
      <h2 style={{ margin: 0 }}>Speed against the source</h2>
      <p className="small">Reads up to the rows asked for from the real source and masks them as a run would, writing nothing. Writing isn't measured: the target's own speed is in each run's stage bar.</p>
      {bench && (
        bench.status === 'running' ? <div className="banner info" role="status">Measuring {count(bench.rows)} rows, started {ago(bench.at)} by {bench.by}…</div>
          : bench.status === 'failed' || measured?.error ? <div className="banner bad" role="alert">Could not measure: {bench.problem ?? measured?.error}</div>
            : measured && (
              <div className="stats">
                <div><div className="label">Rows read</div><div className="big mono">{count(measured.rows)}</div><div className="small">{measured.complete ? 'all the query returns' : 'a sample'}</div></div>
                <div><div className="label">Reading</div><div className="big">{rate(measured.readRowsPerSecond)}</div><div className="small">{duration(measured.readSeconds)} busy</div></div>
                <div><div className="label">Masking</div><div className="big">{rate(measured.maskRowsPerSecond)}</div><div className="small">{duration(measured.maskSeconds)} busy · {bench.report?.masker}</div></div>
                <div><div className="label">Sets the pace</div><div className="big">{measured.pace ?? '—'}</div><div className="small">measured {ago(bench.at)} by {bench.by}</div></div>
              </div>
            )
      )}
      {measured?.throttledSeconds ? <p className="small"><Pill kind="warn">Read limit</Pill> waited {duration(measured.throttledSeconds)} on the source's read limit.</p> : null}
      {can(user, 'operator') && (
        <form className="row" style={{ alignItems: 'flex-end' }} onSubmit={(event) => { event.preventDefault(); start.mutate() }}>
          <label className="field">Rows to read<input type="number" min="1" max="10000000" value={rows} onChange={(event) => setRows(event.target.value)} /></label>
          <button type="submit" className="btn" disabled={start.isPending || bench?.status === 'running'}>Measure</button>
          {start.error && <span className="error" role="alert">{start.error.message}</span>}
        </form>
      )}
    </section>
  )
}
