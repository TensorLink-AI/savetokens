"""The decide mode: a decision model picks what to do as a budget runs out, and savetokens acts on it.

When a budget in decide is at risk (or used up), savetokens sends the measured facts to a decision model
(decisionmodels.io, EU tier: nothing is kept) with the choices that apply right now, and two questions: what
should the work do, and is there something to ask the person. Only numbers, the budget's label and the tool and
provider go: no prompts, code, project names or keys.

  carry_on          the work fits in what's left (not offered once it's used up: only the person raises it)
  finish_then_stop  finish the task at hand, then stop new work that counts toward it
  cheaper_model     a model you've used on this provider that cost less per token, measured from your usage
  move              another plan or budget that has room, named
  wait_for_reset    hold new work until the budget resets
  stop_now          stop new work now (blocked, as guard does)

Each call is logged with what was sent and the answer. Once the budget's period ends, how it ended is added,
so each budget builds a track record, which goes back to the model with the next call. Each choice carries
what it would likely lead to, worked out from the forecast and your measured usage (a what-if, not a guess);
where that can't be measured, the choice says so. If the model can't be reached, the budget behaves as guard.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

from . import alerts, pools
from .store import home

API = "https://api.decisionmodels.io/v1/systemone"
MODEL = "s1-pro-3"               # pinned, so a budget's track record is one model's; takes both questions at once
USD_PER_M = 0.084                # s1-pro, EU tier, per million input tokens (output is free), at launch
KEY_NAMES = ("DECISIONMODELS_API_KEY",)
CACHE_SECONDS = 600              # a budget's call holds this long, so prompts don't each wait on it
RETRY_SECONDS = 60               # after a failure, guard's rule holds this long before trying again
TIMEOUT = 1.5                    # hooks run on every prompt
LOG_MAX = 500
MODEL_DAYS = 30                  # the models compared for cheaper_model: used in this time

DO = {
    "carry_on": "Carry on: the work fits in what's left",
    "finish_then_stop": "Finish the current task, then stop new work that counts toward this budget",
    "cheaper_model": "Switch to {model}, which cost ${rate:.2f} per million tokens here, against ${now_rate:.2f} for {now}",
    "move": "Move the work to {name}, which has room ({used:.0f}% used now)",
    "wait_for_reset": "Hold new work until the budget resets in {span}",
    "stop_now": "Stop new work that counts toward this budget now",
}
ASK = {
    "nothing": "Nothing to ask the person",
    "raise_budget": "Ask the person whether to raise this budget",
    "pause_project": "Ask the person whether to pause this project for the rest of the period",
}
BLOCKS = ("stop_now", "wait_for_reset")   # what stops new work
SHORT = {"carry_on": "Carry on", "finish_then_stop": "Finish this task, then stop", "cheaper_model": "Use a cheaper model",
         "move": "Move the work", "wait_for_reset": "Stopped: waits for the reset", "stop_now": "Stopped"}
HUB_MAX = 2000                   # calls the hub keeps, from every machine


def key_path():
    return home() / "decisionmodels.key"


def api_key() -> str | None:
    """From the environment, else the key file (owner-only). Never copied or printed."""
    for name in KEY_NAMES:
        if os.environ.get(name):
            return os.environ[name]
    p = key_path()
    try:
        if os.name != "nt" and p.stat().st_mode & 0o077:
            return None   # others can read it: don't trust it
        return p.read_text().strip() or None
    except OSError:
        return None


def save_key(key: str):
    p = key_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.touch(mode=0o600)
    tmp.chmod(0o600)
    tmp.write_text(key.strip() + "\n")
    tmp.replace(p)
    return p


def _post(body, key):
    req = urllib.request.Request(API, json.dumps(body).encode(), {
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "S1-Region": "eu"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def _models(store, g, provider, now):
    """(model, $ per million tokens, tokens) for each model used under this budget (on this provider) lately."""
    from . import spend
    where, args = pools._scope(g, store)
    if provider:
        where += f" AND ({spend.PROVIDER} = ? OR {spend.PROVIDER} LIKE ?)"
        args = args + [provider, pools.stem(provider) + "%"]
    rows = store.conn.execute(
        f"SELECT model, SUM(COALESCE(cost_usd, 0)), SUM({pools.UNITS['tokens']}),"
        f" SUM(CASE WHEN ts >= ? THEN COALESCE(cost_usd, 0) ELSE 0 END) FROM usage WHERE {where}"
        " AND ts >= ? AND model IS NOT NULL GROUP BY model", [now - 86400] + args + [now - MODEL_DAYS * 86400]).fetchall()
    return [(m, 1e6 * usd / tok, tok, day) for m, usd, tok, day in rows if tok and usd]


def facts(store, o, g, here, now) -> dict:
    """What's measured about this budget and the work, and what it could switch to. Only numbers and names of
    budgets, plans, tools, providers and models."""
    usd = o["unit"] == "usd"
    f = {"budget": g["label"], "counts": o["counts"], "unit": "USD" if usd else "tokens",
         "used_pct": round(o["used"], 1), "spent": round(o["spent"], 4 if usd else 0), "amount": o["amount"],
         "period": o.get("period") or "total by a date", "hours_to_reset": round((o["resets"] - now) / 3600, 1)}
    if o.get("p50") is not None:
        f["likely_at_reset_pct"] = {"p10": round(o["p10"]), "p50": round(o["p50"]), "p90": round(o["p90"])}
    if o.get("p_hit") is not None:
        f["chance_it_runs_out_pct"] = round(100 * o["p_hit"])
    if o.get("eta") and o["used"] < 100:
        f["hours_until_it_runs_out"] = round((o["eta"] - now) / 3600, 1)
    pool = pools.Pool(o["pool"], "all", "goal", o["pool"], g)
    f["spent_last_24h"] = round(pools.spent(store, pool, now - 86400, now), 4 if usd else 0)
    f["work"] = {"tool": pools.TOOLS.get(here.get("tool"), here.get("tool")), "provider": here.get("provider"),
                 "billing": here.get("billing")}
    ms = _models(store, g, here.get("provider"), now)
    cur = max(ms, key=lambda m: m[3], default=None)
    if cur and cur[3] > 0:
        f["model_now"] = {"model": cur[0], "usd_per_m_tokens": round(cur[1], 3)}
        cheap = min((m for m in ms if m[1] < cur[1]), key=lambda m: m[1], default=None)
        if cheap:
            f["cheaper_model"] = {"model": cheap[0], "usd_per_m_tokens": round(cheap[1], 3),
                                  "tokens_measured": int(cheap[2])}
    f["room_elsewhere"] = _room(store, o, here, now)
    f["if_chosen"] = outcomes(store, o, g, f, here, now)
    f["track_record"] = _past(store, o["pool"][5:])
    for r in f["room_elsewhere"]:
        r.pop("_usd", None)
    return f


def _room(store, o, here, now):
    """Plans and budgets the work isn't counted in that have room: not at risk, not used up."""
    from . import forecast
    out = []
    for x in forecast.outlook(store, now, goals=True):
        if x["pool"] == o["pool"] or x["used"] >= 100 or alerts.stage(x, now):
            continue
        if x["kind"] == "goal" and pools.covers({"scope": x["scope"], "count": x["count"]}, **here):
            continue   # the work counts toward it already
        if x["kind"] == "subscription" and here.get("billing") == "plan" and x["harness"] == here.get("tool"):
            continue   # the plan the work is on
        out.append({"name": x["label"], "used_pct": round(x["used"]),
                    **({"likely_at_reset_pct": round(x["p50"])} if x.get("p50") is not None else {}),
                    **({"_usd": x["amount"]} if x["kind"] == "goal" and x["unit"] == "usd" else
                       {"_usd": x["budget_usd"]} if x["kind"] == "api" else {})})
    return sorted(out, key=lambda r: r.get("likely_at_reset_pct", r["used_pct"]))[:5]


def _range(lo, mid, hi):
    return {"p10": round(lo), "p50": round(mid), "p90": round(hi)}


def outcomes(store, o, g, f, here, now) -> dict:
    """For each choice, where this budget would likely end the period (% of it), from the forecast with the
    choice applied: a what-if from measured usage. A choice whose effect can't be measured has none."""
    from . import forecast, tasks
    used, out = o["used"], {}
    if o.get("p50") is not None:
        out["carry_on"] = _range(o["p10"], o["p50"], o["p90"])
    out["stop_now"] = out["wait_for_reset"] = _range(used, used, used)   # nothing more counts this period
    usd = o["unit"] == "usd" and o["amount"] > 0
    pid = here.get("project") or next(iter((g.get("scope") or {}).get("projects") or []), None)
    t = tasks.estimate(store, pid) if pid and usd else {}
    if t.get("enough"):   # one more task, the size this project's finished tasks measured
        out["finish_then_stop"] = {**_range(used + 100 * t["median_usd"] / o["amount"],
                                            used + 100 * t["median_usd"] / o["amount"],
                                            used + 100 * t["p90_usd"] / o["amount"]), "from_tasks": t["n"]}
    if "cheaper_model" in f and o.get("p50") is not None:
        ms = {m: (rate, day) for m, rate, _, day in _models(store, g, here.get("provider"), now)}
        cur, cheap = f["model_now"]["model"], f["cheaper_model"]["model"]
        day = sum(d for _, d in ms.values())
        if day > 0 and cur in ms and cheap in ms:
            share = ms[cur][1] / day   # the current model's part of the last day's spend, repriced
            cut = 1 - share + share * ms[cheap][0] / ms[cur][0]
            w = next((x for x in forecast.outlook(store, now, pool=o["pool"], cut=cut, cut_hours=f["hours_to_reset"])
                      if x.get("p50") is not None), None)
            if w:
                out["cheaper_model"] = _range(w["p10"], w["p50"], w["p90"])
    room = f["room_elsewhere"][0] if f["room_elsewhere"] else None
    if room and o.get("p50") is not None:
        out["move"] = {**_range(used, used, used)}
        if usd and room.get("_usd") and room.get("likely_at_reset_pct") is not None:
            moved = (o["p50"] - used) / 100 * o["amount"]   # the rest of this period's likely spend, there instead
            out["move"]["there_pct"] = round(room["likely_at_reset_pct"] + 100 * moved / room["_usd"])
    return out


def _past(store, gid, n=5) -> dict:
    """This budget's past calls and how their periods ended: measured feedback for the model."""
    entries = [d for d in log(store) if d["budget"] == gid]
    periods = {d["since"]: d["ended_pct"] for d in entries if "ended_pct" in d}
    return {"periods_ended": len(periods), "periods_within_budget": sum(1 for v in periods.values() if v <= 100),
            "last_calls": [{"call": d["do"], "used_pct_then": (d.get("facts") or {}).get("used_pct"),
                            **({"period_ended_pct": d["ended_pct"]} if "ended_pct" in d else {})}
                           for d in entries[-n:]]}


def _with(text, r):
    """A choice's words, with where it would likely leave the budget."""
    if not r:
        return text + " (its effect isn't measured)"
    end = f"likely ends the period at {r['p50']}%" + (f", range {r['p10']}–{r['p90']}%" if r["p90"] != r["p10"] else "")
    if "there_pct" in r:
        end += f", and the other at {r['there_pct']}%"
    return f"{text} ({end})"


def choices(f, g) -> tuple[dict, dict]:
    """The options that apply now, each with its words for the model."""
    do = {}
    up = f["used_pct"] >= 100
    if not up:
        do["carry_on"] = DO["carry_on"]
        do["finish_then_stop"] = DO["finish_then_stop"]
    if "cheaper_model" in f:
        do["cheaper_model"] = DO["cheaper_model"].format(
            model=f["cheaper_model"]["model"], rate=f["cheaper_model"]["usd_per_m_tokens"],
            now=f["model_now"]["model"], now_rate=f["model_now"]["usd_per_m_tokens"])
    if f["room_elsewhere"]:
        r = f["room_elsewhere"][0]
        do["move"] = DO["move"].format(name=r["name"], used=r["used_pct"])
    if g.get("period") in pools.PERIODS:
        do["wait_for_reset"] = DO["wait_for_reset"].format(span=alerts.span(f["hours_to_reset"] * 3600))
    do["stop_now"] = DO["stop_now"]
    ok = pools.allowed(g)   # the person's rule: only these may be picked
    do = {k: _with(v, (f.get("if_chosen") or {}).get(k)) for k, v in do.items() if k in ok}
    ask = {"nothing": ASK["nothing"], "raise_budget": ASK["raise_budget"]}
    if (g.get("scope") or {}).get("projects"):
        ask["pause_project"] = ASK["pause_project"]
    return do, ask


def ask_model(store, gid, o, g, here, now, key):
    """One call: the facts and choices out, the model's answer back, logged. None if it can't be reached."""
    f = facts(store, o, g, here, now)
    do, ask = choices(f, g)
    if not do:   # nothing the person allows applies now: guard's rule holds
        return {"at": now, "budget": gid, "error": "nothing_allowed"}
    if len(do) == 1:   # one allowed choice: it's the call, no model needed
        return _log(store, {**_entry(gid, g, here, f, do, ask, now), "do": next(iter(do)), "p": 1.0,
                            "ask": "nothing", "only_choice": True, "cost_usd": 0.0})
    body = {"model": MODEL, "state": f, "questions": {
        "do": {"type": "choice", "criteria": do,
               "instructions": "A spending budget for AI coding agents is running low. From these measured facts,"
                               " what should the agent's work that counts toward it do next? if_chosen gives,"
                               " for each choice, where the budget would likely end its period (% of it), from"
                               " the forecast with that choice applied; track_record gives past calls on this"
                               " budget and how their periods ended."},
        "ask": {"type": "choice", "criteria": ask,
                "instructions": "Is there a decision about money or the project's worth to put to the person?"
                                " Only they make those."}}}
    try:
        r = _post(body, key)
        a, q = r["answers"]["do"], r["answers"]["ask"]
    except Exception as e:   # unreachable, slow, out of credit, an answer we can't read
        return {"at": now, "budget": gid, "error": type(e).__name__}
    tokens = (r.get("usage") or {}).get("input_tokens") or 0
    return _log(store, {**_entry(gid, g, here, f, do, ask, now),
                        "do": a["choice"], "p": a.get("confidence"), "probabilities": a.get("probabilities"),
                        "ask": q["choice"], "ask_p": q.get("confidence"), "id": r.get("id"), "tokens": tokens,
                        "model": r.get("model"), "model_version": r.get("model_version"),
                        "cost_usd": tokens * USD_PER_M / 1e6})


def _entry(gid, g, here, f, do, ask, now) -> dict:
    since, until = pools.period(g, now)
    return {"at": now, "budget": gid, "label": g["label"], "since": since, "until": until, "amount": g.get("amount"),
            "unit": g.get("unit", "usd"), "here": here, "facts": f, "choices": do, "asks": ask}


def _log(store, d) -> dict:
    store.set_meta("decisions", ((store.meta("decisions") or []) + [d])[-LOG_MAX:])
    return d


def decision(store, gid, o, g, here, now=None, fresh=False):
    """The model's call for this budget and this work, cached; None when there's no key or it can't be
    reached (then guard's rule holds)."""
    now = now or time.time()
    key = api_key()
    if not key:
        return None
    cache = store.meta("decision_cache") or {}
    ck = f"{gid}|{g.get('amount')}|{','.join(pools.allowed(g))}|{here.get('tool')}|{here.get('provider')}|{here.get('billing')}"   # a raise asks again
    hit = cache.get(ck)
    if hit and not fresh and now - hit["at"] < (RETRY_SECONDS if "error" in hit else CACHE_SECONDS):
        return None if "error" in hit else hit
    d = ask_model(store, gid, o, g, here, now, key)
    cache[ck] = d
    store.set_meta("decision_cache", {k: v for k, v in cache.items() if now - v["at"] < CACHE_SECONDS})
    return None if "error" in d else d


def at_risk(store, here, now=None):
    """(id, budget, outlook) for budgets in decide that this work counts toward and that are at risk or used up."""
    from . import forecast
    now = now or time.time()
    wanted = {gid: g for gid, g in pools.goals_(store).items() if pools.mode(g) == "decide" and pools.covers(g, **here)}
    if not wanted:
        return []
    return [(o["pool"][5:], wanted[o["pool"][5:]], o) for o in forecast.outlook(store, now, goals=True)
            if o["kind"] == "goal" and o["pool"][5:] in wanted and (alerts.stage(o, now) or o["used"] >= 100)]


def calls(store, here, now=None) -> list[tuple[str, dict, dict, dict | None]]:
    """(id, budget, outlook, the model's call or None) for each budget in decide at risk for this work."""
    now = now or time.time()
    return [(gid, g, o, decision(store, gid, o, g, here, now)) for gid, g, o in at_risk(store, here, now)]


def words(d) -> str:
    """The call in a line: what, how sure, and the question for the person."""
    who = ("the only choice this budget allows" if d.get("only_choice")
           else f"the decision model's call, {100 * (d.get('p') or 0):.0f}% sure")
    text = f"{d['choices'].get(d['do'], d['do'])} ({who})."
    if d.get("ask") and d["ask"] != "nothing":
        text += f" {d['asks'][d['ask']]}."
    return text


def blocks(d, o) -> bool:
    """Whether new work stops: the model said so, or (no call) guard's rule: it's used up."""
    return d["do"] in BLOCKS if d else o["used"] >= 100


def log(store, now=None) -> list[dict]:
    """Every logged call, with how its period ended once it has."""
    now = now or time.time()
    entries = store.meta("decisions") or []
    changed = False
    for d in entries:
        if "ended_pct" not in d and d.get("until") and d["until"] <= now and d.get("amount"):
            g = (pools.goals_(store).get(d["budget"]) or {"label": d["label"], "scope": {}, "unit": d["unit"]})
            pool = pools.Pool(f"goal:{d['budget']}", "all", "goal", f"goal:{d['budget']}", g)
            d["ended_pct"] = round(100 * pools.spent(store, pool, d["since"], d["until"]) / d["amount"], 1)
            changed = True
    if changed:
        store.set_meta("decisions", entries)
    return entries


def record(store, gid, now=None) -> dict:
    """A budget's track record: of the periods the model made calls in that have ended, how many ended within
    the budget; and its calls, by answer."""
    entries = [d for d in log(store, now) if d["budget"] == gid]
    periods = {d["since"]: d["ended_pct"] for d in entries if "ended_pct" in d}
    by = {}
    for d in entries:
        by[d["do"]] = by.get(d["do"], 0) + 1
    return {"calls": len(entries), "by_answer": by, "periods_ended": len(periods),
            "periods_within": sum(1 for v in periods.values() if v <= 100),
            "cost_usd": sum(d.get("cost_usd") or 0 for d in entries),
            "last": entries[-1] if entries else None}


def active(store, o, now=None) -> dict | None:
    """The call that needs the person now: this period's latest (from any machine), while the budget is still at
    risk or used up, when it stops the work or has a question for them."""
    now = now or time.time()
    gid = o["pool"][5:]
    d = next((d for d in reversed(log(store, now)) if d["budget"] == gid), None)
    if not d or d.get("until", 0) <= now or d.get("amount") != o.get("amount"):
        return None   # its period is over, or the budget was changed since
    if not (alerts.stage(o, now) or o["used"] >= 100):
        return None
    return d if d["do"] in BLOCKS or d.get("ask", "nothing") != "nothing" else None


def view(store, o, now=None, n=10) -> dict | None:
    """For the dashboard: a budget's latest call, whether it needs the person now, its last n calls with what was
    sent, and its track record."""
    r = record(store, o["pool"][5:], now)
    if not r["last"]:
        return None
    a = active(store, o, now)
    one = lambda d: {"at": d["at"], "machine": d.get("machine"), "do": d["do"], "short": SHORT.get(d["do"], d["do"]),
                     "text": words(d), "p": d.get("p"), "ask": d.get("ask"), "sent": d.get("facts"),
                     "choices": list(d.get("choices") or {}), "ended_pct": d.get("ended_pct"),
                     "next": runner_up(d), "model_version": d.get("model_version"), "would": would(d)}
    entries = [d for d in log(store, now) if d["budget"] == o["pool"][5:]]
    return {"calls": r["calls"], "periods_ended": r["periods_ended"], "periods_within": r["periods_within"],
            "cost_usd": r["cost_usd"], "last": words(r["last"]), "at": r["last"]["at"],
            "now": {**one(a), "blocks": a["do"] in BLOCKS, "pause": "pause_project" in a.get("asks", {})} if a else None,
            "history": [one(d) for d in reversed(entries[-n:])]}


def unsent(store) -> list[dict]:
    """Calls made here that the hub hasn't had yet."""
    after = store.meta("pushed:decisions") or 0
    return [{k: v for k, v in d.items() if k != "here"} for d in store.meta("decisions") or [] if d["at"] > after]


def merge(store, machine, entries) -> int:
    """On the hub: a machine's calls, checked, added to every machine's."""
    ok = []
    for d in entries if isinstance(entries, list) else []:
        if not (isinstance(d, dict) and isinstance(d.get("at"), (int, float)) and isinstance(d.get("budget"), str)
                and isinstance(d.get("do"), str) and isinstance(d.get("choices"), dict)
                and isinstance(d.get("asks"), dict) and isinstance(d.get("since"), (int, float))
                and isinstance(d.get("until"), (int, float)) and len(json.dumps(d)) <= 16384):
            continue
        ok.append({**{k: v for k, v in d.items() if k not in ("here", "ended_pct")}, "machine": machine})
    have = store.meta("decisions") or []
    seen = {(d.get("machine"), d["at"], d["budget"]) for d in have}
    new = [d for d in ok if (d["machine"], d["at"], d["budget"]) not in seen]
    if new:
        store.set_meta("decisions", sorted(have + new, key=lambda d: d["at"])[-HUB_MAX:])
    return len(new)


def would(d) -> str | None:
    """Each offered choice with where the budget would likely end the period, as sent to the model."""
    f = d.get("facts") or {}
    ifs, room = f.get("if_chosen"), (f.get("room_elsewhere") or [{}])[0]
    if ifs is None:
        return None   # a call from before the what-ifs were sent
    out = []
    for k in d.get("choices") or {}:
        name = {"cheaper_model": f"cheaper model{' (' + f['cheaper_model']['model'] + ')' if f.get('cheaper_model') else ''}",
                "move": f"move{' to ' + room['name'] if room.get('name') else ''}"}.get(k, k.replace("_", " "))
        r = ifs.get(k)
        if not r:
            out.append(f"{name}: not measured")
            continue
        end = f"{r['p50']}%" + (f" ({r['p10']}–{r['p90']}%)" if r["p90"] != r["p10"] else "")
        out.append(f"{name} → {end}" + (f", {room.get('name', 'the other')} {r['there_pct']}%" if "there_pct" in r else ""))
    return " · ".join(out) or None


def runner_up(d) -> dict | None:
    """The second most likely choice, with its probability: how close the call was."""
    ps = sorted(((p, k) for k, p in (d.get("probabilities") or {}).items() if k != d["do"]), reverse=True)
    return {"do": ps[0][1], "short": SHORT.get(ps[0][1], ps[0][1]), "p": ps[0][0]} if ps else None
