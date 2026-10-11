"""Where tokens go, and what the same work would cost done differently: facts and what-ifs.

Every number here is one of:
  fact      plain accounting from the usage records
  what-if   exact arithmetic under one assumption, stated with the number (prices only: whether the
            work would come out as well is never claimed)
Measured effects (what a change did to your own work) come from history, not from here.

Nothing knows about a particular tool or model: prices come from the catalogue (pricing.py), and
every tool's records have the same shape. A conversation's requests share a `thread`; sources that
only give per-session totals (Hermes) have none, so the context facts leave them out and say so.
"""
from __future__ import annotations

import time
from collections import defaultdict

from . import pricing

DAY = 86400
HISTORY_DAYS = 30   # "models you already use": the ones in your records this recently
FIELDS = ("harness", "provider", "model", "project", "session_id", "billing", "subagent", "thread")


def _rows(store, since, until, where):
    q = ("SELECT harness, provider, model, project, session_id, thread, subagent, billing, ts, input, cache_read,"
         " cache_write_5m, cache_write_1h, output, reasoning, cost_usd FROM usage WHERE ts >= ? AND ts < ?")
    p = [since, until]
    for k, v in (where or {}).items():
        if k not in FIELDS or v is None:
            raise ValueError(f"can't filter by {k}")
        if k == "session_id":
            q += " AND session_id LIKE ?"
            p.append(v + "%")
        else:
            q += f" AND {k} = ?"
            p.append(v)
    return store.conn.execute(q + " ORDER BY thread, ts", p).fetchall()


def _prompt(r):
    return r["input"] + r["cache_read"] + r["cache_write_5m"] + r["cache_write_1h"]


class _Rates:
    """Each request's rate, its long-context tier applied: memoised per model."""

    def __init__(self):
        self.by = {}

    def __call__(self, model, prompt):
        if model not in self.by:
            self.by[model] = pricing.rate(model)
        r = self.by[model]
        if r and r.long and prompt > r.long[0]:
            return r.long[1]
        return r


def facts(store, since, until=None, where=None) -> dict:
    """Accounting for [since, until), optionally for one tool, provider, model, project, session or billing."""
    until = until or time.time()
    rows = _rows(store, since, until, where)
    rate = _Rates()
    tot = defaultdict(float)
    by = {k: defaultdict(lambda: defaultdict(float)) for k in ("provider", "model", "harness", "project", "billing")}
    ctx = defaultdict(float)
    prev = {}
    unpriced = set()
    for r in rows:
        prompt = _prompt(r)
        usd = r["cost_usd"]
        tot["requests"] += 1
        tot["input"] += r["input"]
        tot["cache_read"] += r["cache_read"]
        tot["cache_write"] += r["cache_write_5m"] + r["cache_write_1h"]
        tot["output"] += r["output"]
        tot["tokens"] += prompt + r["output"]
        if r["reasoning"] is not None:
            tot["reasoning"] += r["reasoning"]
            tot["output_reported"] += r["output"]
        if usd is None:
            unpriced.add(r["model"])
        else:
            tot["usd"] += usd
        for k in by:
            g = by[k][r[k] or "unknown"]
            g["tokens"] += prompt + r["output"]
            g["usd"] += usd or 0.0
            g["requests"] += 1

        # context carried over from the previous request in the same conversation
        if not r["thread"]:
            ctx["unthreaded_tokens"] += prompt
            continue
        p = prev.get(r["thread"])
        resent = min(prompt, p["prompt"] + p["output"]) if p else 0
        cached = min(resent, r["cache_read"])
        ctx["prompt"] += prompt
        ctx["resent"] += resent
        ctx["resent_cached"] += cached
        ctx["growth"] += max(0, prompt - p["first"]) if p else 0
        # re-sent tokens are either read from the cache, written to it now (new to the cache: last turn's
        # output), or billed as plain input: only that last part is caching missed
        plain = min(resent - cached, r["input"])
        ctx["resent_plain"] += plain
        rr = rate(r["model"], prompt)
        if rr:
            written = resent - cached - plain
            ctx["resent_usd"] += (plain * rr.input + cached * rr.cache_read + written * rr.cache_write) / 1e6
            # caching what-if: the plain part at the cache-read price, plus writing it to the cache once
            ctx["cache_saving_usd"] += plain * (rr.input - rr.cache_read) / 1e6
            ctx["cache_write_cost_usd"] += plain * max(0.0, rr.cache_write - rr.input) / 1e6
            if p and prompt > p["first"]:   # growth past the conversation's first prompt, at this prompt's mix
                prompt_usd = (r["input"] * rr.input + r["cache_read"] * rr.cache_read
                              + r["cache_write_5m"] * rr.cache_write + r["cache_write_1h"] * rr.cache_write_1h) / 1e6
                ctx["growth_usd"] += (prompt - p["first"]) / prompt * prompt_usd
        prev[r["thread"]] = {"prompt": prompt, "output": r["output"], "first": p["first"] if p else prompt}

    out = {
        "since": since, "until": until, "where": where or {},
        "requests": int(tot["requests"]), "tokens": int(tot["tokens"]), "usd": tot["usd"],
        "token_types": {k: int(tot[k]) for k in ("input", "cache_read", "cache_write", "output")},
        "unpriced_models": sorted(m for m in unpriced if m),
        "by": {k: sorted(({"name": n, **{f: (int(v) if f != "usd" else v) for f, v in g.items()}}
                          for n, g in grp.items()), key=lambda x: (-x["usd"], -x["tokens"]))
               for k, grp in by.items()},
        "reasoning": ({"tokens": int(tot["reasoning"]), "share_of_output": tot["reasoning"] / tot["output_reported"]}
                      if tot["output_reported"] else None),
        "context": None,
    }
    if ctx["prompt"]:
        out["context"] = {
            "resent_tokens": int(ctx["resent"]), "resent_share": ctx["resent"] / ctx["prompt"],
            "resent_cached_share": ctx["resent_cached"] / ctx["resent"] if ctx["resent"] else None,
            "resent_uncached_tokens": int(ctx["resent_plain"]),
            "resent_usd": ctx["resent_usd"], "growth_tokens": int(ctx["growth"]), "growth_usd": ctx["growth_usd"],
            "unthreaded_tokens": int(ctx["unthreaded_tokens"]),
            "cache_saving_usd": ctx["cache_saving_usd"], "cache_write_cost_usd": ctx["cache_write_cost_usd"],
        }
    return out


def as_model(store, since, until=None, where=None, model=None) -> list[dict]:
    """What-if: the same tokens at another model's prices. With `model`, that one; otherwise each priced model
    in this scope against every cheaper one you've used from the same provider (in the last HISTORY_DAYS). Price only: whether that model
    would do the work as well isn't claimed."""
    until = until or time.time()
    rows = _rows(store, since, until, where)
    used = defaultdict(lambda: {"usd": 0.0, "provider": None, "subagent_usd": 0.0, "rows": []})
    for r in rows:
        if r["cost_usd"] is None or not r["model"]:
            continue
        u = used[r["model"]]
        u["usd"] += r["cost_usd"]
        u["provider"] = r["provider"]
        u["subagent_usd"] += r["cost_usd"] if r["subagent"] else 0.0
        u["rows"].append(r)
    # the models you already use (in the last HISTORY_DAYS, anywhere), by provider
    yours = defaultdict(set)
    for prov, n in store.conn.execute("SELECT DISTINCT provider, model FROM usage WHERE ts >= ? AND model IS NOT NULL"
                                      " AND cost_usd IS NOT NULL", (until - HISTORY_DAYS * DAY,)):
        yours[prov].add(n)
    out = []
    for m, u in used.items():
        if model:
            targets = [model] if pricing.normalize(model) != pricing.normalize(m) else []
        else:
            targets = sorted(n for n in yours[u["provider"]] if n != m)
        for n in targets:
            base = pricing.rate(n)
            if base is None:
                continue
            usd = 0.0
            for r in u["rows"]:
                prompt = _prompt(r)
                usd += pricing.cost(n, input=r["input"], output=r["output"], cache_read=r["cache_read"],
                                    cache_write_5m=r["cache_write_5m"], cache_write_1h=r["cache_write_1h"],
                                    at=base.long[1] if base.long and prompt > base.long[0] else base)
            if model or usd < u["usd"]:
                out.append({"kind": "what-if", "model": m, "as": n, "usd": u["usd"], "usd_as": usd,
                            "saving_usd": u["usd"] - usd, "subagent_share": u["subagent_usd"] / u["usd"] if u["usd"] else 0,
                            "assumption": f"the same tokens at {n}'s prices; quality not compared"})
    return sorted(out, key=lambda x: -x["saving_usd"])


def what_ifs(f: dict) -> list[dict]:
    """Same work, same models, done differently: from facts(), biggest first, each with its assumption.
    (Other models' prices are a separate question: as_model.)"""
    out = []
    c = f.get("context")
    if c and c["cache_saving_usd"] > 0:
        out.append({"kind": "what-if", "id": "cache", "saving_usd": c["cache_saving_usd"],
                    "upto_cost_usd": c["cache_write_cost_usd"],
                    "title": "Cache the context that's re-sent",
                    "assumption": "re-sent tokens billed as plain input, at the provider's cache-read price instead;"
                                  " writing them to the cache once costs up to the extra shown"})
    if c and c["growth_usd"] > 0:
        out.append({"kind": "what-if", "id": "growth", "saving_usd": c["growth_usd"],
                    "title": "Keep conversations near their starting size",
                    "assumption": "an upper bound: every prompt beyond its conversation's first one; starting fresh"
                                  " still costs a handover"})
    return sorted(out, key=lambda x: -x["saving_usd"])


def report(store, days=7, now=None, where=None, model=None) -> dict:
    now = now or time.time()
    f = facts(store, now - days * DAY, now, where)
    return {"days": days, "facts": f, "what_ifs": what_ifs(f),
            "other_models": as_model(store, now - days * DAY, now, where, model)[:5]}


def _usd(v):
    return f"${v:,.0f}" if abs(v) >= 10 else f"${v:,.2f}"


def _n(v):
    return f"{v / 1e9:.2f}B" if v >= 1e9 else f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v:,}"


def text(r) -> str:
    f = r["facts"]
    if not f["requests"]:
        return f"No usage in the last {r['days']} days" + (" for that filter." if f["where"] else ".")
    top = lambda k: " · ".join(f"{x['name']} {_usd(x['usd'])}" if x["usd"] else f"{x['name']} {_n(x['tokens'])} tokens"
                               for x in f["by"][k][:4])
    lines = [f"Last {r['days']} days: {f['requests']:,} requests, {_n(f['tokens'])} tokens, {_usd(f['usd'])}"
             " (API-equivalent where on a plan).",
             f"  providers: {top('provider')}", f"  models: {top('model')}"]
    if len(f["by"]["project"]) > 1 or (f["by"]["project"] and f["by"]["project"][0]["name"] != "unknown"):
        lines.append(f"  projects: {top('project')}")
    c = f["context"]
    if c:
        lines += ["", "Facts:",
                  f"  {c['resent_share']:.0%} of prompt tokens were re-sent from earlier in the conversation"
                  + (f"; {c['resent_cached_share']:.0%} of that came from the cache." if c["resent_cached_share"] is not None else "."),
                  f"  Prompts grew {_n(c['growth_tokens'])} tokens past where their conversations started"
                  f" ({_usd(c['growth_usd'])})."]
    if f["reasoning"]:
        lines.append(f"  {f['reasoning']['share_of_output']:.0%} of output tokens were reasoning (where the tool reports it).")
    if r["what_ifs"]:
        lines += ["", "Same work, done differently (what-ifs):"]
        for w in r["what_ifs"]:
            lines.append(f"  {_usd(w['saving_usd']):>7}  {w['title']}: {w['assumption']}.")
    if r["other_models"]:
        lines += ["", "Same tokens, other models (prices only; whether they'd do the work as well isn't compared):"]
        for s in r["other_models"]:
            sub = f", {s['subagent_share']:.0%} of it subagent work" if s["subagent_share"] else ""
            lines.append(f"  {s['model']} at {s['as']}'s prices: {_usd(s['usd'])} -> {_usd(s['usd_as'])}{sub}.")
    gaps = []
    if c and c["unthreaded_tokens"]:
        gaps.append(f"{_n(c['unthreaded_tokens'])} tokens from tools that report per-session totals have no context facts")
    if f["unpriced_models"]:
        gaps.append("no price yet for " + ", ".join(f["unpriced_models"]) + " (savetokens price MODEL IN OUT)")
    if gaps:
        lines += ["", "Not covered: " + "; ".join(gaps) + "."]
    return "\n".join(lines)
