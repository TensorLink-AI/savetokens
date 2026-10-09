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

// Hourly usage as bars in a plain frame: past solid; the forecast from the ┊, ▒ to likely, ░ to the high end.
// Returns the frame's top and bottom, and each row as [past, forecast] so the forecast can be coloured.
const chartRows = (d: Snapshot['demand'], width: number, height = 6) => {
  const room = Math.max(12, width - 3)   // the frame's two sides and the ┊ divider
  let past = d.past, next = d.next, hi = d.next_hi ?? d.next
  if (past.length + next.length > room) {
    const keepNext = Math.min(next.length, Math.floor(room / 2))
    past = past.slice(-(room - keepNext))
    next = next.slice(0, keepNext)
    hi = hi.slice(0, keepNext)
  }
  const hours = past.length + next.length
  const cell = 2 * hours <= room ? 2 : 1
  const top = niceTop(Math.max(0.01, ...past, ...hi))
  const glyph = (v: number, r: number, h?: number): string => {
    const level = (v / top) * height
    if (h !== undefined) {   // forecast: one joined column of whole cells, ▒ to the likely value, ░ to the high end
      const high = Math.max(level, (h / top) * height)
      return r < Math.round(level) ? '▒' : r < Math.round(high) ? '░' : r === 0 && level > 0 ? '▁' : ' '
    }
    if (level >= r + 1) return '█'
    if (level > r) return BARS[Math.max(1, Math.floor((level - r) * 8))] ?? '▁'
    return ' '
  }
  const rows: [string, string][] = []
  for (let r = height - 1; r >= 0; r--) {
    rows.push([past.map(v => glyph(v, r).repeat(cell)).join(''),
               (next.length ? '┊' : '') + next.map((v, i) => glyph(v, r, hi[i]).repeat(cell)).join('')])
  }
  const inner = hours * cell + (next.length ? 1 : 0)
  return { rows, top: '┌' + '─'.repeat(inner) + '┐', bottom: '└' + '─'.repeat(inner) + '┘' }
}

// What stopping a running session would change: the run-out time with and without it, or what it saves.
const ifStopped = (x: NonNullable<Snapshot['sessions']>[number]) => {
  const w = x.if_stopped
  if (!x.running) return 'not running'
  if (!w) return 'no forecast yet'
  if (w.eta !== null) return `out ${clock(w.eta)} → ${w.eta_if_stopped !== null ? clock(w.eta_if_stopped) : 'after reset'}`
  return `saves ${w.adds.toFixed(1)}%`
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
    // the pane's own body width, not the terminal's: a chart sized to the terminal wraps inside a docked pane
    const cols = e.props.bodyColumns || e.viewport?.columns || 60
    if (!s) {
      return (
        <Box flexDirection="column">
          <Text dimColor>{err ? `savetokens: ${err}` : 'Loading…'}</Text>
        </Box>
      )
    }
    const head = s.headline ?? { level: 'none', text: '' }
    const headColor = head.level === 'bad' ? 'red' : head.level === 'warn' ? 'yellow' : head.level === 'ok' ? 'green' : undefined
    const bw = Math.max(10, Math.min(30, cols - 34))
    const sessions = s.sessions ?? []
    const ch = chartRows(s.demand, cols)
    const used = s.demand.past.reduce((a, b) => a + b, 0)
    const ahead = s.demand.next.reduce((a, b) => a + b, 0)
    return (
      <Box flexDirection="column">
        <Text color={headColor} bold wrap="wrap">
          {head.level === 'ok' ? '✓' : head.level === 'none' ? '·' : '⚠'} {head.text}
        </Text>
        <Text dimColor wrap="truncate-end">
          {s.source === 'ephemeris' ? 'Ephemeris' : s.source ?? 'no'} forecast
          {s.forecast_made_at ? `, ${span(s.now - s.forecast_made_at)} ago` : ''}
          {s.synced_at ? ' · synced' : ''}
          {s.sync_error ? ' · server unreachable' : ''}
        </Text>
        <Text> </Text>

        {s.limits.length > 0 && <Text bold>LIMITS</Text>}
        {s.limits.map(l => (
          <Box flexDirection="column">
            <Text wrap="truncate-end">
              {'  ' + (l.name === 'five_hour' ? '5-hour' : 'weekly').padEnd(7)}
              <Text color={l.stage ? STAGE_COLOR[l.stage] : 'green'}>{bar(l, bw)}</Text>
              <Text bold> {`${Math.round(l.used)}%`.padStart(4)}</Text>
              <Text dimColor> now</Text>
            </Text>
            <Text dimColor wrap="truncate-end">
              {'         '}
              {l.p50 !== null ? `likely ${Math.round(l.p50)}% (${Math.round(l.p10 ?? 0)}–${Math.round(l.p90 ?? 0)}%) · ` : ''}
              resets {clock(l.resets)}
            </Text>
          </Box>
        ))}
        {s.limits.length > 0 && <Text dimColor>{'         '}█ used ▒ likely by reset ░ could reach</Text>}
        <Text> </Text>

        {(s.demand.past.some(v => v > 0) || s.demand.next.length > 0) && (
          <Box flexDirection="column">
            <Text>
              <Text bold>USAGE PER HOUR </Text>
              <Text dimColor>last {s.demand.past.length}h ┊ next {s.demand.next.length}h</Text>
            </Text>
            <Text dimColor wrap="truncate-end">{ch.top}</Text>
            {ch.rows.map(([p, f]) => (
              <Text wrap="truncate-end">
                <Text dimColor>│</Text>
                {p}
                <Text color="cyan">{f}</Text>
                <Text dimColor>│</Text>
              </Text>
            ))}
            <Text dimColor wrap="truncate-end">{ch.bottom}</Text>
            <Text dimColor wrap="wrap">
              used {used.toFixed(1)}% of the week · ~{ahead.toFixed(1)}% to come · █ used ▒ likely ░ could reach
            </Text>
            <Text> </Text>
          </Box>
        )}

        {sessions.length > 0 && (
          <Box flexDirection="column">
            <Text>
              <Text bold>SESSIONS </Text>
              <Text dimColor>last 24h · {sessions.filter(x => x.running).length} running</Text>
            </Text>
            <Text dimColor wrap="truncate-end">
              {'  ' + 'project'.padEnd(15)}{'today'.padStart(6)}{'last hr'.padStart(9)}   if you pause it (5h)
            </Text>
            {sessions.map(x => (
              <Text wrap="truncate-end" dimColor={!x.running}>
                <Text color={x.running ? 'green' : undefined}>{x.running ? '●' : '○'}</Text>
                {' ' + (x.project ?? x.session ?? '?').slice(0, 14).padEnd(15)}
                {(x.pct_week !== null ? `${x.pct_week.toFixed(1)}%` : '–').padStart(6)}
                {(x.running && x.pace ? `${x.pace.toFixed(1)}%` : '–').padStart(9)}
                {x.session ? '   ' + ifStopped(x) : ''}
              </Text>
            ))}
            <Text dimColor>today and last hr: % of your weekly limit</Text>
            <Text> </Text>
          </Box>
        )}

        {s.models.length > 0 && (
          <Text wrap="wrap">
            <Text bold>MODELS </Text>
            {s.models.slice(0, 3).map(m => `${m.model.replace('claude-', '')} ${Math.round(m.share * 100)}%`
              + (m.subagents >= 0.05 ? ` (${Math.round(m.subagents * 100)}% subagents)` : '')).join(' · ')}
          </Text>
        )}
        {s.machines.length > 1 && <Text dimColor>{s.machines.length} machines this week</Text>}
        {s.alerts.slice(0, 1).map(a => (
          <Text wrap="wrap">
            <Text bold>LATEST ALERT </Text>
            <Text dimColor>{clock(a.ts)} </Text>
            {a.message}
          </Text>
        ))}
        {err && <Text dimColor>last refresh failed: {err}</Text>}
      </Box>
    )
  })
}
