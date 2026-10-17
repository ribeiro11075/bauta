// How times, durations and counts read on every screen. Times are shown in
// the viewer's own time zone; history records them in UTC.

const dateTime = new Intl.DateTimeFormat(undefined, { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' })
const fullDateTime = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'medium' })
const number = new Intl.NumberFormat()

export function when(iso: string | null | undefined): string {
  return iso ? dateTime.format(new Date(iso)) : '—'
}

export function whenExactly(iso: string | null | undefined): string {
  return iso ? fullDateTime.format(new Date(iso)) : '—'
}

export function count(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : number.format(value)
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 1) return 'under 1s'
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${Math.round(seconds % 60)}s`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

// Seconds as a number, for an axis or a difference, where prose says "under 1s".
export function seconds(value: number): string {
  return value < 60 ? `${Number(value.toFixed(value < 1 ? 2 : value < 10 ? 1 : 0))}s` : duration(value)
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return '—'
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`
  return `${Math.floor(seconds / 86400)} d ago`
}

export function plural(value: number, noun: string): string {
  return `${number.format(value)} ${noun}${value === 1 ? '' : 's'}`
}
