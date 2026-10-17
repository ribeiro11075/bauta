import { useQuery } from '@tanstack/react-query'
import { api, type Activity } from '../api'
import { Failure, Loading, PageHead } from '../components/ui'
import { whenExactly } from '../format'

export function ActivityPage() {
  const activity = useQuery({ queryKey: ['activity'], queryFn: () => api<{ activity: Activity[] }>('/activity') })

  return (
    <>
      <PageHead title="Activity" sub="Everything done through Studio, and who did it: sign-ins, runs started, checks, edits with their diff, and changes to users." />
      {activity.isPending ? <Loading /> : activity.error ? <Failure error={activity.error} /> : (
        <div className="tablebox">
          <table>
            <thead><tr><th>When</th><th>Who</th><th>What</th><th>Detail</th></tr></thead>
            <tbody>
              {activity.data.activity.map((entry) => (
                <tr key={entry.id}>
                  <td style={{ whiteSpace: 'nowrap' }}>{whenExactly(entry.at)}</td>
                  <td>{entry.email || <span className="muted">command line</span>}</td>
                  <td>{entry.action}{entry.target && <> <span className="mono">{entry.target}</span></>}</td>
                  <td className="wrap">
                    {typeof entry.detail.diff === 'string' ? (
                      <details><summary>Diff</summary><pre className="log" style={{ marginTop: 8 }}>{entry.detail.diff}</pre></details>
                    ) : <span className="small mono">{Object.entries(entry.detail).map(([key, value]) => `${key}: ${JSON.stringify(value)}`).join(' · ')}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!activity.data.activity.length && <div className="empty">Nothing yet.</div>}
        </div>
      )}
    </>
  )
}
