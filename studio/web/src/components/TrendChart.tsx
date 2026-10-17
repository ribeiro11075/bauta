import { useId, useState } from 'react'
import type { JobRecord } from '../api'
import { count, seconds, whenExactly } from '../format'

// One measure of a job's recent runs, a column per run, oldest on the left.
// Rows and duration are drawn as two charts, never one with two scales. A
// run that failed or was skipped has no measure: it is marked at the
// baseline in the status colour, with its word in the tooltip and the key.

const WIDTH = 560
const HEIGHT = 160
const PAD = { top: 12, right: 8, bottom: 22, left: 56 }
const BAR_MAX = 24
const GAP = 2

function niceCeiling(value: number): number {
  if (value <= 0) return 1
  const power = 10 ** Math.floor(Math.log10(value))
  const step = [1, 2, 2.5, 5, 10].find((factor) => factor * power >= value) ?? 10
  return step * power
}

interface Props {
  title: string
  records: JobRecord[]
  measure: (record: JobRecord) => number | null
  format: (value: number) => string
}

export function TrendChart({ title, records, measure, format }: Props) {
  const runs = [...records].reverse()
  const [hover, setHover] = useState<number | null>(null)
  const titleId = useId()
  if (!runs.length) return null

  const values = runs.map((record) => (record.status === 'completed' ? measure(record) : null))
  const top = niceCeiling(Math.max(0, ...values.map((value) => value ?? 0)))
  const plotWidth = WIDTH - PAD.left - PAD.right
  const plotHeight = HEIGHT - PAD.top - PAD.bottom
  const slot = plotWidth / runs.length
  const bar = Math.max(2, Math.min(BAR_MAX, slot - GAP))
  const y = (value: number) => PAD.top + plotHeight - (value / top) * plotHeight
  const ticks = [0, top / 2, top]
  const shown = hover !== null ? runs[hover] : null

  return (
    <figure className="trend" aria-labelledby={titleId}>
      <figcaption id={titleId} className="label">{title}, last {runs.length} runs</figcaption>
      <div className="trend-plot">
        <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={`${title} per run, oldest to newest`} onMouseLeave={() => setHover(null)}>
          {ticks.map((tick) => (
            <g key={tick}>
              <line className="grid" x1={PAD.left} x2={WIDTH - PAD.right} y1={y(tick)} y2={y(tick)} />
              <text className="tick" x={PAD.left - 8} y={y(tick)} dy="0.32em" textAnchor="end">{format(tick)}</text>
            </g>
          ))}
          {runs.map((record, index) => {
            const value = values[index]
            const x = PAD.left + index * slot + (slot - bar) / 2
            const failed = record.status !== 'completed'
            const height = value === null ? 0 : Math.max(value > 0 ? 2 : 0, plotHeight - (y(value) - PAD.top))
            const radius = Math.min(4, bar / 2, height)
            return (
              <g key={`${record.runId}-${index}`}>
                {!failed && height > 0 && (
                  <path className={`series${hover === index ? ' on' : ''}`}
                    d={`M${x},${PAD.top + plotHeight} V${PAD.top + plotHeight - height + radius} Q${x},${PAD.top + plotHeight - height} ${x + radius},${PAD.top + plotHeight - height}
                       H${x + bar - radius} Q${x + bar},${PAD.top + plotHeight - height} ${x + bar},${PAD.top + plotHeight - height + radius} V${PAD.top + plotHeight} Z`} />
                )}
                {failed && <text className="failed-mark" x={x + bar / 2} y={PAD.top + plotHeight - 4} textAnchor="middle">×</text>}
                <rect className="hit" x={PAD.left + index * slot} y={PAD.top} width={slot} height={plotHeight} tabIndex={0}
                  aria-label={`${whenExactly(record.finishedAt)}: ${failed ? record.status : format(value ?? 0)}`}
                  onMouseEnter={() => setHover(index)} onFocus={() => setHover(index)} onBlur={() => setHover(null)} />
              </g>
            )
          })}
          <line className="axis" x1={PAD.left} x2={WIDTH - PAD.right} y1={PAD.top + plotHeight} y2={PAD.top + plotHeight} />
          <text className="tick" x={PAD.left} y={HEIGHT - 4}>older</text>
          <text className="tick" x={WIDTH - PAD.right} y={HEIGHT - 4} textAnchor="end">latest</text>
        </svg>
        {shown && hover !== null && (
          <div className="tooltip" role="status" style={{ left: `${((PAD.left + (hover + 0.5) * slot) / WIDTH) * 100}%` }}>
            <strong>{whenExactly(shown.finishedAt)}</strong>
            <span>{shown.status === 'completed' ? format(values[hover] ?? 0) : shown.status === 'failed' ? 'Failed' : 'Skipped'}</span>
          </div>
        )}
      </div>
    </figure>
  )
}

export function JobTrends({ records }: { records: JobRecord[] }) {
  if (!records.length) return null
  const failed = records.some((record) => record.status !== 'completed')
  return (
    <section className="card">
      <h2>Trend</h2>
      <div className="two">
        <TrendChart title="Rows" records={records} measure={(record) => record.rowCount} format={(value) => count(Math.round(value))} />
        <TrendChart title="Duration" records={records} measure={(record) => record.durationSeconds ?? null} format={seconds} />
      </div>
      {failed && <p className="small" style={{ marginTop: 8 }}><span className="failed-key" aria-hidden="true">×</span> a run that failed or was skipped, and so has no measure</p>}
    </section>
  )
}
