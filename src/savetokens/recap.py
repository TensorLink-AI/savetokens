"""The recap: how the last week (or 30 days) went against your plan, in numbers you can check.

  savetokens recap            the last 7 days
  savetokens recap --days 30  the last 30 days (what install shows on its first run)

It only claims what the data shows:
- **under plan**: an API budget's dollars against its share for these days;
- **under forecast**: where a weekly limit ended against the projection made early that week;
- **limit hits**: how often you ran out, against the days before.
It never says "we saved you $X": nobody can know what you'd have used without it.
Once a week, upkeep puts the 7-day recap on your next prompt (`maintain`).
"""
from __future__ import annotations

import time
from datetime import datetime

from . import pools as pools_
from .pools import TOOLS

DAY = 86400
PERIOD_DAYS = {"day": 1, "week": 7, "month": 365.25 / 12}
RECAP_EVERY = 7 * DAY


def _usage(store, since, until):
    """{harness: {"usd", "api_usd", "tokens", "requests"}} between since and until."""
    out = {}
    for h, usd, api, tok, n in store.conn.execute(
            "SELECT harness, COALESCE(SUM(cost_usd), 0), COALESCE(SUM(CASE WHEN billing = 'api' THEN cost_usd END), 0),"
            " SUM(input + output + cache_read + cache_write_5m + cache_write_1h), COUNT(*)"
            " FROM usage WHERE ts >= ? AND ts < ? GROUP BY harness", (since, until)):
        out[h] = {"usd": usd, "api_usd": api, "tokens": tok or 0, "requests": n}
    return out


def _hits(store, since, until):
    return store.conn.execute("SELECT COUNT(*) FROM hits WHERE ts >= ? AND ts < ?", (since, until)).fetchone()[0]


def _windows(store, until):
    """Every weekly-limit window that has ended by `until`, from the readings in time order:
    [{"harness", "account", "ended", "final"}]. A window ends at its reset, or earlier when the next one has
    already begun (Codex sometimes resets a week early). Readings taken before the account was known join the
    account seen with the same reset."""
    rows = store.conn.execute("SELECT harness, account, ts, pct, resets FROM meter WHERE name = 'seven_day'"
                              " AND resets IS NOT NULL ORDER BY ts").fetchall()
    owner = {}
    for h, acct, _, _, resets in rows:
        if acct:
            owner.setdefault((h, round(resets / 3600)), acct)
    open_, out = {}, []
    for h, acct, ts, pct, resets in rows:
        key = (h, acct or owner.get((h, round(resets / 3600))))
        w = open_.get(key)
        if w and abs(resets - w["resets"]) > 3600:        # a new window has begun
            out.append({"harness": h, "account": key[1], "ended": min(w["resets"], ts), "final": w["final"]})
            w = None
        if not w:
            w = open_[key] = {"resets": resets, "final": pct}
        w["final"] = max(w["final"], pct)
    for (h, acct), w in open_.items():
        if w["resets"] <= until:                           # reset since the last reading
            out.append({"harness": h, "account": acct, "ended": w["resets"], "final": w["final"]})
    return sorted(out, key=lambda w: w["ended"])


def _weeks(store, since, until):
    """Weekly windows that ended between since and until, each with the earliest projection (p50) made for it
    in its first 3 days, when there is one."""
    out = []
    for w in _windows(store, until):
        if not since <= w["ended"] <= until:
            continue
        first = store.conn.execute(
            "SELECT p50, made_at, window_end FROM outlook WHERE name = 'seven_day' AND account = ?"
            " AND window_end >= ? AND p50 IS NOT NULL ORDER BY made_at LIMIT 1",
            (w["account"] or w["harness"], w["ended"] - 3600)).fetchone()
        early = first and abs(first[2] - w["ended"]) < 3600 and first[1] <= w["ended"] - 4 * DAY
        out.append({**w, "final": min(w["final"], 100.0), "forecast": min(first[0], 100.0) if early else None})
    return out


def _budget(store, pool, since, until, now):
    """A budget over these days: spent against its share, and where its current period is heading."""
    b = pool.budget
    if pool.kind == "goal":
        b = {**b, "usd": b["amount"]}
        days_in = (b["by"] - b["start"]) / DAY if b.get("by") else PERIOD_DAYS[b["period"]]
        per_day = b["usd"] / max(days_in, 1.0)
    else:
        per_day = b["usd"] / PERIOD_DAYS[b["period"]]
    days = (until - since) / DAY
    spent = pools_.spent(store, pool, since, until)
    start, end = pools_.period(b, now)
    so_far = pools_.spent(store, pool, start, now)
    pace = spent / days if days else 0.0                   # $ a day over these days
    likely = so_far + pace * max(0.0, end - now) / DAY
    runs_out = None
    if pace > 0 and likely > b["usd"]:
        runs_out = now + max(0.0, b["usd"] - so_far) / pace * DAY
    return {"tool": pool.harness, "label": b.get("label"), "unit": pool.unit, "budget_usd": b["usd"],
            "per": b.get("period") or "period", "planned_usd": per_day * days,
            "spent_usd": spent, "under_usd": per_day * days - spent, "period_end": end, "so_far_usd": so_far,
            "likely_usd": likely, "runs_out": runs_out}


def recap(store, now=None, days=7) -> dict:
    now = now or time.time()
    since, before = now - days * DAY, now - 2 * days * DAY
    use, prev = _usage(store, since, now), _usage(store, before, since)
    return {
        "days": days, "since": since, "until": now,
        "usage": use,
        "previous_usd": sum(v["usd"] for v in prev.values()),
        "hits": _hits(store, since, now), "previous_hits": _hits(store, before, since),
        "weeks": _weeks(store, since, now),
        "budgets": [_budget(store, p, since, now, now) for p in pools_.pools(store, now, goals=True)
                    if p.kind in ("api", "goal")],
        "efficiency": _efficiency(store, since, now),
    }


def _efficiency(store, since, now):
    """The week's context facts and its biggest same-work what-if (efficiency.py), for one line each."""
    from . import efficiency
    f = efficiency.facts(store, since, now)
    w = efficiency.what_ifs(f)
    from . import effects
    settled = [m for m in effects.followups(store, now) if now - m["shown"] < (effects.SETTLE_DAYS + 7) * DAY]
    return {"context": f["context"], "top": w[0] if w else None, "measured": settled[:2]}


def _date(ts):
    return datetime.fromtimestamp(ts).strftime("%a %-d %b")


def _tokens(n):
    return f"{n / 1e9:.1f}B" if n >= 1e9 else f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"


def _usd(v):
    return f"${v:,.0f}" if abs(v) >= 10 else f"${v:,.2f}"


def text(r) -> str | None:
    """The recap in a few lines, or None when nothing happened in these days."""
    use = r["usage"]
    if not use and not r["weeks"]:
        return None
    head = "Last 7 days" if r["days"] == 7 else f"Last {r['days']} days"
    lines = []
    if use:
        parts = []
        for h, v in sorted(use.items(), key=lambda kv: -kv[1]["usd"]):
            sub = v["usd"] - v["api_usd"]
            if v["usd"] < 0.01:                   # no price known (Codex models): tokens say more than $0
                parts.append(f"{TOOLS.get(h, h)} {_tokens(v['tokens'])} tokens")
                continue
            parts.append(f"{TOOLS.get(h, h)} {_usd(v['usd'])}" + (" (API-equivalent, on your plan)" if sub > v["api_usd"] else ""))
        total = sum(v["usd"] for v in use.values())
        trend = ""
        if r["previous_usd"] > 0:
            change = (total - r["previous_usd"]) / r["previous_usd"] * 100
            trend = f"; spend {abs(change):.0f}% {'up' if change > 0 else 'down'} on the {r['days']} days before"
        lines.append(f"{head}: " + ", ".join(parts) + trend + ".")
    else:
        lines.append(f"{head}: no usage.")
    for b in r["budgets"]:
        tool = b["label"] or TOOLS.get(b["tool"], b["tool"])
        if b["unit"] == "tokens":
            diff = b["under_usd"]
            lines.append(f"  {tool} budget: {_tokens(b['spent_usd'])} of the {_tokens(b['planned_usd'])} tokens planned"
                         f" for these days, {_tokens(abs(diff))} {'under' if diff >= 0 else 'over'} plan.")
            continue
        if b["under_usd"] >= 0:
            lines.append(f"  {tool} budget: {_usd(b['spent_usd'])} of the {_usd(b['planned_usd'])} planned for these days,"
                         f" {_usd(b['under_usd'])} under plan.")
        else:
            lines.append(f"  {tool} budget: {_usd(b['spent_usd'])} against {_usd(b['planned_usd'])} planned,"
                         f" {_usd(-b['under_usd'])} over plan.")
        if b["so_far_usd"] >= b["budget_usd"]:
            lines.append(f"    Already over: {_usd(b['so_far_usd'])} of your {_usd(b['budget_usd'])} this {b['per']}.")
        elif b["runs_out"]:
            lines.append(f"    At this pace your {_usd(b['budget_usd'])} a {b['per']} runs out around {_date(b['runs_out'])}.")
        else:
            lines.append(f"    At this pace it ends the {b['per']} around {_usd(b['likely_usd'])} of {_usd(b['budget_usd'])}.")
    by_tool = {}
    for w in r["weeks"]:
        by_tool.setdefault((w["harness"], w["account"]), []).append(w)
    for ws in by_tool.values():
        if len(ws) > 2:                           # many windows (Codex can reset every few days): one line
            tool = TOOLS.get(ws[0]["harness"], ws[0]["harness"])
            out_of = [w for w in ws if w["final"] >= 100]
            lo, hi = min(w["final"] for w in ws), max(w["final"] for w in ws)
            line = f"  {tool} weekly limit: {len(ws)} windows ended, at {lo:.0f}–{hi:.0f}%"
            if out_of:
                line += f"; you ran out in {len(out_of)} ({', '.join(_date(w['ended']) for w in out_of)})"
            lines.append(line + ".")
            continue
    for w in [w for ws in by_tool.values() if len(ws) <= 2 for w in ws]:
        tool = TOOLS.get(w["harness"], w["harness"])
        line = f"  {tool} weekly limit: ended at {w['final']:.0f}% on {_date(w['ended'])}"
        if w["final"] >= 100:
            line += " (you ran out)"
        elif w["forecast"] is not None and w["forecast"] - w["final"] >= 1:
            line += f", {w['forecast'] - w['final']:.0f} points under the {w['forecast']:.0f}% forecast early that week"
        elif w["forecast"] is not None and w["final"] - w["forecast"] >= 1:
            line += f", {w['final'] - w['forecast']:.0f} points over the {w['forecast']:.0f}% forecast early that week"
        lines.append(line + ".")
    e = r.get("efficiency") or {}
    c = e.get("context")
    if c and c["resent_share"]:
        lines.append(f"  {c['resent_share']:.0%} of prompt tokens were context re-sent"
                     + (f", {c['resent_cached_share']:.0%} of it from the cache." if c["resent_cached_share"] is not None else "."))
    if e.get("top"):
        w = e["top"]
        lines.append(f"  Biggest what-if: {w['title'].lower()}, up to {_usd(w['saving_usd'])}"
                     " (`savetokens efficiency` for the assumptions).")
    for m in e.get("measured") or []:
        lines.append(f"  {m['text']}")
    if r["hits"] or r["previous_hits"]:
        lines.append(f"  Limit hits: {r['hits']} (the {r['days']} days before: {r['previous_hits']}).")
    elif use:
        lines.append("  No limit hits.")
    return "\n".join(lines)


def due(store, now=None) -> str | None:
    """Once a week: the 7-day recap for the next prompt (None when not due or nothing happened)."""
    now = now or time.time()
    last = store.meta("recap_at")
    if last is None:                       # the first week starts now; install showed the 30-day one
        store.set_meta("recap_at", now)
        return None
    if now - last < RECAP_EVERY:
        return None
    store.set_meta("recap_at", now)
    return text(recap(store, now, 7))
