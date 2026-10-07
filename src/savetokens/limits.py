"""Subscription vs API billing, and learning how usage moves subscription limits.

Claude Code sends `rate_limits` in the statusline only to subscribers, so each
session seen there is tagged by that; older sessions take the account's plan
from ~/.claude.json (plan fields only, never names or emails).

Anthropic doesn't publish how limits are counted, so savetokens learns it: for
each statusline reading, the limit's used % is regressed on the API-equivalent
dollars spent per model since that window opened. The fit gives "% of limit per
dollar" per model, shrunk towards a pooled rate for models with little data.
Every fit is logged in `calibration`, keyed by plan tier, so it sharpens over
time and starts again if the plan changes. Usage from other devices or
claude.ai also counts towards the limit but is invisible here, which biases
the rates up; the fit's R² shows how well local usage explains the readings.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict
from pathlib import Path

from . import pricing
from .store import Store

WINDOWS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
THIN_SECONDS = 300          # at most one reading per 5 minutes per window enters the fit
REFIT_SECONDS = 3600
SUBSCRIPTION, API, UNKNOWN = "subscription", "api", "unknown"


def account_plan(claude_json: Path | None = None) -> dict:
    """Plan fields from Claude Code's account record, and whether an API key overrides it."""
    from .adapters.claude_code import claude_home
    path = claude_json or Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home()) / ".claude.json"
    if not path.exists():
        path = claude_home() / ".claude.json"
    try:
        acct = json.loads(path.read_text()).get("oauthAccount") or {}
    except (OSError, ValueError):
        acct = {}
    sub = "subscription" in str(acct.get("billingType", ""))
    if os.environ.get("ANTHROPIC_API_KEY"):
        sub = False
    uuid = acct.get("accountUuid") or acct.get("organizationUuid")
    return {
        # a short hash, so readings and sessions from different accounts never mix
        "account": hashlib.sha1(uuid.encode()).hexdigest()[:12] if uuid else None,
        "billing": SUBSCRIPTION if sub else (API if os.environ.get("ANTHROPIC_API_KEY") else UNKNOWN),
        "plan": acct.get("organizationType"),
        "tier": acct.get("organizationRateLimitTier") or acct.get("userRateLimitTier"),
        "extra_usage": bool(acct.get("hasExtraUsageEnabled")),
    }


def record_session(store: Store, harness, session_id, *, billing, model=None, plan=None, tier=None, source,
                   ts=None, account=None):
    if not session_id:
        return
    ts = ts or time.time()
    store.conn.execute(
        "INSERT INTO sessions (session_id, harness, billing, plan, tier, model, source, first_seen, last_seen,"
        " account) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET"
        " account = COALESCE(sessions.account, excluded.account),"
        " billing = CASE WHEN excluded.source = 'statusline' OR sessions.source != 'statusline'"
        "   THEN excluded.billing ELSE sessions.billing END,"
        " model = COALESCE(excluded.model, sessions.model), plan = COALESCE(excluded.plan, sessions.plan),"
        " tier = COALESCE(excluded.tier, sessions.tier),"
        " source = CASE WHEN sessions.source = 'statusline' THEN sessions.source ELSE excluded.source END,"
        " last_seen = MAX(sessions.last_seen, excluded.last_seen)",
        (session_id, harness, billing, plan, tier, model, source, ts, ts, account))
    store.conn.commit()


def default_billing(store: Store, harness: str) -> str:
    if harness == "hermes":
        return API
    return store.meta("claude_code_billing") or account_plan()["billing"]


def current_account(store: Store):
    plan = store.meta("account_plan")
    return (plan or {}).get("account")


def other_account_sessions(store: Store, account=None) -> set:
    """Sessions known to belong to a different account than the current one."""
    account = account or current_account(store)
    if not account:
        return set()
    return {r[0] for r in store.conn.execute(
        "SELECT session_id FROM sessions WHERE account IS NOT NULL AND account != ?", (account,))}


def account_filter(store: Store):
    """SQL fragment and params selecting limit readings of the current account."""
    account = current_account(store)
    return ("(account IS NULL OR account = ?)", (account,)) if account else ("1=1", ())


def billing_map(store: Store) -> dict:
    return {r["session_id"]: r["billing"] for r in store.conn.execute("SELECT session_id, billing FROM sessions")}


def billing_of(event, bmap, defaults) -> str:
    return bmap.get(event.session_id) or defaults.get(event.harness, UNKNOWN)


def split_by_billing(store: Store, events):
    bmap = billing_map(store)
    defaults = {"claude-code": default_billing(store, "claude-code"), "hermes": API}
    out = defaultdict(list)
    for e in events:
        out[billing_of(e, bmap, defaults)].append(e)
    return out


# ── learning limit rates ─────────────────────────────────────────────────────

def _readings(store: Store, window, since=0):
    acc, params = account_filter(store)
    rows = store.conn.execute(
        f"SELECT ts, {window}_pct AS pct, {window}_resets AS resets FROM limits"
        f" WHERE harness = 'claude-code' AND {window}_pct IS NOT NULL AND {window}_resets IS NOT NULL AND ts >= ?"
        f" AND {acc} ORDER BY ts",
        (since, *params))
    kept, last = [], {}
    for r in rows:
        key = r["resets"]
        if key in last and r["ts"] - last[key] < THIN_SECONDS:
            continue
        last[key] = r["ts"]
        kept.append((r["ts"], r["pct"], r["resets"]))
    return kept


def _design(store: Store, window, readings, subscription_sessions):
    """Rows of (pct, {model: dollars since the window opened}), from one pass over usage."""
    import bisect
    if not readings:
        return []
    span = WINDOWS[window]
    others = other_account_sessions(store)
    events = [e for e in store.usage(since=min(r[2] for r in readings) - span, until=max(r[0] for r in readings) + 1)
              if e.cost_usd and e.session_id not in others
              and (subscription_sessions is None or e.session_id in subscription_sessions)]
    ts = [e.ts for e in events]
    models = sorted({pricing.normalize(e.model) for e in events})
    prefix = {m: [0.0] for m in models}
    for e in events:
        m = pricing.normalize(e.model)
        for k in models:
            prefix[k].append(prefix[k][-1] + (e.cost_usd if k == m else 0.0))
    rows = []
    for t, pct, resets in readings:
        lo, hi = bisect.bisect_left(ts, resets - span), bisect.bisect_right(ts, t)
        x = {m: prefix[m][hi] - prefix[m][lo] for m in models}
        x = {m: v for m, v in x.items() if v > 0}
        if x:
            rows.append((pct, x))
    return rows


def fit(rows, shrink=0.25, iters=200):
    """Per-model % per dollar: non-negative least squares through the origin, shrunk to the pooled rate."""
    if not rows:
        return None
    sxy = sum(y * sum(x.values()) for y, x in rows)
    sxx = sum(sum(x.values()) ** 2 for _, x in rows)
    if sxx == 0:
        return None
    pooled = max(sxy / sxx, 0.0)
    models = sorted({m for _, x in rows for m in x})
    w = {m: pooled for m in models}
    # prior strength in the same units for every model, so a model with little usage stays near the pooled rate
    strength = shrink * sxx / len(rows)
    lam = {m: strength for m in models}
    for _ in range(iters):
        for m in models:
            num = lam[m] * pooled
            den = lam[m]
            for y, x in rows:
                xm = x.get(m, 0.0)
                if not xm:
                    continue
                rest = sum(w[k] * v for k, v in x.items() if k != m)
                num += xm * (y - rest)
                den += xm * xm
            w[m] = max(num / den, 0.0) if den else pooled
    ys = [y for y, _ in rows]
    mean = sum(ys) / len(ys)
    ss_tot = sum((y - mean) ** 2 for y in ys)
    ss_res = sum((y - sum(w[k] * v for k, v in x.items())) ** 2 for y, x in rows)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else None
    return {"pooled": pooled, "models": w, "n": len(rows), "r2": r2,
            "usd": {m: sum(x.get(m, 0) for _, x in rows) / len(rows) for m in models}}


def calibrate(store: Store, now=None, force=False):
    """Refit both windows (at most hourly) and log the result. Returns {window: fit}."""
    now = now or time.time()
    last = store.meta("calibration_at", 0)
    if not force and now - last < REFIT_SECONDS:
        return current(store)
    plan = account_plan()
    subs = {sid for sid, b in billing_map(store).items() if b == SUBSCRIPTION}
    if default_billing(store, "claude-code") == SUBSCRIPTION:
        subs = None   # older sessions without a statusline reading count as subscription too
    out = {}
    for window in WINDOWS:
        f = fit(_design(store, window, _readings(store, window), subs))
        if not f:
            continue
        out[window] = f
        rows = [(now, window, plan["tier"], "*", f["pooled"], f["n"], f["r2"])]
        rows += [(now, window, plan["tier"], m, w, f["n"], f["r2"]) for m, w in f["models"].items()]
        store.conn.executemany("INSERT INTO calibration (ts, window, tier, model, pct_per_usd, n, r2)"
                               " VALUES (?,?,?,?,?,?,?)", rows)
    store.conn.commit()
    store.set_meta("calibration_at", now)
    store.set_meta("calibration", out)
    return out


def current(store: Store) -> dict:
    return store.meta("calibration") or {}


def pct_of(cal: dict, window: str, events) -> float | None:
    """Estimated % of a limit window used by these events, or None before any calibration."""
    f = cal.get(window)
    if not f:
        return None
    return sum((f["models"].get(pricing.normalize(e.model), f["pooled"])) * (e.cost_usd or 0) for e in events)


def effective_rate(store: Store, window: str, now=None, days=7):
    """% of a window per API-equivalent $ at the recent model mix (for converting dollar forecasts)."""
    f = current(store).get(window)
    if not f:
        return None
    now = now or time.time()
    usd = defaultdict(float)
    for e in store.usage(since=now - days * 86400):
        if e.cost_usd:
            usd[pricing.normalize(e.model)] += e.cost_usd
    total = sum(usd.values())
    if not total:
        return f["pooled"]
    return sum(f["models"].get(m, f["pooled"]) * v for m, v in usd.items()) / total


def history(store: Store, window="seven_day", model="*", limit=20):
    """How a learned rate has moved over time (for `savetokens learn`)."""
    return list(store.conn.execute(
        "SELECT ts, tier, pct_per_usd, n, r2 FROM calibration WHERE window = ? AND model = ?"
        " ORDER BY ts DESC LIMIT ?", (window, model, limit)))[::-1]


SETTLE_READINGS = 12
SETTLE_SPAN = 6 * 3600


def learning_state(store: Store) -> dict:
    """Whether the weekly rate has settled: enough readings, spread over enough time."""
    rows = _readings(store, "seven_day")
    f = current(store).get("seven_day") or {}
    span = rows[-1][0] - rows[0][0] if rows else 0
    return {"readings": len(rows), "span_hours": span / 3600, "r2": f.get("r2"),
            "settled": len(rows) >= SETTLE_READINGS and span >= SETTLE_SPAN}
