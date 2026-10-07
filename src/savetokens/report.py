"""Where tokens went, and deterministic waste rules with dollar estimates.

Estimates are API-list-price dollars. For subscription users they show the
relative size of each kind of waste, not money billed.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field

from . import pricing
from .store import Store

BIG_CONTEXT = 150_000
CARRY_BASE = 50_000
BIG_OUTPUT_CHARS = 40_000
CHARS_PER_TOKEN = 4

ESTIMATED_SOURCES = {"hermes_blended"}


@dataclass
class Finding:
    rule: str
    title: str
    usd: float
    detail: str
    fix: str | None = None
    evidence: list = field(default_factory=list)


def _read_price(model):
    r = pricing.rates(model)
    return r[2] / 1e6 if r else 0.0


def _write_price(model):
    r = pricing.rates(model)
    return r[0] * 1.25 / 1e6 if r else 0.0


def _by_session(events):
    out = defaultdict(list)
    for e in events:
        out[e.session_id].append(e)
    return out


def context_carry(events):
    """Turns that re-read a context far past a working size. /clear between tasks avoids this."""
    total, worst = 0.0, []
    for sid, evs in _by_session([e for e in events if not e.subagent]).items():
        big = [e for e in evs if e.context_tokens > BIG_CONTEXT]
        usd = sum((e.context_tokens - CARRY_BASE) * _read_price(e.model) for e in big)
        if big:
            worst.append((usd, sid, len(big), max(e.context_tokens for e in big), evs[0].project))
        total += usd
    worst.sort(reverse=True)
    if total < 0.01:
        return None
    ev = [f"{p or sid[:8]}: {n} turns over {BIG_CONTEXT // 1000}k (peak {peak // 1000}k), ${u:.2f}"
          for u, sid, n, peak, p in worst[:3]]
    return Finding("context_carry", "Long contexts re-read every turn", total,
                   f"Turns with more than {BIG_CONTEXT // 1000}k tokens of context paid to re-read everything above"
                   f" {CARRY_BASE // 1000}k. Use /clear when you switch task, /compact when you don't.",
                   "context-guard", ev)


def cache_rebuilds(events):
    """Cache writes after an idle gap longer than the cache TTL: paying to rebuild the prefix."""
    total, n = 0.0, 0
    for sid, evs in _by_session([e for e in events if not e.subagent]).items():
        for prev, e in zip(evs, evs[1:]):
            ttl = 3600 if prev.cache_write_1h > 0 and prev.cache_write_5m == 0 else 300
            rebuilt = e.cache_write_5m + e.cache_write_1h
            if e.ts - prev.ts > ttl and rebuilt >= 10_000:
                r = pricing.rates(e.model)
                if not r:
                    continue
                write = (e.cache_write_5m * 1.25 + e.cache_write_1h * 2.0) * r[0] / 1e6
                total += write - rebuilt * r[2] / 1e6
                n += 1
    if n == 0:
        return None
    return Finding("cache_rebuild", "Prompt cache expired while idle", total,
                   f"{n} turns rebuilt an expired cache after a pause. Finish or /clear before long breaks;"
                   " a fresh session after a break costs less than resuming a long one.", None)


def _tools(store: Store, since, sessions):
    return [t for t in store.tools(since=since) if sessions is None or t.session_id.split("/", 1)[0] in sessions]


def rereads(store: Store, since, sessions=None):
    tools = _tools(store, since, sessions)
    reads, sizes = defaultdict(int), defaultdict(list)
    extra = 0
    names = defaultdict(int)
    for t in tools:
        if not t.target:
            continue
        key = (t.session_id, t.target)
        if t.kind == "edit":
            reads[key] = 0
        elif t.kind == "read":
            reads[key] += 1
            if t.output_chars:
                sizes[key].append(t.output_chars)
            if reads[key] > 1:
                extra += (t.output_chars or 0) // CHARS_PER_TOKEN
                names[t.target] += 1
    if not names:
        return None
    top = sorted(names.items(), key=lambda kv: -kv[1])[:3]
    usd = extra * 5.0 * 1.25 / 1e6    # written to cache once at Opus-class input price
    return Finding("reread", "Files re-read without changes", usd,
                   f"{sum(names.values())} re-reads of unchanged files ({extra // 1000}k tokens).",
                   "output-hygiene", [f"{t}: {n} extra reads" for t, n in top])


def big_outputs(store: Store, events, since, sessions=None):
    """Large tool outputs stay in context and are re-read on every later turn."""
    main = _by_session([e for e in events if not e.subagent])
    total, n = 0.0, 0
    for t in _tools(store, since, sessions):
        if (t.output_chars or 0) < BIG_OUTPUT_CHARS:
            continue
        sid = t.session_id.split("/", 1)[0]
        if "/" in t.session_id:
            continue
        later = [e for e in main.get(sid, []) if e.ts > t.ts]
        tokens = t.output_chars // CHARS_PER_TOKEN
        total += sum(tokens * _read_price(e.model) for e in later)
        if later:
            total += tokens * _write_price(later[0].model)
        n += 1
    if n == 0:
        return None
    return Finding("big_output", "Large tool outputs carried in context", total,
                   f"{n} tool outputs over {BIG_OUTPUT_CHARS // 1000}k characters were re-read on later turns."
                   " Cap command output (tail, head, grep) instead of dumping it.", "bash-output-cap")


def subagent_model(events):
    sub = [e for e in events if e.subagent and pricing.is_expensive(e.model) and e.cost_usd]
    if not sub:
        return None
    cheaper = sum(pricing.cost("claude-sonnet-5-5", input=e.input, output=e.output, cache_read=e.cache_read,
                               cache_write_5m=e.cache_write_5m, cache_write_1h=e.cache_write_1h) or 0 for e in sub)
    spent = sum(e.cost_usd for e in sub)
    return Finding("subagent_model", "Subagents on an Opus/Fable-class model", spent - cheaper,
                   f"Subagents spent ${spent:.2f}; on Sonnet 5.5 the same tokens cost about ${cheaper:.2f}."
                   " Exploration and search subagents rarely need the top model.", "subagent-model")


def cache_share(events):
    inp = sum(e.input for e in events)
    read = sum(e.cache_read for e in events)
    write = sum(e.cache_write_5m + e.cache_write_1h for e in events)
    denom = inp + read + write
    if denom == 0:
        return None
    share = read / denom
    if share >= 0.8:
        return None
    return Finding("cache_share", "Low prompt-cache hit share", 0.0,
                   f"Only {share:.0%} of prompt tokens came from cache. Mid-session model switches, editing"
                   " CLAUDE.md or MCP changes invalidate the cache.", None)


def findings_for(store: Store, events, since):
    sessions = {e.session_id for e in events}
    found = [f for f in (context_carry(events), cache_rebuilds(events), rereads(store, since, sessions),
                         big_outputs(store, events, since, sessions), subagent_model(events), cache_share(events)) if f]
    return sorted(found, key=lambda f: -f.usd)


def build(store: Store, days=7, harness=None, now=None):
    from . import limits
    now = now or time.time()
    since = now - days * 86400
    events = store.usage(since=since, harness=harness)
    priced = [e for e in events if e.cost_usd is not None]
    totals = {
        "usd": sum(e.cost_usd for e in priced),
        "requests": len(events),
        "unpriced": len(events) - len(priced),
        "input": sum(e.input for e in events),
        "cache_read": sum(e.cache_read for e in events),
        "cache_write": sum(e.cache_write_5m + e.cache_write_1h for e in events),
        "output": sum(e.output for e in events),
        # per-token averages, for calls recorded before savetokens priced them one by one
        "estimated_usd": sum(e.cost_usd for e in priced if e.cost_source in ESTIMATED_SOURCES),
    }
    groups = {}
    for name, key in (("harness", lambda e: e.harness), ("model", lambda e: pricing.normalize(e.model)),
                      ("project", lambda e: e.project or "?"), ("provider", lambda e: e.provider or "?"),
                      ("subagent", lambda e: "subagents" if e.subagent else "main")):
        g = defaultdict(float)
        for e in priced:
            g[key(e)] += e.cost_usd
        groups[name] = sorted(g.items(), key=lambda kv: -kv[1])
    sessions = defaultdict(lambda: [0.0, 0, None, None])
    for e in priced:
        s = sessions[e.session_id]
        s[0] += e.cost_usd
        s[1] += 1
        s[2] = e.project or s[2]
        s[3] = e.harness
    cal = limits.current(store)
    billing = {}
    for b, evs in limits.split_by_billing(store, events).items():
        billing[b] = {"usd": sum(e.cost_usd or 0 for e in evs), "requests": len(evs),
                      "pct7d": limits.pct_of(cal, "seven_day", evs) if b == limits.SUBSCRIPTION else None,
                      "findings": findings_for(store, evs, since)}
    findings = findings_for(store, events, since)
    alerts = store.alerts(since)
    from . import compaction
    by_trigger, compact_cost, n_compact = compaction.stats(store, since)
    window, n_manual, median_manual = compaction.suggested_window(store)
    turns_above, usd_above = compaction.carry_above(store, window, since)
    return {"days": days, "totals": totals, "groups": groups, "findings": findings, "billing": billing,
            "calibration": cal, "plan": store.meta("account_plan") or {},
            "top_sessions": sorted(sessions.items(), key=lambda kv: -kv[1][0])[:5],
            "compaction": {"by_trigger": by_trigger, "cost_usd": compact_cost, "n": n_compact,
                           "configured": compaction.configured_window(), "suggested": window,
                           "turns_above": turns_above, "usd_above": usd_above},
            "guard": guard_summary(store, alerts)}


def guard_summary(store: Store, alerts):
    """Alerts fired, and burn avoided: the alert-time pace over the next 10 minutes, minus what was spent."""
    by_rule = defaultdict(int)
    avoided = 0.0
    for a in alerts:
        by_rule[a["rule"]] += 1
        if a["burn_usd_per_min"]:
            after = sum(e.cost_usd or 0 for e in store.usage(since=a["ts"], until=a["ts"] + 600,
                                                            session_id=a["session_id"]))
            avoided += max(0.0, a["burn_usd_per_min"] * 10 - after)
    return {"alerts": dict(by_rule), "burn_avoided_usd": avoided}


def fmt_tokens(n):
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return str(int(n))


def _render_findings(L, findings, as_pct=None):
    if not findings:
        L.append("    nothing above the thresholds")
    for f in findings:
        if as_pct and f.usd:
            cost = f"≈{f.usd * as_pct:.1f}% wk"
        else:
            cost = f"~${f.usd:,.2f}" if f.usd else "n/a"
        L.append(f"    {cost:>11}  {f.title}")
        L.append(f"                 {f.detail}")
        for e in f.evidence:
            L.append(f"                 - {e}")
        if f.fix:
            L.append(f"                 fix: savetokens fixes apply {f.fix}" if f.fix != "context-guard"
                     else "                 the guard warns when this happens")


def render(r) -> str:
    t = r["totals"]
    plan = r.get("plan") or {}
    L = [f"savetokens report, last {r['days']} days"
         + (f"  ({plan.get('plan')}, {plan.get('tier')}{', extra usage on' if plan.get('extra_usage') else ''})"
            if plan.get("plan") else ""),
         f"  tokens: input {fmt_tokens(t['input'])}, cache read {fmt_tokens(t['cache_read'])},"
         f" cache write {fmt_tokens(t['cache_write'])}, output {fmt_tokens(t['output'])}"]
    cal7 = (r.get("calibration") or {}).get("seven_day")
    for b, g in sorted(r["billing"].items()):
        if b == "subscription":
            pct = (f" ≈ {g['pct7d']:.0f}% of a weekly limit ({g['pct7d'] / max(r['days'] / 7, 1):.0f}% per week)"
                   if g["pct7d"] is not None else " (limit share not learned yet: needs statusline readings)")
            L.append(f"  subscription: {g['requests']:,} requests, ${g['usd']:,.2f} API-equivalent{pct}")
        else:
            label = "API-billed" if b == "api" else "billing unknown"
            est = t.get("estimated_usd") or 0
            L.append(f"  {label}: {g['requests']:,} requests, ${g['usd']:,.2f}"
                     + (f" ({t['unpriced']} without a known price)" if t["unpriced"] else "")
                     + (f"; ${est:,.2f} of it is a rough estimate from per-token averages (calls from before"
                        " savetokens priced each one)" if est >= 0.01 else ""))
    L.append("")
    for name in ("harness", "provider", "model", "project", "subagent"):
        rows = r["groups"][name][:5]
        if rows:
            L.append(f"  by {name} (API-equivalent $): " + ", ".join(f"{k} ${v:,.2f}" for k, v in rows))
    if r["top_sessions"]:
        L += ["", "  biggest sessions:"]
        for sid, (usd, n, project, harness) in r["top_sessions"]:
            L.append(f"    ${usd:,.2f}  {n} requests  {harness}  {project or ''}  {sid[:8]}")
    for b, g in sorted(r["billing"].items()):
        rate = cal7["pooled"] if (b == "subscription" and cal7) else None
        unit = " (share of weekly limit)" if rate else (" (API-equivalent $)" if b == "subscription" else " ($)")
        L += ["", f"  waste found, {b}{unit}, largest first:"]
        _render_findings(L, g["findings"], rate)
    c = r.get("compaction")
    if c and (c["n"] or c["turns_above"]):
        L += ["", "  compaction:"]
        for trig, v in sorted(c["by_trigger"].items()):
            L.append(f"    {trig:7} {v['n']:4} times, median {v['median'] / 1000:.0f}k tokens of context"
                     f" (max {v['max'] / 1000:.0f}k)")
        if c["n"]:
            L.append(f"    compactions cost ≈ ${c['cost_usd']:,.2f} (summary pass + cache rebuild)")
        conf = "auto (near the full window)" if c["configured"] is None else f"{c['configured']:,} tokens"
        L.append(f"    auto-compact window: {conf}")
        if c["configured"] is None or c["configured"] > c["suggested"]:
            L.append(f"    {c['turns_above']:,} turns ran above {c['suggested'] // 1000}k, re-reading ≈"
                     f" ${c['usd_above']:,.2f} above it (upper bound)")
            L.append("    fix: savetokens fixes apply autocompact-window")
    g = r["guard"]
    if g["alerts"]:
        L += ["", "  guard: " + ", ".join(f"{k} {v}" for k, v in g["alerts"].items())
              + f"; burn avoided ≈ ${g['burn_avoided_usd']:,.2f} (estimate)"]
    return "\n".join(L)
