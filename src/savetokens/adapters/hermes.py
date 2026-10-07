"""Hermes adapter: backfill from state.db and the token-tracker calls.db, plus live hook handlers.

Hermes's pre_tool_call fails closed (a timeout or exception blocks the tool),
so it is registered only when blocking is opted in, and every handler here
swallows its own errors. Warnings go out through transform_tool_result, which
runs inside the tool loop; pre_llm_call fires only once per user turn and
would miss a loop that runs within one turn.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from .. import guard, pricing
from ..store import Store, ToolEvent, UsageEvent, load_config
from ..toolkinds import args_hash, classify

HARNESS = "hermes"


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _blended_rates(con):
    """$ per token by model from Hermes's own cost records, for models missing from our price table."""
    rates = {}
    for model, usd, tok in con.execute(
            "SELECT model, SUM(CASE WHEN actual_cost_usd > 0 THEN actual_cost_usd ELSE estimated_cost_usd END),"
            " SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens)"
            " FROM session_model_usage GROUP BY model"):
        if usd and tok:
            rates[model] = usd / tok
    return rates


def backfill(store: Store, home: Path | None = None) -> int:
    """Import per-call rows from token-tracker where they exist, and session aggregates elsewhere.

    Sessions with live savetokens or token-tracker rows skip their state.db aggregate, so
    nothing is counted twice.
    """
    home = home or hermes_home()
    state, calls = home / "state.db", home / "plugin-data" / "token-tracker" / "calls.db"
    rates, added = {}, 0
    if state.exists():
        con = _ro(state)
        try:
            rates = _blended_rates(con)
        except sqlite3.Error:
            pass
        con.close()
    if calls.exists():
        con = _ro(calls)
        con.row_factory = sqlite3.Row
        events = []
        for r in con.execute("SELECT * FROM calls WHERE usage_missing = 0"):
            ev = UsageEvent(HARNESS, r["session_id"] or "unknown", r["api_request_id"] or f"tt:{r['id']}",
                            r["ended_at"], model=r["model"], input=r["input_tokens"], output=r["output_tokens"],
                            cache_read=r["cache_read_tokens"], cache_write_5m=r["cache_write_tokens"],
                            provider=provider_label(_col(r, "provider") or _col(r, "billing_provider"),
                                                    _col(r, "base_url")))
            own = _col(r, "actual_cost_usd") or _col(r, "cost_usd") or _col(r, "estimated_cost_usd")
            if own:      # the call's own cost as Hermes recorded it beats a per-token average
                ev.cost_usd, ev.cost_source = float(own), "hermes"
            else:
                _price(ev, rates)
            events.append(ev)
        con.close()
        added += store.add_usage(events)
    live = {r[0] for r in store.conn.execute(
        "SELECT DISTINCT session_id FROM usage WHERE harness = ? AND request_id NOT LIKE 'smu:%'", (HARNESS,))}
    if state.exists():
        con = _ro(state)
        con.row_factory = sqlite3.Row
        try:
            rows = list(con.execute("SELECT * FROM session_model_usage"))
        except sqlite3.Error:
            rows = []
        con.close()
        events = []
        for r in rows:
            if r["session_id"] in live:
                continue
            usd = r["actual_cost_usd"] or r["estimated_cost_usd"] or None
            events.append(UsageEvent(
                HARNESS, r["session_id"], f"smu:{r['session_id']}:{r['model']}:{r['billing_provider']}:{r['task']}",
                r["last_seen"] or r["first_seen"] or 0, model=r["model"], subagent=bool(r["task"]),
                input=r["input_tokens"], output=r["output_tokens"], cache_read=r["cache_read_tokens"],
                cache_write_5m=r["cache_write_tokens"], cost_usd=usd, cost_source="hermes" if usd else None,
                provider=_col(r, "billing_provider")))
        # aggregates grow as sessions continue: replace rather than ignore
        store.conn.executemany("DELETE FROM usage WHERE harness = ? AND request_id = ?",
                               [(HARNESS, e.request_id) for e in events])
        added += store.add_usage(events)
        store.conn.execute(f"DELETE FROM usage WHERE harness = ? AND request_id LIKE 'smu:%' AND session_id IN"
                           f" ({','.join('?' * len(live))})", (HARNESS, *live))
        store.conn.commit()
    return added


def _col(row, name):
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _price(ev: UsageEvent, rates=None):
    ev.cost_usd = pricing.cost(ev.model, input=ev.input, output=ev.output, cache_read=ev.cache_read,
                               cache_write_5m=ev.cache_write_5m)
    if ev.cost_usd is not None:
        ev.cost_source = "price_table"
    elif rates and ev.model in rates:
        ev.cost_usd = rates[ev.model] * (ev.input + ev.output + ev.cache_read + ev.cache_write_5m)
        ev.cost_source = "hermes_blended"


def _failed(result) -> bool:
    try:
        d = json.loads(result) if isinstance(result, str) else result
    except ValueError:
        return False
    if not isinstance(d, dict):
        return False
    if d.get("error") or d.get("success") is False or d.get("status") == "error":
        return True
    code = d.get("exit_code", d.get("returncode"))
    return isinstance(code, int) and code != 0


# ── live hook handlers (called from the Hermes plugin) ───────────────────────

def provider_label(provider=None, base_url=None) -> str | None:
    """Who bills the call: Hermes's provider name, or the API host for custom endpoints."""
    from urllib.parse import urlparse
    if provider and provider not in ("custom", "openai-compat", "auto", "local"):
        return provider
    host = urlparse(base_url).hostname if base_url else None
    return host or provider


def on_api_request(store: Store, *, session_id=None, task_id=None, model=None, response_model=None,
                   ended_at=None, usage=None, api_request_id=None, provider=None, base_url=None,
                   cost_usd=None, cost_status=None, cost_source=None, **_):
    """cost_usd/cost_status come from Hermes's own estimator (see the plugin): the provider's reported
    cost where it has one, its price catalogues otherwise, and "included" for subscription routes."""
    if not isinstance(usage, dict):
        return
    sid = session_id or task_id or "unknown"
    ev = UsageEvent(HARNESS, sid, api_request_id or f"live:{sid}:{ended_at or time.time()}",
                    float(ended_at or time.time()), model=response_model or model,
                    input=int(usage.get("input_tokens") or 0), output=int(usage.get("output_tokens") or 0),
                    cache_read=int(usage.get("cache_read_tokens") or 0),
                    cache_write_5m=int(usage.get("cache_write_tokens") or 0),
                    provider=provider_label(provider, base_url))
    included = cost_status == "included"
    if cost_usd is not None and not included:
        ev.cost_usd, ev.cost_source = float(cost_usd), f"hermes:{cost_source or 'estimate'}"
    else:
        _price(ev)
    store.add_usage([ev])
    from .. import limits
    limits.record_session(store, HARNESS, sid, billing=limits.SUBSCRIPTION if included else limits.API,
                          model=ev.model, source="hermes")


def on_tool_result(store: Store, *, tool_name="?", args=None, result=None, task_id=None, session_id=None,
                   tool_call_id=None, cfg=None, **_):
    """Record the call; return a one-line warning to append to the tool result, or None."""
    sid = session_id or task_id or "unknown"
    kind, target = classify(tool_name, args)
    store.add_tools([ToolEvent(HARNESS, sid, time.time(), tool_name, kind, tool_use_id=tool_call_id,
                               args_hash=args_hash(args), target=target, ok=not _failed(result),
                               output_chars=len(result) if isinstance(result, str) else None)])
    alerts = guard.evaluate(store, HARNESS, sid, sid, cfg or load_config())
    msg = " ".join(a.message for a in alerts)
    return msg or None


def on_pre_tool(store: Store, *, tool_name="?", args=None, task_id=None, session_id=None, cfg=None, **_):
    sid = session_id or task_id or "unknown"
    kind, _target = classify(tool_name, args)
    reason = guard.block_reason(store, HARNESS, sid, sid, tool_name, args_hash(args), kind, cfg)
    return {"action": "block", "message": reason} if reason else None


def on_pre_llm(store: Store, *, session_id=None, task_id=None, is_first_turn=False, cfg=None, **_):
    """pre_llm_call: the briefing on a session's first turn, a budget nudge on later ones. Returns context or None."""
    from .. import steer
    sid = session_id or task_id or "unknown"
    if is_first_turn:
        text = steer.briefing(store, cwd=None, session_id=sid, cfg=cfg, harness=HARNESS)
    else:
        n = steer.nudge(store, sid, HARNESS, cfg=cfg)
        text = n[0] if n else None
    return {"context": text} if text else None
