// How times, durations and counts read on every screen. Times are shown in
// the viewer's own time zone; history records them in UTC.

const dateTime = new Intl.DateTimeFormat(undefined, { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' })
const fullDateTime = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'medium' })
const number = new Intl.NumberFormat()

/** A time as a short date and time, or a dash for none. */
export function when(iso: string | null | undefined): string {
  return iso ? dateTime.format(new Date(iso)) : '—'
}

/** A time to the second, or a dash for none. */
export function whenExactly(iso: string | null | undefined): string {
  return iso ? fullDateTime.format(new Date(iso)) : '—'
}

/** A number with the viewer's digit grouping, or a dash for none. */
export function count(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : number.format(value)
}

/** Seconds at the scale they are: under 1s, 4.3s, 2m 5s, 2h 5m; a dash for none. */
export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 1) return 'under 1s'
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${Math.round(seconds % 60)}s`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

/** Seconds as a number, for an axis or a difference, where prose says "under 1s". */
export function seconds(value: number): string {
  return value < 60 ? `${Number(value.toFixed(value < 1 ? 2 : value < 10 ? 1 : 0))}s` : duration(value)
}

/** How long ago a time was, roughly: just now, minutes, hours or days. */
export function ago(iso: string | null | undefined): string {
  if (!iso) return '—'
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`
  return `${Math.floor(seconds / 86400)} d ago`
}

/** `value` and `noun`, plural unless the value is one. */
export function plural(value: number, noun: string): string {
  return `${number.format(value)} ${noun}${value === 1 ? '' : 's'}`
}
