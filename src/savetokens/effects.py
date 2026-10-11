"""Measured effects: what working a certain way did to the cost of your own work.

The third kind of number, after facts and what-ifs (efficiency.py), and only from your history:

  compare()    finished tasks split two ways (started in a fresh session or not; their main model; their
               tool): the median cost of each side, with how many tasks, only when both sides have enough
  followups()  every suggestion savetokens has shown you, once a week has passed: whether the thing it
               was about moved (the share of subagent work on the suggested model, prompt size, ...) and what
               the cost of a unit of work did, before against after

Both say what was observed, not why: other things change too, so they're labelled "measured, before
against after" or "measured, across N tasks", never "this saved you".
"""
from __future__ import annotations

import statistics
import time
from collections import Counter, defaultdict

from . import efficiency, tasks

DAY = 86400
MIN_EACH = 3          # tasks needed on each side of a comparison
WINDOW_DAYS = 14      # before and after a suggestion: up to this many days each side
SETTLE_DAYS = 7       # a suggestion is followed up once this long has passed


def _finished(store, project_ids=None):
    out = []
    for t in tasks.listing(store, project_ids):
        if not (t.get("done") and t.get("n")):
            continue
        rows = store.conn.execute(
            "SELECT session_id, model, harness, ts, COALESCE(cost_usd, 0) usd FROM usage WHERE task_id = ? ORDER BY ts",
            (t["id"],)).fetchall()
        if not rows:
            continue
        first = rows[0]["ts"]
        sessions = {r["session_id"] for r in rows}
        earlier = store.conn.execute(
            f"SELECT COUNT(*) FROM usage WHERE session_id IN ({','.join('?' * len(sessions))}) AND ts < ?",
            list(sessions) + [first - 1]).fetchone()[0]
        by_model, by_tool = defaultdict(float), defaultdict(float)
        for r in rows:
            by_model[r["model"]] += r["usd"]
            by_tool[r["harness"]] += r["usd"]
        out.append({**t, "fresh": earlier == 0, "model": max(by_model, key=by_model.get),
                    "tool": max(by_tool, key=by_tool.get), "requests": len(rows)})
    return out


def _side(ts):
    return {"n": len(ts), "median_usd": statistics.median(t["usd"] for t in ts),
            "median_tokens": statistics.median(t["tokens"] for t in ts),
            "median_requests": statistics.median(t["requests"] for t in ts)}


def compare(store, project_ids=None) -> list[dict]:
    """Measured, across finished tasks: how their cost differs by how they were worked."""
    done = _finished(store, project_ids)
    groups = [("fresh", "started in a fresh session", "that continued an existing session", done,
               lambda t: t["fresh"])]
    for key in ("model", "tool"):
        common = [k for k, n in Counter(t[key] for t in done).most_common(2) if n >= MIN_EACH]
        if len(common) == 2:
            a, b = common
            groups.append((f"{key}:{a}|{b}", f"mainly on {a}", f"mainly on {b}",
                           [t for t in done if t[key] in (a, b)], lambda t, a=a, key=key: t[key] == a))
    out = []
    for sid, yes_label, no_label, pool, pred in groups:
        yes, no = [t for t in pool if pred(t)], [t for t in pool if not pred(t)]
        item = {"kind": "measured", "id": sid, "a": yes_label, "b": no_label, "n_a": len(yes), "n_b": len(no)}
        if len(yes) < MIN_EACH or len(no) < MIN_EACH:
            item["enough"] = False
            item["text"] = (f"Tasks {yes_label} against tasks {no_label}: not enough data yet ({len(yes)} and"
                            f" {len(no)} finished; {MIN_EACH} each needed).")
        else:
            A, B = _side(yes), _side(no)
            ratio = A["median_usd"] / B["median_usd"] if B["median_usd"] else None
            item.update(enough=True, side_a=A, side_b=B, ratio_usd=ratio)
            item["text"] = (f"Tasks {yes_label}: median ${A['median_usd']:,.2f} ({A['median_requests']:.0f} requests),"
                            f" against ${B['median_usd']:,.2f} ({B['median_requests']:.0f}) for tasks {no_label}"
                            + (f", {abs(1 - ratio):.0%} {'less' if ratio < 1 else 'more'}" if ratio else "")
                            + f". Measured across {len(yes)} and {len(no)} finished tasks; cost only, not quality.")
        out.append(item)
    return out


# ── the feedback loop ────────────────────────────────────────────────────────

def _metric(store, sid, since, until, target=None):
    """The thing a suggestion is about, over a window; None when there's nothing to measure."""
    if sid == "subagents":
        rows = store.conn.execute("SELECT model, SUM(COALESCE(cost_usd, 0)) FROM usage WHERE subagent AND ts >= ?"
                                  " AND ts < ? GROUP BY model", (since, until)).fetchall()
        total = sum(r[1] for r in rows)
        return {"name": f"share of subagent $ on {target}", "value": sum(r[1] for r in rows if r[0] == target) / total
                } if total else None
    harness = sid.split(":", 1)[1] if ":" in sid else None
    f = efficiency.facts(store, since, until, {"harness": harness} if harness else None)
    c = f["context"]
    if sid == "fresh" and c and f["requests"]:
        prompts = c["resent_tokens"] / max(1, f["requests"])
        return {"name": "re-sent tokens per request", "value": prompts}
    if sid.startswith("cache:") and c and c["resent_tokens"]:
        return {"name": "re-sent tokens billed as fresh input", "value": c["resent_uncached_tokens"] / c["resent_tokens"]}
    if sid.startswith("reasoning:") and f["reasoning"]:
        return {"name": "reasoning share of output", "value": f["reasoning"]["share_of_output"]}
    return None


def _cost_of_work(store, since, until):
    """$ per finished task in the window when there are enough, else $ per active hour (both measured)."""
    done = [t for t in tasks.listing(store) if t.get("done") and since <= t["done"] < until and t.get("n")]
    if len(done) >= MIN_EACH:
        return {"unit": "finished task", "n": len(done), "value": statistics.median(t["usd"] for t in done)}
    from .advise import active_hours
    rows = store.conn.execute("SELECT ts, COALESCE(cost_usd, 0) FROM usage WHERE ts >= ? AND ts < ?",
                              (since, until)).fetchall()
    hours = active_hours([r[0] for r in rows])
    if hours < 1:
        return None
    return {"unit": "active hour", "n": round(hours, 1), "value": sum(r[1] for r in rows) / hours}


def record_shown(store, options, now=None):
    """Remember each suggestion the first time it's shown (advise / the brief call this)."""
    now = now or time.time()
    seen = store.meta("suggested") or {}
    changed = False
    for o in options:
        if o.get("kind") not in ("what-if", "fact") or o["id"] in seen:
            continue
        target = None
        if o["id"] == "subagents":
            target = o["title"].removeprefix("Run subagents on ")
        seen[o["id"]] = {"title": o["title"], "shown": now, "target": target}
        changed = True
    if changed:
        store.set_meta("suggested", seen)


def followups(store, now=None) -> list[dict]:
    """Measured, before against after, for every suggestion shown at least SETTLE_DAYS ago."""
    now = now or time.time()
    out = []
    for sid, s in (store.meta("suggested") or {}).items():
        shown = s["shown"]
        if now - shown < SETTLE_DAYS * DAY:
            continue
        span = min(WINDOW_DAYS * DAY, now - shown)
        before = (shown - span, shown)
        after = (shown, shown + span)
        mb, ma = _metric(store, sid, *before, s.get("target")), _metric(store, sid, *after, s.get("target"))
        cb, ca = _cost_of_work(store, *before), _cost_of_work(store, *after)
        item = {"kind": "measured", "id": sid, "title": s["title"], "shown": shown, "days": span / DAY,
                "metric_before": mb, "metric_after": ma, "cost_before": cb, "cost_after": ca}
        parts = [f"Since “{s['title']}” ({span / DAY:.0f} days before against after):"]
        if mb and ma:
            parts.append(f"{mb['name']} {_fmt(mb['value'])} → {_fmt(ma['value'])}.")
        if cb and ca and cb["unit"] == ca["unit"]:
            ch = (ca["value"] - cb["value"]) / cb["value"] if cb["value"] else None
            parts.append(f"$ per {cb['unit']} ${cb['value']:,.2f} → ${ca['value']:,.2f}"
                         + (f" ({ch:+.0%})" if ch is not None else "") + f"; n {cb['n']} and {ca['n']}.")
        if len(parts) == 1:
            parts.append("nothing to measure yet.")
        parts.append("Measured; other things changed too.")
        item["text"] = " ".join(parts)
        out.append(item)
    return sorted(out, key=lambda x: -x["shown"])


def _fmt(v):
    return f"{v:.0%}" if 0 <= v <= 1 else f"{v:,.0f}"
