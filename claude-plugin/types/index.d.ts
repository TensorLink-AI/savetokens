export type Limit = {
  name: string
  label: string
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
  source: string | null
  forecast_made_at: number | null
  synced_at: number | null
  sync_error?: string
  limits: Limit[]
  demand: { past: number[]; next: number[] }
  models: { model: string; share: number; subagents: number }[]
  machines: { machine: string; last_seen: number; requests: number }[]
  sessions?: {
    session: string | null
    project: string | null
    share: number
    pct_week: number | null
    pace?: number | null
    model?: string | null
    subagents?: number
    running: boolean
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
