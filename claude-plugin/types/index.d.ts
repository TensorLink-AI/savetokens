export type Limit = {
  name: string           // five_hour | seven_day | budget
  label: string
  short?: string         // "5-hour", "weekly", "Codex wk", "Claude $"
  pool?: string          // claude-code | codex | claude-code:api | codex:api
  kind?: 'subscription' | 'api'
  spent_usd?: number
  budget_usd?: number
  per?: string
  used: number
  resets: number
  p10: number | null
  p50: number | null
  p90: number | null
  p_hit: number | null
  eta: number | null
  stage: 'heads_up' | 'act' | 'last_call' | null
}

export type Snapshot = {
  now: number
  headline?: { level: 'bad' | 'warn' | 'ok' | 'none'; text: string }
  source: string | null
  forecast_made_at: number | null
  synced_at: number | null
  sync_error?: string
  limits: Limit[]
  demand: { start: number; past: number[]; next: number[]; next_hi?: number[]; next_lo?: number[]
            pool?: string | null; unit?: '%' | '$'; label?: string | null }
  pools?: { id: string; tool: string; kind: string }[]
  models: { model: string; harness?: string; share: number; subagents: number }[]
  machines: { machine: string; last_seen: number; requests: number }[]
  sessions?: {
    session: string | null
    project: string | null
    harness?: string
    pool?: string
    share: number | null
    pct_week: number | null
    pace?: number | null
    model?: string | null
    subagents?: number
    running: boolean
    if_stopped?: { limit: string; short?: string; adds: number; eta: number | null; eta_if_stopped: number | null
                   hours: number }
  }[]
  accounts: { account: string | null; weekly: number | null; active: boolean }[]
  alerts: { ts: number; message: string }[]
  hits: { ts: number; kind: string; model: string | null }[]
}

declare module 'claude-code' {
  interface PluginState {
    savetokens: { snap: Snapshot | null; error: string | null }
  }
}
