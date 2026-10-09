"""What can run out: one pool per tool and way of paying.

  subscription  Claude Code or Codex on a plan: its 5-hour and weekly limits, read from the
                tool's own meter (the Claude Code statusline, Codex's session logs)
  api           Claude Code or Codex on an API key: a dollar budget per day, week or month,
                set with `savetokens api`, against the API price of what was used

Each pool has its own demand series, forecast paths and alerts. A subscription's demand is
in % of its weekly limit an hour; an API pool's is in dollars an hour.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import meter

TOOLS = {"claude-code": "Claude Code", "codex": "Codex"}
PERIODS = ("day", "week", "month")
ACTIVE_SECONDS = 8 * 86400   # a plan whose meter was read in this time still has a pool


@dataclass
class Pool:
    id: str              # "claude-code", "codex", "claude-code:api", "codex:api"
    harness: str
    kind: str            # "subscription" | "api"
    key: str | None      # forecast paths, ledger series and alerts: the account for Claude's plan, else the id
    budget: dict | None = None

    @property
    def unit(self):
        return "usd" if self.kind == "api" else "pct_of_weekly_limit"

    @property
    def tool(self):
        return TOOLS.get(self.harness, self.harness)


def budgets(store) -> dict:
    """{harness: {"usd", "period", "tz"}}: API budgets, kept in the store so the server has them too."""
    return store.meta("budgets") or {}


def set_budget(store, harness, usd, period, tz=None):
    b = budgets(store)
    if usd is None:
        b.pop(harness, None)
    else:
        if period not in PERIODS:
            raise ValueError(f"period is one of {', '.join(PERIODS)}")
        tz = time.localtime().tm_gmtoff if tz is None else tz
        b[harness] = {"usd": float(usd), "period": period, "tz": int(tz)}
    store.set_meta("budgets", b)
    store.set_meta("budgets_at", time.time())   # the newest setting wins across machines
    return b


def pools(store, now=None) -> list[Pool]:
    """Every pool, Claude Code's plan first."""
    now = now or time.time()
    out = []
    for h in sorted(meter.harnesses(store, since=now - ACTIVE_SECONDS), key=lambda h: h != meter.CLAUDE):
        out.append(Pool(h, h, "subscription", meter.active_account(store, now, h) if h == meter.CLAUDE else h))
    for h, b in sorted(budgets(store).items(), key=lambda kv: kv[0] != meter.CLAUDE):
        out.append(Pool(f"{h}:api", h, "api", f"{h}:api", b))
    return out


def history(store, pool: Pool, now, hours=meter.HISTORY_HOURS):
    """(hourly demand [(hour, value)], % per weight unit or None) for a pool."""
    if pool.kind == "api":
        return meter.spend(store, now, hours, pool.harness), None
    return meter.demand(store, now, hours, pool.harness)


def period(budget, now):
    """(start, end) of the budget period holding `now`, in the time zone the budget was set in."""
    tz = timezone(timedelta(seconds=budget.get("tz", 0)))
    d = datetime.fromtimestamp(now, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    kind = budget["period"]
    if kind == "day":
        start, end = d, d + timedelta(days=1)
    elif kind == "week":   # Monday to Monday
        start = d - timedelta(days=d.weekday())
        end = start + timedelta(days=7)
    else:
        start = d.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
    return start.timestamp(), end.timestamp()


def spent(store, pool: Pool, since, until):
    return store.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE harness = ? AND billing = 'api'"
                              " AND ts >= ? AND ts <= ?", (pool.harness, since, until)).fetchone()[0]


def unpriced(store, harness, since):
    """Models used on an API key with no known price (their usage can't count against a budget)."""
    return [r[0] for r in store.conn.execute(
        "SELECT DISTINCT model FROM usage WHERE harness = ? AND billing = 'api' AND cost_usd IS NULL AND ts >= ?"
        " AND model IS NOT NULL", (harness, since))]
