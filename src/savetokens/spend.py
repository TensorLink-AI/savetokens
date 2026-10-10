"""Spend and tokens by provider and model: today, this week and this month, and what's coming.

  pay as you go   requests billed by API: Claude Code or Codex on a key, Hermes on any provider.
                  Their price (the provider's own figure, or the price list) is the cost.
  subscriptions   a fixed price per day, week or month (`savetokens plan anthropic --usd 200`):
                  spread evenly over time whatever is used. What a plan's requests would have
                  cost at API prices is kept too, as "API value".
Tokens count everything: input, cache reads and writes, output.

Each provider's hourly tokens are forecast (Ephemeris, with the baseline as fallback) as the
series "tokens:<provider>". Dollars follow from that provider's recent dollars per token, and
each model's part from its recent share. Totals add the providers' sample paths one by one, so
their range is the range of the sum.
"""
from __future__ import annotations

import time
from collections import defaultdict

from . import forecast, pools
from .meter import HISTORY_HOURS, HOUR, hour_floor

UNIT = "mtokens"                       # forecast series unit: millions of tokens an hour
TOKENS = "(input + cache_read + cache_write_5m + cache_write_1h + output)"
PROVIDER = ("COALESCE(provider, CASE harness WHEN 'claude-code' THEN 'anthropic' WHEN 'codex' THEN 'openai'"
            " ELSE harness END)")
PERIODS = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400}   # "next": the coming day, week, 30 days
RATE_DAYS = 7                          # dollars per token and model shares: from the last week
ACTIVE_DAYS = 14                       # a provider used in this time gets a forecast
MIN_HISTORY_HOURS = 24
NAMES = {"anthropic": "Anthropic", "openai": "OpenAI", "openai-codex": "OpenAI (ChatGPT plan)",
         "openrouter": "OpenRouter", "nous": "Nous Portal", "gemini": "Google Gemini", "google": "Google",
         "fireworks": "Fireworks", "together": "Together", "groq": "Groq", "deepseek": "DeepSeek",
         "mistral": "Mistral", "xai": "xAI", "zai": "Z.ai", "kimi": "Kimi", "minimax": "MiniMax",
         "chutes": "Chutes", "chutes.ai": "Chutes", "synthetic.new": "Synthetic", "local": "Local models",
         "bedrock": "AWS Bedrock", "vertex": "Google Vertex", "azure": "Azure OpenAI", "copilot": "GitHub Copilot"}


def label(provider) -> str:
    return NAMES.get(provider) or provider


def key(provider) -> str:
    return f"tokens:{provider}"


def tz(store) -> int:
    """The user's UTC offset: sent by their machines to the server, else this machine's."""
    got = store.meta("tz")
    return int(got) if got is not None else time.localtime().tm_gmtoff


def _variable(plans):
    """SQL for pay-as-you-go rows: billed by API, and not through a provider on a subscription (Hermes)."""
    fixed = sorted(plans)
    sql = "billing = 'api'"
    if fixed:
        sql += f" AND NOT (harness = 'hermes' AND {PROVIDER} IN ({','.join('?' * len(fixed))}))"
    return sql, fixed


def history(store, provider, now, hours=HISTORY_HOURS):
    """Millions of tokens per complete hour before now: [(hour, value)], oldest first, zeros filled in."""
    end = hour_floor(now)
    got = defaultdict(float)
    for h, v in store.conn.execute(
            f"SELECT CAST(ts / 3600 AS INTEGER) * 3600, SUM({TOKENS}) FROM usage WHERE {PROVIDER} = ? AND ts >= ?"
            f" AND ts < ? GROUP BY 1", (provider, end - hours * HOUR, end)):
        got[float(h)] += v / 1e6
    if not got:
        return []
    return [(h, got.get(h, 0.0)) for h in range(int(min(got)), int(end), HOUR)]


def series(store, now):
    """Providers worth forecasting: used lately, with a day of history."""
    out = []
    for (p,) in store.conn.execute(f"SELECT DISTINCT {PROVIDER} FROM usage WHERE ts >= ?",
                                   (now - ACTIVE_DAYS * 86400,)):
        hist = history(store, p, now)
        if len(hist) >= MIN_HISTORY_HOURS:
            out.append((p, hist))
    return out


def fixed_cost(plan, start, end):
    """A subscription's price over [start, end), spread evenly over its own periods."""
    total, t = 0.0, start
    while t < end:
        s, e = pools.period(plan, t)
        nxt = min(end, e)
        total += plan["usd"] * (nxt - t) / (e - s)
        t = nxt
    return total


def _paths(store, provider, now, end):
    """Per sample path: millions of tokens from now to end. (values, source); None when there's no forecast."""
    for src in ("ephemeris", "baseline"):
        paths = forecast.load_paths(store, key(provider), src)
        w = forecast.walk(paths, now, end, None, extend=True) if paths else None
        if w:
            return [a for a, _ in w], src
    return None, None


def _q3(values):
    return [forecast._q(values, 0.1), forecast._q(values, 0.5), forecast._q(values, 0.9)] if values else None


def summary(store, now=None) -> dict:
    """Spend and tokens so far this day, week and month; projected to each one's end; and the next day, week
    and 30 days. By provider, by model within it, and in total (pay as you go plus subscriptions)."""
    now = now or time.time()
    offset, plans = tz(store), pools.plans(store)
    var_sql, var_args = _variable(plans)
    # the last week: dollars per token (pay as you go) and each model's part, per provider
    rates, shares = {}, defaultdict(list)
    for r in store.conn.execute(
            f"SELECT {PROVIDER} AS p, model, SUM({TOKENS}) AS tok,"
            f" SUM(CASE WHEN {var_sql} THEN COALESCE(cost_usd, 0) ELSE 0 END) AS usd"
            f" FROM usage WHERE ts >= ? GROUP BY p, model", (*var_args, now - RATE_DAYS * 86400)):
        shares[r["p"]].append((r["model"], r["tok"] or 0, r["usd"] or 0.0))
    for p, ms in shares.items():
        tok, usd = sum(m[1] for m in ms), sum(m[2] for m in ms)
        rates[p] = usd / tok * 1e6 if tok else 0.0   # pay-as-you-go $ per million of all the provider's tokens
    out = {"now": now, "tz": offset, "periods": {}, "providers": {}, "plans": plans}
    sources = {}
    totals = {}
    for k in PERIODS:
        start, end = pools.period({"period": k, "tz": offset}, now)
        rows = store.conn.execute(
            f"SELECT {PROVIDER} AS p, model, SUM({TOKENS}) AS tok, COUNT(*) AS n,"
            f" SUM(CASE WHEN {var_sql} THEN COALESCE(cost_usd, 0) ELSE 0 END) AS usd,"
            f" SUM(COALESCE(cost_usd, 0)) AS value FROM usage WHERE ts >= ? AND ts <= ? GROUP BY p, model",
            (*var_args, start, now)).fetchall()
        provs = {r["p"] for r in rows} | set(plans) | set(shares)
        sum_tok, sum_usd, sum_n = None, None, None   # path-wise sums across providers
        sums_next_tok, sums_next_usd = None, None
        nxt_end = now + PERIODS[k]
        for p in sorted(provs):
            mine = [r for r in rows if r["p"] == p]
            plan = plans.get(p)
            so_far = {"tokens": sum(r["tok"] or 0 for r in mine), "usd": sum(r["usd"] or 0 for r in mine),
                      "api_value": sum(r["value"] or 0 for r in mine), "requests": sum(r["n"] for r in mine),
                      "fixed_usd": fixed_cost(plan, start, now) if plan else 0.0}
            rest, src = _paths(store, p, now, end)
            ahead, _ = _paths(store, p, now, nxt_end)
            if src:
                sources[p] = src
            pay = rates.get(p, 0.0)   # pay-as-you-go $ per million of all its tokens: a plan's share costs nothing
            entry = out["providers"].setdefault(p, {"provider": p, "label": label(p), "plan": plan,
                                                    "usd_per_mtok": rates.get(p), "periods": {}, "models": {}})
            fixed_end = fixed_cost(plan, start, end) if plan else 0.0
            fixed_next = fixed_cost(plan, now, nxt_end) if plan else 0.0
            proj_tok = [so_far["tokens"] + v * 1e6 for v in rest] if rest else None
            proj_usd = [so_far["usd"] + v * pay + fixed_end for v in rest] if rest else None
            next_tok = [v * 1e6 for v in ahead] if ahead else None
            next_usd = [v * pay + fixed_next for v in ahead] if ahead else None
            entry["periods"][k] = {**so_far, "start": start, "end": end,
                                   "cost": so_far["usd"] + so_far["fixed_usd"],
                                   "projected": {"tokens": _q3(proj_tok), "cost": _q3(proj_usd),
                                                 "fixed_usd": fixed_end},
                                   "next": {"tokens": _q3(next_tok), "cost": _q3(next_usd), "fixed_usd": fixed_next,
                                            "until": nxt_end}}
            sum_tok = _add(sum_tok, proj_tok, so_far["tokens"])
            sum_usd = _add(sum_usd, proj_usd, so_far["usd"] + fixed_end)
            sums_next_tok = _add(sums_next_tok, next_tok, 0.0)
            sums_next_usd = _add(sums_next_usd, next_usd, fixed_next)
            sum_n = (sum_n or 0) + so_far["requests"]
            # models: their own so-far numbers, and their recent share of the provider's coming tokens and cost
            tok_all = sum(m[1] for m in shares.get(p, [])) or 0
            usd_all = sum(m[2] for m in shares.get(p, [])) or 0
            for r in mine:
                m = entry["models"].setdefault(r["model"] or "unknown", {"model": r["model"] or "unknown",
                                                                         "periods": {}})
                m["periods"][k] = {"tokens": r["tok"] or 0, "usd": r["usd"] or 0.0, "api_value": r["value"] or 0.0}
            for model, tok, usd in shares.get(p, []):
                m = entry["models"].setdefault(model or "unknown", {"model": model or "unknown", "periods": {}})
                m["share_tokens"] = tok / tok_all if tok_all else 0.0
                m["share_usd"] = usd / usd_all if usd_all else 0.0
                per = m["periods"].setdefault(k, {"tokens": 0, "usd": 0.0, "api_value": 0.0})
                e = entry["periods"][k]
                if e["projected"]["tokens"]:
                    per["projected_tokens"] = per["tokens"] + (e["projected"]["tokens"][1] - e["tokens"]) * m["share_tokens"]
                    per["projected_usd"] = per["usd"] + (e["projected"]["cost"][1] - e["usd"] - fixed_end) * m["share_usd"]
                    per["next_tokens"] = e["next"]["tokens"][1] * m["share_tokens"]
                    per["next_usd"] = (e["next"]["cost"][1] - fixed_next) * m["share_usd"]
        so = [e["periods"][k] for e in out["providers"].values() if k in e["periods"]]
        totals[k] = {"start": start, "end": end, "tokens": sum(x["tokens"] for x in so),
                     "usd": sum(x["usd"] for x in so), "fixed_usd": sum(x["fixed_usd"] for x in so),
                     "api_value": sum(x["api_value"] for x in so), "requests": sum_n or 0}
        totals[k]["cost"] = totals[k]["usd"] + totals[k]["fixed_usd"]
        totals[k]["projected"] = {"tokens": _q3(sum_tok), "cost": _q3(sum_usd),
                                  "fixed_usd": sum(x["projected"]["fixed_usd"] for x in so)}
        totals[k]["next"] = {"tokens": _q3(sums_next_tok), "cost": _q3(sums_next_usd), "until": nxt_end,
                             "fixed_usd": sum(x["next"]["fixed_usd"] for x in so)}
    out["periods"] = totals
    for e in out["providers"].values():
        e["source"] = sources.get(e["provider"])
        e["models"] = sorted(e["models"].values(), key=lambda m: -(m["periods"].get("month", {}).get("tokens", 0)))
    out["providers"] = sorted(out["providers"].values(),
                              key=lambda e: -(e["periods"]["month"]["cost"] + e["periods"]["month"]["tokens"] / 1e9))
    out["source"] = "ephemeris" if "ephemeris" in sources.values() else "baseline" if sources else None
    return out


def _add(acc, values, base):
    """Path-wise sum: acc + values (a provider with no forecast adds its known `base` to every path)."""
    if values is None:
        values = [base]
    if acc is None:
        return list(values)
    if len(acc) == 1 and len(values) > 1:
        acc = acc * len(values)
    if len(values) == 1:
        return [a + values[0] for a in acc]
    return [a + b for a, b in zip(acc, values)]


def money(v) -> str:
    return "–" if v is None else f"${v:,.0f}" if abs(v) >= 100 else f"${v:,.2f}"


def text(s) -> str:
    """A few lines for an agent: spend so far and likely by each period's end, then by provider."""
    names = {"day": "today", "week": "this week", "month": "this month"}
    out = []
    for k, x in s["periods"].items():
        proj = x["projected"]["cost"]
        out.append(f"{names[k]}: {money(x['cost'])} so far ({x['tokens'] / 1e6:,.1f}M tokens)"
                   + (f", likely {money(proj[1])} by its end ({money(proj[0])}–{money(proj[2])})" if proj else ""))
    for e in s["providers"][:8]:
        m = e["periods"]["month"]
        proj = m["projected"]["cost"]
        out.append(f"- {e['label']}" + (f" (plan {money(e['plan']['usd'])}/{e['plan']['period']})" if e["plan"] else "")
                   + f": this month {money(m['cost'])}, {m['tokens'] / 1e6:,.1f}M tokens"
                   + (f", likely {money(proj[1])}" if proj else "")
                   + (f"; top model {e['models'][0]['model']}" if e["models"] else ""))
    return "\n".join(out)
