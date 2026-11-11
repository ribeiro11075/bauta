import { describe, expect, it, vi } from 'vitest'
import { ago, count, duration, plural, seconds, when } from './format'

describe('how values read', () => {
  it('shows a dash for what is missing', () => {
    expect(when(null)).toBe('—')
    expect(count(undefined)).toBe('—')
    expect(duration(null)).toBe('—')
    expect(ago(null)).toBe('—')
  })

  it('says durations at the scale they are', () => {
    expect(duration(0.4)).toBe('under 1s')
    expect(duration(4.25)).toBe('4.3s')
    expect(duration(42.4)).toBe('42s')
    expect(duration(125)).toBe('2m 5s')
    expect(duration(7500)).toBe('2h 5m')
  })

  it('keeps small numbers exact on an axis, where prose says under 1s', () => {
    expect(seconds(0.25)).toBe('0.25s')
    expect(seconds(90)).toBe('1m 30s')
  })

  it('counts back from now', () => {
    vi.spyOn(Date, 'now').mockReturnValue(new Date('2026-10-16T12:00:00Z').getTime())
    expect(ago('2026-10-16T11:59:30Z')).toBe('just now')
    expect(ago('2026-10-16T11:30:00Z')).toBe('30 min ago')
    expect(ago('2026-10-16T09:00:00Z')).toBe('3 h ago')
    expect(ago('2026-10-14T12:00:00Z')).toBe('2 d ago')
  })

  it('makes a noun plural unless there is one', () => {
    expect(plural(1, 'job')).toBe('1 job')
    expect(plural(2, 'job')).toBe('2 jobs')
  })
})
