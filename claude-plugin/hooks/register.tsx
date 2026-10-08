import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Limit, Snapshot } from '../types'

// The savetokens command and its environment. The data comes from `savetokens dashboard --json`,
// which asks your savetokens server when this machine is connected to one.
const ARGV = ['savetokens', 'dashboard', '--json']
const ENV: Record<string, string> | undefined = undefined
const PANE = 'savetokens'
const EVERY_MS = 30_000
const BARS = ' ▁▂▃▄▅▆▇█'

const snap = atom({ plugin: 'savetokens', key: 'snap' } as const, null)
const error = atom({ plugin: 'savetokens', key: 'error' } as const, null)

const clock = (ts: number) => {
  const d = new Date(ts * 1000)
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  const today = new Date().toDateString() === d.toDateString()
  return today ? hm : `${['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getDay()]} ${hm}`
}

const span = (s: number) =>
  s < 3600 ? `${Math.round(s / 60)} min` : s < 172800 ? `${(s / 3600).toFixed(1)} h` : `${(s / 86400).toFixed(1)} days`

const spark = (values: number[], top: number) =>
  values.map(v => (v > 0 ? BARS[Math.min(8, Math.round((8 * v) / (top || 1)))] : BARS[0])).join('')

const bar = (l: Limit, width: number) => {
  let out = ''
  for (let i = 0; i < width; i++) {
    const x = (100 * (i + 0.5)) / width
    out += x <= l.used ? '█' : l.p50 !== null && x <= l.p50 ? '▒' : l.p90 !== null && x <= l.p90 ? '░' : '·'
  }
  return out
}

const niceTop = (v: number) => {
  if (v <= 0) return 1
  const k = 10 ** Math.floor(Math.log10(v))
  return [1, 2, 2.5, 5, 10].map(m => m * k).find(x => x >= v) ?? 10 * k
}

// Hourly usage as bars with a y-axis and hour ticks: past solid, forecast cyan, its range to p90 shaded.
// Returns rows of [axis, past, forecast] so the pane can colour the forecast part.
const chartRows = (d: Snapshot['demand'], width: number, height = 6) => {
  // Fit the plot: trim hours (older past, later forecast) rather than cut the axis off.
  const room = Math.max(12, width - 10)
  let past = d.past, next = d.next, hi = d.next_hi ?? d.next, start = d.start
  if (past.length + next.length > room) {
    const keepNext = Math.min(next.length, Math.floor(room / 2))
    const keepPast = room - keepNext
    start += (past.length - Math.min(past.length, keepPast)) * 3600
    past = past.slice(-keepPast)
    next = next.slice(0, keepNext)
    hi = hi.slice(0, keepNext)
  }
  const hours = past.length + next.length
  const cell = 2 * hours <= room ? 2 : 1
  const top = niceTop(Math.max(0.01, ...past, ...hi))
  const label = (v: number) => (top < 10 ? `${v.toFixed(1)}%` : `${Math.round(v)}%`).padStart(6)
  const glyph = (v: number, r: number, h?: number): string => {
    const level = (v / top) * height
    if (level >= r + 1) return '█'
    if (level > r) return BARS[Math.max(1, Math.floor((level - r) * 8))] ?? '▁'
    if (h !== undefined && (h / top) * height > r) return '░'
    return ' '
  }
  const rows: [string, string, string][] = []
  for (let r = height - 1; r >= 0; r--) {
    const y = r === height - 1 ? label(top) : r === Math.floor(height / 2) - 1 ? label((top * Math.floor(height / 2)) / height) : ''
    const axis = y ? `${y} ┤` : `${''.padStart(6)} │`
    rows.push([axis, past.map(v => glyph(v, r).repeat(cell)).join(''),
               '┊' + next.map((v, i) => glyph(v, r, hi[i]).repeat(cell)).join('')])
  }
  // x-axis: a tick every 6 hours with its hour written under it, never overlapping the last label
  let axis = `${label(0)} └`
  const ticks = Array.from({ length: 8 + hours * cell + 2 }, () => ' ')
  let free = 0
  for (let i = 0; i < hours; i++) {
    const hour = new Date((start + i * 3600) * 1000).getHours()
    if (i === past.length) axis += '┴'
    const mark = hour % 6 === 0
    axis += (mark ? '┬' : '─') + '─'.repeat(cell - 1)
    const col = 8 + i * cell + (i >= past.length ? 1 : 0)
    if (mark && col >= free) {
      const text = String(hour).padStart(2, '0')
      for (let k = 0; k < text.length; k++) ticks[col + k] = text[k] ?? ' '
      free = col + text.length + 1
    }
  }
  return { rows, axis, ticks: ticks.join('').trimEnd() }
}

const STAGE: Record<string, string> = { heads_up: 'heads-up', act: 'act now', last_call: 'last call' }
const STAGE_COLOR: Record<string, string> = { heads_up: 'yellow', act: 'red', last_call: 'red' }

async function refresh($: EngineInterface) {
  try {
    const r = await $.process.run(ARGV, { env: ENV, timeoutMs: 25_000 })
    if (r.exitCode !== 0) throw new Error(r.stderr.trim().split('\n').pop() || `exit ${r.exitCode}`)
    const parsed = JSON.parse(r.stdout) as Snapshot
    await update($, snap, () => parsed)
    await update($, error, () => null)
  } catch (err) {
    await update($, error, () => String((err as Error).message ?? err).slice(0, 200))
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'savetokens', description: 'Live pane: your Claude limits and when you would run out' })
    void refresh($)
    $.clock.every(EVERY_MS, () => refresh($))
    return next(e)
  })

  on('command.run', { command: 'savetokens' }, async $ => {
    await refresh($)
    await $.ui.open({ id: PANE, title: 'savetokens' })
    return { text: 'savetokens pane opened; it refreshes every 30 seconds.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const s = await read($, snap)
    const err = await read($, error)
    const width = Math.max(20, (e.viewport?.columns ?? 60) - 22)
    if (!s) {
      return (
        <Box flexDirection="column">
          <Text dimColor>{err ? `savetokens: ${err}` : 'Loading…'}</Text>
        </Box>
      )
    }
    const top = Math.max(0.01, ...s.demand.past, ...s.demand.next)
    const n = Math.max(6, Math.min(24, width))
    return (
      <Box flexDirection="column">
        <Text dimColor wrap="truncate-end">
          forecast: {s.source ?? 'none yet'}
          {s.forecast_made_at ? `, ${span(s.now - s.forecast_made_at)} ago` : ''}
          {s.synced_at ? ' · synced' : ''}
          {s.sync_error ? ' · server unreachable' : ''}
        </Text>
        <Text> </Text>
        {s.limits.length === 0 && <Text dimColor>No limit readings yet: send a message in Claude Code.</Text>}
        {s.limits.map(l => (
          <Box flexDirection="column">
            <Text wrap="truncate-end">
              <Text bold>{l.name === 'five_hour' ? '5-hour ' : 'weekly '}</Text>
              <Text color={l.stage ? STAGE_COLOR[l.stage] : 'green'}>{bar(l, width)}</Text>
              <Text> {Math.round(l.used)}%</Text>
            </Text>
            <Text dimColor wrap="truncate-end">
              {'        '}resets {clock(l.resets)}
              {l.p50 !== null ? ` · likely ${Math.round(l.p50)}% (${Math.round(l.p10 ?? 0)}–${Math.round(l.p90 ?? 0)}%)` : ''}
            </Text>
            {l.eta !== null && (
              <Text color={STAGE_COLOR[l.stage ?? 'act']} wrap="truncate-end">
                {'        '}⚠ {STAGE[l.stage ?? 'act']}: out ~{clock(l.eta)}, {span(l.resets - l.eta)} early
              </Text>
            )}
          </Box>
        ))}
        <Text> </Text>
        {(s.demand.past.some(v => v > 0) || s.demand.next.length > 0) && (() => {
          const ch = chartRows(s.demand, (e.viewport?.columns ?? 80) - 4)
          const used = s.demand.past.reduce((a, b) => a + b, 0)
          const ahead = s.demand.next.reduce((a, b) => a + b, 0)
          return (
            <Box flexDirection="column">
              <Text>
                <Text bold>usage per hour </Text>
                <Text dimColor>(y: % of weekly limit per hour) · last {s.demand.past.length}h {used.toFixed(1)}% · next {s.demand.next.length}h ~{ahead.toFixed(1)}%</Text>
              </Text>
              {ch.rows.map(([axis, p, f]) => (
                <Text wrap="truncate-end">
                  <Text dimColor>{axis}</Text>
                  {p}
                  <Text color="cyan">{f}</Text>
                </Text>
              ))}
              <Text dimColor wrap="truncate-end">{ch.axis}</Text>
              <Text dimColor wrap="truncate-end">{ch.ticks}  hour</Text>
              <Text dimColor wrap="truncate-end">{'        '}past ┊ forecast (shaded: its likely range)</Text>
            </Box>
          )
        })()}
        <Text> </Text>
        {s.models.slice(0, 3).map(m => (
          <Text wrap="truncate-end">
            {m.model.replace('claude-', '')} {Math.round(m.share * 100)}%
            <Text dimColor>{m.subagents >= 0.01 ? ` (${Math.round(m.subagents * 100)}% subagents)` : ''}</Text>
          </Text>
        ))}
        {(s.sessions ?? []).length > 0 && (
          <Box flexDirection="column">
            <Text> </Text>
            <Text bold>
              sessions, last 24h <Text dimColor>({(s.sessions ?? []).filter(x => x.running).length} running)</Text>
            </Text>
            <Text dimColor wrap="truncate-end">
              {'  ' + 'project'.padEnd(14)} {'share'.padStart(5)} {'of week'.padStart(8)} {'last hr'.padStart(8)}  session
            </Text>
            {(s.sessions ?? []).map(x => (
              <Text wrap="truncate-end">
                <Text color={x.running ? 'green' : undefined} dimColor={!x.running}>{x.running ? '●' : '○'}</Text>
                {' ' + (x.project ?? '?').slice(0, 14).padEnd(14)} {`${Math.round(x.share * 100)}%`.padStart(5)}
                {' ' + (x.pct_week !== null ? `${x.pct_week.toFixed(1)}%` : '–').padStart(8)}
                <Text color={x.running && x.pace ? 'yellow' : undefined}>
                  {' ' + (x.pace ? `${x.pace.toFixed(1)}%` : '–').padStart(8)}
                </Text>
                <Text dimColor>{x.session ? `  ${x.session}` : ''}</Text>
              </Text>
            ))}
            <Text dimColor wrap="wrap">of week and last hr: % of your weekly limit</Text>
          </Box>
        )}
        {s.machines.length > 1 && <Text dimColor>{s.machines.length} machines this week</Text>}
        {s.alerts.slice(0, 2).map(a => (
          <Text color="yellow" wrap="wrap">
            {clock(a.ts)} {a.message}
          </Text>
        ))}
        {err && <Text dimColor>last refresh failed: {err}</Text>}
      </Box>
    )
  })
}
