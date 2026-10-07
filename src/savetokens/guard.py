"""Runaway guard: shared rules evaluated inside harness hooks.

Every rule fires at most once per (session, rule, key), and messages are one
line, because a warning lands in the agent's context and costs tokens too.
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass

from .store import Store

AGENT = "agent"   # warning goes to the agent and the user
USER = "user"     # warning goes to the user only (the agent can't act on it)


@dataclass
class Alert:
    rule: str
    key: str
    message: str
    audience: str = AGENT
    burn: float | None = None


def _burn(store: Store, session_id, cfg, now):
    window = cfg["burn_window_min"] * 60
    events = [e for e in store.usage(session_id=session_id) if e.cost_usd is not None]
    if not events:
        return None
    recent = sum(e.cost_usd for e in events if now - window < e.ts <= now)
    start = events[0].ts
    buckets = {}
    for e in events:
        if e.ts <= now - window:
            buckets.setdefault(int((e.ts - start) // window), 0.0)
            buckets[int((e.ts - start) // window)] += e.cost_usd
    floor = cfg["burn_floor_usd"]
    if len(buckets) >= 3:
        usual = statistics.median(buckets.values())
        threshold = max(cfg["burn_multiple"] * usual, floor)
    else:
        usual, threshold = None, floor * cfg["burn_multiple"]
    if recent <= threshold:
        return None
    pace = f", {recent / usual:.0f}x its usual pace" if usual else ""
    return Alert("burn", str(int(now // 1800)),
                 f"savetokens: this session spent ${recent:.2f} in the last {cfg['burn_window_min']} min{pace}."
                 " Check for a loop or a runaway subagent before continuing.",
                 burn=recent / (window / 60))


def _same_run(tools):
    """Length of the trailing run of identical (tool, args) calls."""
    if not tools:
        return 0
    last = (tools[-1].tool, tools[-1].args_hash)
    n = 0
    for t in reversed(tools):
        if (t.tool, t.args_hash) != last:
            break
        n += 1
    return n


def _loop(tools, cfg):
    n = _same_run(tools)
    if n >= cfg["loop_repeats"]:
        t = tools[-1]
        return Alert("loop", t.args_hash or t.tool,
                     f"savetokens: {t.tool} was called {n} times in a row with the same arguments."
                     " Repeating it will not change the result; change approach or ask the user.")
    return None


def _reread(tools, cfg):
    reads = {}
    for t in tools:
        if not t.target:
            continue
        if t.kind == "edit":
            reads.pop(t.target, None)
        elif t.kind == "read":
            reads[t.target] = reads.get(t.target, 0) + 1
    if tools and tools[-1].kind == "read" and tools[-1].target:
        target = tools[-1].target
        n = reads.get(target, 0)
        if n >= cfg["reread_limit"]:
            return Alert("reread", f"{target}:{n // cfg['reread_limit']}",
                         f"savetokens: {target} has been read {n} times without being changed."
                         " Its contents are already in context; avoid re-reading it.")
    return None


def _failing_streak(tools):
    """Consecutive failed test runs at the end, with no edit in between."""
    n = 0
    for t in reversed(tools):
        if t.kind == "edit":
            break
        if t.kind == "test":
            if t.ok is False:
                n += 1
            elif t.ok is True:
                break
    return n


def _tests(tools, cfg):
    n = _failing_streak(tools)
    if n >= cfg["failing_tests"] and tools and tools[-1].kind == "test":
        first = next(t for t in reversed(tools) if t.kind == "test")
        return Alert("failing_tests", f"{first.args_hash}:{n // cfg['failing_tests']}",
                     f"savetokens: tests failed {n} runs in a row with no code change in between."
                     " Re-running will not change the result; read the failure and edit, or ask the user.")
    return None


IDLE_GAP = 3600   # a pause this long usually means a new task, and the prompt cache has gone cold


def _context(store: Store, session_id, cfg):
    """Warn the user (not the agent) when every turn re-reads a large context.

    Silent when the user's own auto-compact window is about to fire anyway. After a
    long pause, suggests /clear: the cache is cold, so resuming re-pays the whole
    context, and the task has often changed.
    """
    from . import compaction
    main = [e for e in store.usage(session_id=session_id) if not e.subagent]
    if not main:
        return None
    last = main[-1]
    ctx = last.context_tokens
    if ctx < cfg["context_tokens"]:
        return None
    window = compaction.configured_window(last.model)
    if window and ctx >= 0.85 * window:
        return None
    step = cfg["context_tokens"]
    idle = len(main) >= 2 and last.ts - main[-2].ts >= IDLE_GAP
    if idle:
        msg = (f"savetokens: resumed after a break with {ctx // 1000}k tokens of context and a cold cache."
               " If this is a new task, /clear is far cheaper than carrying the old context.")
        return Alert("context", f"idle:{int(last.ts // IDLE_GAP)}", msg, audience=USER)
    hint = ("" if window else
            " Auto-compact only fires near the full window; `savetokens fixes plan autocompact-window` moves it earlier.")
    return Alert("context", str(ctx // step),
                 f"savetokens: context is {ctx // 1000}k tokens and every turn re-reads all of it."
                 f" /clear if the task changed, otherwise /compact.{hint}", audience=USER)


SURGE_HOURS = 2   # backtest: 2 hours in a row above p99 cut alarms ~3x vs 1 hour, keeping most catches


def surge(store: Store, now, cfg, session_id=None):
    """Machine-wide spend above the forecast's 99th percentile for two complete hours in a row.

    Catches runaways in any session or scheduled job, not only the one making the current
    tool call, with thresholds that know the time of day (from Ephemeris when connected).
    `savetokens backtest` scores this rule on your own history.
    """
    from collections import defaultdict
    from . import forecast, windows
    h = int(windows.hour_floor(now))
    hours = [h - k * 3600 for k in range(SURGE_HOURS, 0, -1)]
    events = [e for e in store.usage(since=hours[0], until=h) if e.cost_usd]
    floor = cfg["burn_floor_usd"]
    if sum(e.cost_usd for e in events) <= floor * SURGE_HOURS:
        return None
    unit_of = windows.classifier(store)
    spent, by_session = defaultdict(float), defaultdict(float)
    for e in events:
        u = unit_of(e)
        if u:
            spent[(u, windows.hour_floor(e.ts))] += e.cost_usd
            by_session[e.session_id] += e.cost_usd
    src = forecast.preferred_source(store)
    for unit in windows.UNITS:
        th = windows.surge_thresholds(store, src, unit)
        if not all(x in th for x in hours):
            continue
        if not all(spent[(unit, x)] > max(th[x], floor) for x in hours):
            continue
        total = sum(spent[(unit, x)] for x in hours)
        top = max(by_session, key=by_session.get)
        mine = session_id is not None and top == session_id
        where = ("This session is the biggest spender: check for a loop or a runaway subagent." if mine else
                 f"Biggest spender: session {top[:8]} (${by_session[top]:.2f}). Check for a runaway agent or"
                 " scheduled job.")
        return Alert("surge", f"{unit}:{h}",
                     f"savetokens: this machine spent ${total:.2f} in the last {SURGE_HOURS} hours, above the {src}"
                     f" forecast's 99th percentile in each hour (${', $'.join(f'{th[x]:.2f}' for x in hours)})."
                     f" {where}", audience=AGENT if mine else USER, burn=total / (60 * SURGE_HOURS))
    return None


def evaluate(store: Store, harness: str, session_id: str, tool_session: str | None = None,
             cfg: dict | None = None, now: float | None = None) -> list[Alert]:
    """New alerts for this session (already-fired ones are suppressed and not returned)."""
    from .store import load_config
    from . import steer
    cfg = cfg or load_config()
    cfg = steer.adjust_guard(cfg, steer.guard_mode(store, cfg))
    now = now or time.time()
    tools = store.tools(session_id=tool_session or session_id, limit=400)
    candidates = [_burn(store, session_id, cfg, now), _loop(tools, cfg), _reread(tools, cfg),
                  _tests(tools, cfg), _context(store, session_id, cfg), _surge_safe(store, now, cfg, session_id)]
    fresh = []
    for a in candidates:
        if a and store.add_alert(harness, session_id, a.rule, a.key, "warn", a.message, a.burn, ts=now):
            fresh.append(a)
    if any(a.rule in ("failing_tests", "loop") for a in fresh):
        try:
            from . import levers
            rule = next(a.rule for a in fresh if a.rule in ("failing_tests", "loop"))
            undone = levers.on_trouble(store, harness, rule, now)
            if undone:
                fresh.append(Alert("levers", str(int(now)), "savetokens: switched back from " + ", ".join(undone)
                                   + " for new sessions and subagents, since this looks like it needs the stronger"
                                   " settings. Use full-strength subagents for the rest of this task.", AGENT))
        except Exception:
            pass
    return fresh


def _surge_safe(store, now, cfg, session_id):
    try:
        return surge(store, now, cfg, session_id)
    except Exception:   # a forecast problem must never break the guard
        return None


def block_reason(store: Store, harness, session_id, tool_session, tool, args_hash, kind, cfg=None):
    """With blocking enabled, why the next call should be denied (or None)."""
    from .store import load_config
    from . import steer
    cfg = cfg or load_config()
    if not cfg.get("block"):
        return None
    cfg = steer.adjust_guard(cfg, steer.guard_mode(store, cfg))
    tools = store.tools(session_id=tool_session or session_id, limit=400)
    run = _same_run(tools)
    if tools and (tools[-1].tool, tools[-1].args_hash) == (tool, args_hash) and run >= cfg["loop_repeats"]:
        msg = (f"savetokens blocked this call: {tool} already ran {run} times in a row with these arguments."
               " Try a different approach or ask the user.")
        store.add_alert(harness, session_id, "loop", f"block:{args_hash}:{run}", "block", msg)
        return msg
    if kind == "test" and _failing_streak(tools) >= cfg["failing_tests"]:
        msg = ("savetokens blocked this test run: the last runs failed with no code change since."
               " Edit the code first or ask the user.")
        store.add_alert(harness, session_id, "failing_tests", f"block:{len(tools)}", "block", msg)
        return msg
    return None
