"""Hermes Agent capture: pay-as-you-go API usage from Hermes's own session store.

Hermes runs on any provider's API (OpenRouter, Anthropic, OpenAI, Nous, a local
server, ...) and keeps running totals per session and model in
~/.hermes/state.db (and each profile's, ~/.hermes/profiles/*/state.db): tokens,
and its own estimated or actual cost (`session_model_usage`). Each read adds
what those totals grew by since the last read. Its usage is billed "api" and
counts against the Hermes API budget (`savetokens api hermes --budget ...`).

Usage Hermes marks as included in a subscription (a ChatGPT plan through
Codex's login) is captured but counts against no budget: that plan's own
meter already has it.

Counts only: never prompts, replies or tool output.
"""
from __future__ import annotations

import os
import sqlite3
import urllib.parse
from pathlib import Path

from . import pricing
from .store import Store, Usage

HARNESS = "hermes"
INCLUDED = "subscription_included"
MAX_SPLIT_HOURS = 48       # usage first seen in a long session is spread over its hours, up to this many
COLS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")


GENERIC = {"", "custom", "unknown", "openai-compatible", "local"}


def provider_name(provider, base_url="") -> str:
    """Hermes's provider, or for a custom endpoint the API's host (api.synthetic.new -> synthetic.new)."""
    p = (provider or "").strip().lower()
    if p not in GENERIC:
        return p
    host = (urllib.parse.urlparse(base_url or "").hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        return "local"
    for prefix in ("api.", "inference.", "llm."):
        host = host.removeprefix(prefix)
    return host or p or "unknown"


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def databases(root: Path | None = None):
    root = root or hermes_home()
    found = [root / "state.db"] + sorted((root / "profiles").glob("*/state.db"))
    return [p for p in found if p.is_file()]


def _connect(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _rows(conn, since):
    """Running totals per (session, model, provider, task), newest first seen after `since`."""
    t = _tables(conn)
    if "sessions" not in t:
        return []
    if "session_model_usage" in t:
        return conn.execute(
            "SELECT u.session_id, u.model, u.billing_provider, u.billing_base_url, u.billing_mode, u.task,"
            " u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens, u.reasoning_tokens,"
            " u.estimated_cost_usd, u.actual_cost_usd, u.first_seen, u.last_seen,"
            " s.cwd, s.parent_session_id, s.started_at FROM session_model_usage u"
            " LEFT JOIN sessions s ON s.id = u.session_id WHERE COALESCE(u.last_seen, s.started_at) >= ?",
            (since,)).fetchall()
    # older Hermes: one running total per session
    return conn.execute(
        "SELECT id AS session_id, model, billing_provider, billing_base_url, billing_mode, '' AS task,"
        " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,"
        " estimated_cost_usd, actual_cost_usd, started_at AS first_seen, COALESCE(ended_at, started_at) AS last_seen,"
        " cwd, parent_session_id, started_at FROM sessions WHERE COALESCE(ended_at, started_at) >= ?",
        (since,)).fetchall()


def _cost(r):
    actual, est = r["actual_cost_usd"] or 0.0, r["estimated_cost_usd"] or 0.0
    return actual if actual > 0 else est


def ingest_db(store: Store, path: Path) -> int:
    path = Path(path)
    key = f"hermes_db:{path.parent.name}"
    state = store.meta(key) or {}
    seen, since = state.get("seen", {}), state.get("since", 0)
    try:
        conn = _connect(path)
        try:
            rows = _rows(conn, since - 3600)       # an hour of slack for totals written late
        finally:
            conn.close()
    except sqlite3.Error:
        return 0
    usage, newest = [], since
    for r in rows:
        sid = r["session_id"]
        k = "|".join(str(r[c] or "") for c in ("session_id", "model", "billing_provider", "billing_base_url",
                                                "billing_mode", "task"))
        now_tot = [int(r[c] or 0) for c in COLS] + [_cost(r)]
        known, before = k in seen, seen.get(k) or [0] * len(now_tot)
        d = [max(0, a - b) for a, b in zip(now_tot, before)]
        seen[k] = now_tot
        last = r["last_seen"] or r["started_at"]
        if last is None:
            continue
        newest = max(newest, last)
        if not any(d[:4]):
            continue
        first = r["first_seen"] or last
        # first sight of a long session: spread over its hours, so history isn't one spike
        parts = 1 if known else max(1, min(MAX_SPLIT_HOURS, int((last - first) // 3600) + 1))
        model = r["model"] or None
        bill = None if r["billing_mode"] == INCLUDED else "api"
        tokens_in, out, cr, cw, reason, usd = d
        if not usd and bill == "api":
            usd = pricing.cost(model and model.split("/")[-1], input=tokens_in, output=out + reason, cache_read=cr, cache_write_5m=cw)
        elif bill is None:
            usd = None
        for i in range(parts):
            ts = last if parts == 1 else first + (last - first) * (i + 0.5) / parts
            share = lambda v: v // parts + (1 if i < v % parts else 0)
            usage.append(Usage(HARNESS, sid, f"{k}:{sum(now_tot[:4])}:{i}", ts, model=model,
                               subagent=bool(r["parent_session_id"] or r["task"]),
                               input=share(tokens_in), cache_read=share(cr), cache_write_5m=share(cw),
                               output=share(out + reason), cost_usd=None if usd is None else usd / parts,
                               project=Path(r["cwd"]).name if r["cwd"] else None, billing=bill,
                               provider=provider_name(r["billing_provider"], r["billing_base_url"])))
    added = store.add_usage(usage)
    store.set_meta(key, {"seen": seen, "since": newest})
    store.conn.commit()
    return added


def backfill(store: Store, root: Path | None = None) -> int:
    return sum(ingest_db(store, p) for p in databases(root))
