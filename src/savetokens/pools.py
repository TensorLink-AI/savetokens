"""What can run out: one pool per tool and way of paying.

  subscription  Claude Code or Codex on a plan: its 5-hour and weekly limits, read from the
                tool's own meter (the Claude Code statusline, Codex's session logs)
  api           Claude Code or Codex on an API key, or Hermes on any provider's API: a dollar
                budget per day, week or month, set with `savetokens api`, against the API
                price of what was used
  goal          a budget you set with `savetokens budget`: overall or for a project or task, in dollars or
                tokens, per day, week or month or a total by a date; it counts API spend only (real
                money) or all usage at API prices, from every provider and tool or the ones you name

Each pool has its own demand series, forecast paths and alerts. A subscription's demand is
in % of its weekly limit an hour; an API pool's is in dollars an hour.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import meter

TOOLS = {"claude-code": "Claude Code", "codex": "Codex", "hermes": "Hermes"}
PERIODS = ("day", "week", "month")
ACTIVE_SECONDS = 8 * 86400   # a plan whose meter was read in this time still has a pool


@dataclass
class Pool:
    id: str              # "claude-code", "codex", "claude-code:api", "codex:api", "hermes:api"
    harness: str
    kind: str            # "subscription" | "api"
    key: str | None      # forecast paths, ledger series and alerts: the account for Claude's plan, else the id
    budget: dict | None = None
    account: str | None = None   # a plan's own account, when several are in use (each has its own limits)
    n: int = 0                   # and which of them: 1 is the latest used

    @property
    def unit(self):
        if self.kind == "goal":
            return self.budget.get("unit", "usd")
        return "usd" if self.kind == "api" else "pct_of_weekly_limit"

    @property
    def tool(self):
        return TOOLS.get(self.harness, self.harness)

    @property
    def name(self):
        """The tool, and which account when several are in use: "Codex (account 2)"."""
        return f"{self.tool} (account {self.n})" if self.n else self.tool


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


def plans(store) -> dict:
    """{provider: {"usd", "period", "tz"}}: subscriptions, a fixed cost (Claude Max, ChatGPT Pro, a flat-rate API)."""
    return store.meta("plans") or {}


def set_plan(store, provider, usd, period, tz=None):
    p = plans(store)
    if usd is None:
        p.pop(provider, None)
    else:
        if period not in PERIODS:
            raise ValueError(f"period is one of {', '.join(PERIODS)}")
        tz = time.localtime().tm_gmtoff if tz is None else tz
        p[provider] = {"usd": float(usd), "period": period, "tz": int(tz)}
    store.set_meta("plans", p)
    store.set_meta("budgets_at", time.time())   # synced with the budgets
    return p


def pools(store, now=None, goals=False) -> list[Pool]:
    """Every pool, Claude Code's plan first; with goals=True, the budgets you set too (`savetokens budget`)."""
    now = now or time.time()
    out = []
    for h in sorted(meter.harnesses(store, since=now - ACTIVE_SECONDS), key=lambda h: h != meter.CLAUDE):
        accts = meter.recent_accounts(store, now - ACTIVE_SECONDS, h)
        if len(accts) < 2:   # one account: every reading is its
            out.append(Pool(h, h, "subscription", meter.active_account(store, now, h) if h == meter.CLAUDE else h))
            continue
        for i, a in enumerate(accts):   # several: each its own limits, forecast and alerts
            out.append(Pool(h if i == 0 else f"{h}:{a[:6]}", h, "subscription",
                            a if h == meter.CLAUDE else f"{h}:{a}", account=a, n=i + 1))
    for h, b in sorted(budgets(store).items(), key=lambda kv: kv[0] != meter.CLAUDE):
        out.append(Pool(f"{h}:api", h, "api", f"{h}:api", b))
    if goals:
        out += [Pool(f"goal:{gid}", "all", "goal", f"goal:{gid}", g) for gid, g in sorted(goals_(store).items())]
    return out


# ── budgets you set: overall or for projects, in $ or tokens ──────────────────

UNITS = {"usd": "COALESCE(cost_usd, 0)", "tokens": "input + cache_read + cache_write_5m + cache_write_1h + output"}


def goals_(store) -> dict:
    """{id: {"label", "scope": {"projects"|"tasks": [ids], "providers": [..], "tools": [..]}, "count": api|all,
    "unit": usd|tokens, "amount", "period" | "by", "start", "tz"}}. Kept in the store, so the hub has them too."""
    return store.meta("goals") or {}


def set_goal(store, gid, goal):
    g = goals_(store)
    g[gid] = goal
    store.set_meta("goals", g)
    store.set_meta("goals_at", time.time())


def add_budget(store, usd, period="month", providers=(), tools=()) -> str:
    """A budget on API spend (real money), for some providers or tools, or overall. Its id."""
    from . import spend
    label = " + ".join([spend.label(p) for p in providers] + [TOOLS.get(t, t) for t in tools]) or "Overall"
    scope = {k: sorted(v) for k, v in (("providers", [p.lower() for p in providers]), ("tools", list(tools))) if v}
    gid = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "budget"
    set_goal(store, gid, {"label": label, "scope": scope, "count": "api", "unit": "usd", "amount": float(usd),
                          "period": period, "start": time.time(), "tz": time.localtime().tm_gmtoff})
    return gid


def apply_budget(store, body) -> dict:
    """What the dashboard sends: {"off": id} removes a budget; {"usd", "per", "provider" | "project"} sets one
    on API spend (real money), for a provider, a project (its id) or overall. Raises ValueError when it's not."""
    if not isinstance(body, dict):
        raise ValueError("bad body")
    if "off" in body:
        if not remove_goal(store, str(body["off"])):
            raise ValueError("no such budget")
        return {"ok": True, "off": body["off"]}
    m = body.get("mode")
    if m is not None and m not in MODES:
        raise ValueError("mode: off, watch, advise or guard")
    if "id" in body:   # change an existing budget's mode
        if m is None or not set_mode(store, str(body["id"]), m):
            raise ValueError("no such budget, or no mode")
        return {"ok": True, "id": body["id"], "mode": m}
    try:
        usd = float(body.get("usd"))
    except (TypeError, ValueError):
        raise ValueError("an amount in $") from None
    per = body.get("per") or "month"
    if not (0 < usd < 1e7) or per not in PERIODS:
        raise ValueError("an amount over $0, per day, week or month")
    provider, project = body.get("provider"), body.get("project")
    for v in (provider, project):
        if v is not None and not (isinstance(v, str) and 0 < len(v) <= 32 and re.fullmatch(r"[\w.:-]+", v)):
            raise ValueError("bad provider or project")
    if project:
        from . import projects
        name = projects.names(store).get(project) or f"project {project[:8]}"
        gid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or project[:12]
        set_goal(store, gid, {"label": name, "scope": {"projects": [project]}, "count": "api", "unit": "usd",
                              "amount": usd, "period": per, "start": time.time(), "tz": time.localtime().tm_gmtoff})
    else:
        gid = add_budget(store, usd, per, providers=[provider] if provider else ())
    if m:
        set_mode(store, gid, m)
    return {"ok": True, "id": gid}


def remove_goal(store, gid) -> bool:
    g = goals_(store)
    if gid not in g:
        return False
    del g[gid]
    store.set_meta("goals", g)
    store.set_meta("goals_at", time.time())
    return True


# what the budget agent may do for a budget, each mode including the ones before it
MODES = {"off": "no alerts", "watch": "alerts you as it runs out (the default)",
         "advise": "also tells the agent at work, so it paces itself and proposes cheaper ways; you approve",
         "guard": "also stops new work once it's used up: Claude Code's prompts are blocked, `savetokens check`"
                  " exits 4, and Codex and Hermes are told through MCP",
         "decide": "a decision model picks what to do as it runs out (carry on, finish then stop, a cheaper model,"
                   " another plan, wait for the reset, stop now) and savetokens acts on it; guard's rule if it"
                   " can't be reached"}


def mode(goal) -> str:
    return goal.get("mode") if goal.get("mode") in MODES else "watch"


def set_mode(store, gid, m) -> bool:
    g = goals_(store)
    if gid not in g or m not in MODES:
        return False
    g[gid]["mode"] = m
    store.set_meta("goals", g)
    store.set_meta("goals_at", time.time())
    return True


def find_goal(store, name) -> str | None:
    """A budget's id from its id or its name, as `savetokens budget` lists it."""
    g = goals_(store)
    if name in g:
        return name
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return slug if slug in g else next((k for k, v in g.items() if v.get("label", "").lower() == str(name).lower()), None)


def covers(goal, tool, provider, project, billing) -> bool:
    """Whether a session (its tool, provider, project id and billing: "api" or "plan") counts toward this budget.
    Something not known (None) doesn't rule it out."""
    if counts(goal) == "api" and billing == "plan":
        return False
    sc = goal.get("scope") or {}
    if sc.get("tasks"):
        return False   # a task's budget: the session's task isn't known at a prompt
    if sc.get("tools") and tool and tool not in sc["tools"]:
        return False
    if sc.get("providers") and provider and stem(provider) not in {stem(x) for x in sc["providers"]}:
        return False
    return not sc.get("projects") or not project or project in sc["projects"]


def used_up(store, now=None, mode_=None) -> list[tuple[str, dict, float]]:
    """(id, budget, % used) for budgets used up this period, only those in mode_ when given. Cheap: no forecast."""
    now = now or time.time()
    out = []
    for gid, g in goals_(store).items():
        if mode_ and mode(g) != mode_:
            continue
        since, _ = period(g, now)
        pool = Pool(f"goal:{gid}", "all", "goal", f"goal:{gid}", g)
        used = 100.0 * spent(store, pool, since, now) / g["amount"] if g.get("amount") else 0.0
        if used >= 100:
            out.append((gid, g, used))
    return out


COUNTS = {"api": "API spend: only what was billed pay-as-you-go", "all": "all usage, at API prices"}


def counts(goal) -> str:
    """What a budget counts: "api" (real money: pay-as-you-go only) or "all" (plan usage too, at API prices).
    Budgets from before this was asked count everything."""
    return goal.get("count") or "all"


def stem(provider) -> str:
    """A provider's name without its domain ending: "chutes.ai" -> "chutes", "saygm.com" -> "saygm"."""
    return provider.lower().split(".")[0] if "." in provider else provider.lower()


def _scope(goal, store=None):
    """SQL (where, args) for the requests a budget counts: its project or task, providers and tools, and
    whether plan usage counts."""
    from . import spend
    sc = goal.get("scope") or {}
    where, args = [], []
    for col, key in (("task_id", "tasks"), ("project_id", "projects")):
        ids = sc.get(key)
        if ids:
            where.append(f"{col} IN ({','.join('?' * len(ids))})")
            args += list(ids)
            break
    if sc.get("providers"):   # a provider by any of its names: "chutes" is the "chutes.ai" Hermes records
        names = sorted({n for p in sc["providers"] for n in (p, stem(p))})
        where.append(f"({spend.PROVIDER} IN ({','.join('?' * len(names))})"
                     + "".join(f" OR {spend.PROVIDER} LIKE ?" for _ in names) + ")")
        args += names + [f"{n}.%" for n in names]
    if sc.get("tools"):
        where.append(f"harness IN ({','.join('?' * len(sc['tools']))})")
        args += list(sc["tools"])
    if counts(goal) == "api":
        var, fixed = spend._variable(plans(store) if store is not None else {})
        where.append(f"({var})")
        args += fixed
    return " AND ".join(where) or "1", args


def describe(goal) -> str:
    """What a budget counts, in words: "API spend · OpenAI · Codex"."""
    sc = goal.get("scope") or {}
    from . import spend
    parts = ["API spend" if counts(goal) == "api" else "all usage at API prices" if goal.get("unit", "usd") == "usd"
             else "all usage"]
    parts += [spend.label(p) for p in sc.get("providers") or []] + [TOOLS.get(t, t) for t in sc.get("tools") or []]
    return " · ".join(parts)


def history(store, pool: Pool, now, hours=meter.HISTORY_HOURS):
    """(hourly demand [(hour, value)], % per weight unit or None) for a pool."""
    if pool.kind == "goal":
        where, args = _scope(pool.budget, store)
        end = meter.hour_floor(now)
        got = {}
        for h, v in store.conn.execute(
                f"SELECT CAST(ts / {meter.HOUR} AS INTEGER) * {meter.HOUR} AS h, SUM({UNITS[pool.unit]}) FROM usage"
                f" WHERE {where} AND ts >= ? AND ts < ? GROUP BY h", args + [end - hours * meter.HOUR, end]):
            got[h] = float(v or 0)
        return ([(h, got.get(h, 0.0)) for h in range(int(min(got)), int(end), meter.HOUR)] if got else []), None
    if pool.kind == "api":
        return meter.spend(store, now, hours, pool.harness), None
    return meter.demand(store, now, hours, pool.harness, pool.account)


def period(budget, now):
    """(start, end) of the budget period holding `now`, in the time zone the budget was set in; for a total
    by a date, from when it was set to that date."""
    if budget.get("by"):
        return budget["start"], budget["by"]
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
    if pool.kind == "goal":
        hub = (store.meta("hub_goal_spent") or {}).get(pool.id)
        if hub and hub["since"] == since and until - hub["at"] < 3600:   # the hub's total: every machine
            return hub["spent"]
        where, args = _scope(pool.budget, store)
        return store.conn.execute(f"SELECT COALESCE(SUM({UNITS[pool.unit]}), 0) FROM usage WHERE {where}"
                                  " AND ts >= ? AND ts <= ?", args + [since, until]).fetchone()[0]
    return store.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE harness = ? AND billing = 'api'"
                              " AND ts >= ? AND ts <= ?", (pool.harness, since, until)).fetchone()[0]


def unpriced(store, harness, since):
    """Models used on an API key with no known price (their usage can't count against a budget)."""
    return [r[0] for r in store.conn.execute(
        "SELECT DISTINCT model FROM usage WHERE harness = ? AND billing = 'api' AND cost_usd IS NULL AND ts >= ?"
        " AND model IS NOT NULL", (harness, since))]
