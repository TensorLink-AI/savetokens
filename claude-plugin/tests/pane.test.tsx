import { expect, test } from 'claude-code/testing'

const NOW = 1_790_000_000
const SNAP = {
  now: NOW,
  headline: { level: 'bad', text: "At this pace you'll run out of your weekly limit around Wed 05:33. Pausing synth would get you to the reset." }, source: 'ephemeris', forecast_made_at: NOW - 600, synced_at: null,
  limits: [
    { name: 'five_hour', label: '5-hour limit', used: 10, resets: NOW + 3600, p10: 11, p50: 15, p90: 23, p_hit: 0, eta: null, stage: null },
    { name: 'seven_day', label: 'weekly limit', used: 80, resets: NOW + 86400, p10: 95, p50: 120, p90: 140, p_hit: 0.9, eta: NOW + 7200, stage: 'act' },
  ],
  demand: { start: NOW - 4 * 3600, past: [0, 1, 2, 4], next: [3, 2, 1], next_hi: [5, 4, 2] },
  models: [{ model: 'claude-opus-5-5', share: 0.98, subagents: 0.27 }],
  machines: [], accounts: [], alerts: [], hits: [],
  sessions: [
    { session: 'e8aadbbb', project: 'synth', share: 0.58, pct_week: 5.3, pace: 0.7, model: 'claude-opus-5-5', subagents: 0.7, running: true,
      if_stopped: { limit: 'seven_day', adds: 3.5, eta: NOW + 7200, eta_if_stopped: null, hours: 5 } },
    { session: null, project: '3 more', share: 0.1, pct_week: 0.9, running: false },
  ],
}

test('the pane draws each limit and when you would run out', async ($, on) => {
  const ran: string[][] = []
  on('ui.open', async () => ({ value: { isPlaced: true as const } }))
  on('process.run', async (_$, e) => {
    ran.push([...e.argv])
    return { value: { exitCode: 0, stdout: JSON.stringify(SNAP), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  await $.command.run({ command: 'savetokens', args: '' } as never)
  expect(ran.at(-1)?.slice(-2)).toEqual(['dashboard', '--json'])
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({
      plugin: 'savetokens', surface, component: 'Pane', requestId: 'savetokens',
      props: { title: 'savetokens', isFocused: false } as never,
    })
    expect(await ui.find({ type: 'Text', text: /⚠ At this pace you'll run out of your weekly limit/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /likely 120%/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /MODELS .*opus-5-5 98%/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /synth .*5\.3%.*0\.7%/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /1 running/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /out (\w+ )?\d\d:\d\d → after reset/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /project .*today .*last hr .*if you pause it/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^┌─+┐$/ })).toBeDefined()           // the chart's frame
    expect(await ui.find({ type: 'Text', text: /^└─+┘$/ })).toBeDefined()
    await ui.unmount()
  }
})

test('the chart frame fits the pane body and its sides line up', async ($, on) => {
  on('ui.open', async () => ({ value: { isPlaced: true as const } }))
  on('process.run', async () => ({ value: { exitCode: 0, stdout: JSON.stringify({ ...SNAP, demand: {
    start: NOW - 24 * 3600, past: Array.from({ length: 24 }, (_, i) => i % 5), next: Array.from({ length: 24 }, () => 1),
    next_hi: Array.from({ length: 24 }, () => 3) } }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
  await $.command.run({ command: 'savetokens', args: '' } as never)
  for (const bodyColumns of [40, 64, 120]) {
    const ui = await $.ui.mount({
      plugin: 'savetokens', surface: 'terminal', component: 'Pane', requestId: 'savetokens',
      props: { title: 'savetokens', isFocused: false, bodyColumns } as never,
    })
    const top = (await ui.find({ type: 'Text', text: /^┌─+┐$/ }))?.text ?? ''
    const bottom = (await ui.find({ type: 'Text', text: /^└─+┘$/ }))?.text ?? ''
    expect(top.length).toBeGreaterThan(10)
    expect(top.length).toBeLessThanOrEqual(bodyColumns)
    expect(bottom.length).toBe(top.length)
    await ui.unmount()
  }
})

test('a failing command shows why instead of a blank pane', async ($, on) => {
  on('ui.open', async () => ({ value: { isPlaced: true as const } }))
  on('process.run', async () => ({ value: { exitCode: 1, stdout: '', stderr: 'savetokens: command not found', isStdoutTruncated: false, isStderrTruncated: false } }))
  await $.command.run({ command: 'savetokens', args: '' } as never)
  const ui = await $.ui.mount({
    plugin: 'savetokens', surface: 'terminal', component: 'Pane', requestId: 'savetokens',
    props: { title: 'savetokens', isFocused: false } as never,
  })
  expect(await ui.find({ type: 'Text', text: /command not found/ })).toBeDefined()
})

test('Codex and an API budget get their own rows and dollars', async ($, on) => {
  const snap = {
    ...SNAP,
    limits: [...SNAP.limits,
      { name: 'seven_day', label: 'Codex weekly limit', short: 'Codex wk', pool: 'codex', kind: 'subscription', used: 10,
        resets: NOW + 86400, p10: 30, p50: 60, p90: 90, p_hit: 0.1, eta: null, stage: null },
      { name: 'budget', label: 'Claude Code API budget', short: 'Claude $', pool: 'claude-code:api', kind: 'api', used: 62,
        spent_usd: 124, budget_usd: 200, per: 'a month', resets: NOW + 9 * 86400, p10: 80, p50: 95, p90: 130,
        p_hit: 0.3, eta: null, stage: null }],
    pools: [{ id: 'claude-code', tool: 'Claude Code', kind: 'subscription' }, { id: 'codex', tool: 'Codex', kind: 'subscription' }],
    models: [...SNAP.models, { model: 'gpt-6-astra', harness: 'codex', share: 1, subagents: 0 }],
    sessions: [...SNAP.sessions.slice(0, 1).map(x => ({ ...x, pool: 'claude-code', harness: 'claude-code' })),
      { session: 'c0ffee00', project: 'api', harness: 'codex', pool: 'codex', share: 1, pct_week: 4.1, pace: 0.4, running: true }],
  }
  on('ui.open', async () => ({ value: { isPlaced: true as const } }))
  on('process.run', async () => ({ value: { exitCode: 0, stdout: JSON.stringify(snap), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
  await $.command.run({ command: 'savetokens', args: '' } as never)
  const ui = await $.ui.mount({ plugin: 'savetokens', surface: 'terminal', component: 'Pane', requestId: 'savetokens',
    props: { title: 'savetokens', isFocused: false, bodyColumns: 80 } as never })
  expect(await ui.find({ type: 'Text', text: /Codex wk .*10%/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /\$124 of \$200 a month .*ends/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /MODELS Codex .*gpt-6-astra 100%/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /cx api/ })).toBeDefined()
  await ui.unmount()
})
