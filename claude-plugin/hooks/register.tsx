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
        {s.demand.past.length > 0 && (
          <Box flexDirection="column">
            <Text bold>usage per hour</Text>
            <Text wrap="truncate-end">  last  {spark(s.demand.past.slice(-n), top)}</Text>
            {s.demand.next.length > 0 && (
              <Text wrap="truncate-end">
                {'  next  '}
                <Text color="cyan">{spark(s.demand.next.slice(0, n), top)}</Text>
              </Text>
            )}
          </Box>
        )}
        <Text> </Text>
        {s.models.slice(0, 3).map(m => (
          <Text wrap="truncate-end">
            {m.model.replace('claude-', '')} {Math.round(m.share * 100)}%
            <Text dimColor>{m.subagents >= 0.01 ? ` (${Math.round(m.subagents * 100)}% subagents)` : ''}</Text>
          </Text>
        ))}
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
