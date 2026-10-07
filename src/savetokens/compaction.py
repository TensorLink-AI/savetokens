"""Compaction: when it fires, what it costs, and the auto-compact window.

Claude Code auto-compacts when the context reaches its auto-compact window. On
1M-context models the default ("auto") sits near the full window, so most of a
long session re-reads a very large context every turn. `autoCompactWindow`
(100k-1M tokens, top-level or per model under `modelOverrides`) or the
CLAUDE_CODE_AUTO_COMPACT_WINDOW variable moves it earlier.
"""
from __future__ import annotations

import json
import os
import statistics
from collections import defaultdict

from . import pricing
from .store import Store

MIN_WINDOW, MAX_WINDOW = 100_000, 1_000_000
SUGGEST_MIN, SUGGEST_MAX, DEFAULT_SUGGESTION = 200_000, 600_000, 400_000


def _settings():
    from .adapters.claude_code import claude_home
    try:
        return json.loads((claude_home() / "settings.json").read_text())
    except (OSError, ValueError):
        return {}


def configured_window(model=None):
    """The auto-compact window in tokens if the user set one, else None ("auto")."""
    env = os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    if env and env.isdigit():
        return int(env)
    s = _settings()
    if model:
        for name, o in (s.get("modelOverrides") or {}).items():
            if pricing.normalize(name) == pricing.normalize(model) and isinstance(o, dict):
                v = o.get("autoCompactWindow")
                if isinstance(v, int):
                    return v
    v = s.get("autoCompactWindow")
    return v if isinstance(v, int) else None


def events(store: Store, since=0):
    return list(store.conn.execute("SELECT * FROM compactions WHERE ts >= ? ORDER BY ts", (since,)))


def suggested_window(store: Store):
    """Where you compact by hand, rounded to 50k and kept between 200k and 600k."""
    manual = [r["pre_tokens"] for r in events(store) if r["trigger"] == "manual" and r["pre_tokens"]]
    if len(manual) < 3:
        return DEFAULT_SUGGESTION, len(manual), None
    med = statistics.median(manual)
    return int(min(SUGGEST_MAX, max(SUGGEST_MIN, round(med / 50_000) * 50_000))), len(manual), med


def stats(store: Store, since=0):
    """Counts and context size by trigger, and what compactions cost (API-equivalent $).

    Cost per compaction = the summarising pass over the old context (priced as a cache read)
    + the cache write of the first turn after it (rebuilding the new, shorter prefix).
    """
    rows = events(store, since)
    by = defaultdict(list)
    cost = 0.0
    for r in rows:
        by[r["trigger"] or "unknown"].append(r["pre_tokens"] or 0)
        after = store.conn.execute("SELECT * FROM usage WHERE session_id = ? AND ts > ? AND subagent = 0"
                                   " ORDER BY ts LIMIT 1", (r["session_id"], r["ts"])).fetchone()
        if after is None:
            continue
        rates = pricing.rates(after["model"])
        if not rates:
            continue
        cost += (r["pre_tokens"] or 0) * rates[2] / 1e6
        cost += (after["cache_write_5m"] * 1.25 + after["cache_write_1h"] * 2.0) * rates[0] / 1e6
    return {t: {"n": len(v), "median": statistics.median(v) if v else 0, "max": max(v) if v else 0}
            for t, v in by.items()}, cost, len(rows)


def carry_above(store: Store, window, since=0):
    """Turns over `window` and the re-reading above it (API-equivalent $): what an earlier window could save."""
    turns, usd = 0, 0.0
    for e in store.usage(since=since):
        if e.subagent or e.harness != "claude-code" or e.context_tokens <= window:
            continue
        r = pricing.rates(e.model)
        if r:
            turns += 1
            usd += (e.context_tokens - window) * r[2] / 1e6
    return turns, usd
