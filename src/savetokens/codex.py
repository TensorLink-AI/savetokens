"""Codex capture: usage and limit readings from Codex's own session logs.

On every turn Codex records the tokens used and, on a ChatGPT plan, its limit
meter (`rate_limits`: % used, window length, reset time) in
~/.codex/sessions/**/rollout-*.jsonl. The meter goes to the meter table under
harness "codex", the same as Claude Code's statusline readings.

A session on an API key has no plan meter: its usage is marked billing "api"
and counts against the Codex API budget, if one is set. So is one on another
provider (the session's `model_provider`: a custom endpoint, a local server):
recorded under that provider, as Codex records it, never as OpenAI's plan. Codex models have no
built-in price; add one with `savetokens price` for API budgets.

Counts only: never prompts, replies or tool output.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from . import pricing
from .store import Store, Usage

HARNESS = "codex"
WINDOWS = {300: "five_hour", 10080: "seven_day"}
MAIN_LIMIT = "codex"       # other limit ids are per-model extras (e.g. a fast model's own limit)
THIN_SECONDS = 300         # one reading per window per 5 minutes unless the % moves


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def auth_mode() -> str | None:
    """'chatgpt' (a plan) or 'apikey', from Codex's auth file (only that field is read)."""
    try:
        return json.loads((codex_home() / "auth.json").read_text()).get("auth_mode")
    except (OSError, ValueError):
        return None


LOCAL = {"oss", "ollama", "lmstudio"}


def provider_of(pid) -> str | None:
    """Who serves a session, from its model_provider: None for OpenAI (Codex's own), else the host of the
    endpoint config.toml gives it (api.example.com -> example.com), else the id Codex records."""
    pid = (pid or "").strip()
    if not pid or pid == "openai":
        return None
    if pid.lower() in LOCAL:
        return "local"
    from .hermes import provider_name
    try:
        import tomllib
        conf = tomllib.loads((codex_home() / "config.toml").read_text())
        url = ((conf.get("model_providers") or {}).get(pid) or {}).get("base_url")
    except (OSError, ValueError):
        url = None
    return provider_name("custom", url) if url else pid.lower()


def fill_providers(store: Store, root: Path | None = None) -> int:
    """Once: requests read before providers were, from sessions on another provider, put under it and billed
    as API. Earlier ones were counted as OpenAI's plan."""
    if store.meta("codex_providers_filled"):
        return 0
    n = 0
    for path in transcripts(root):
        try:
            with open(path, "rb") as f:
                d = json.loads(f.readline())
        except (OSError, ValueError):
            continue
        p = d.get("payload") or {}
        who = provider_of(p.get("model_provider")) if d.get("type") == "session_meta" else None
        sid = p.get("id") or p.get("session_id")
        if who and sid:
            n += store.relabel(HARNESS, sid, who)
    store.set_meta("codex_providers_filled", True)
    return n


def _ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ingest_file(store: Store, path: Path, billing=None) -> int:
    """New complete lines of one rollout file. billing: "api" forces API billing for this machine's Codex."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    offset, _ = store.get_offset(path)
    if size < offset:
        offset = 0
    if size == offset:
        return 0
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n") + 1
    state = store.meta(f"codex_file:{path.name}") or {}
    sid, model, project, sub = state.get("sid"), state.get("model"), state.get("project"), state.get("sub", False)
    cwd, branch = state.get("cwd"), state.get("branch")
    planned = state.get("planned", False)          # this session has shown a plan meter
    who = state.get("provider")                     # another provider than OpenAI: provider_of
    acct = state.get("account")                     # the ChatGPT account and user it ran as, hashed
    usage, readings = [], []
    last_total = state.get("total")
    for line in data[:end].splitlines():
        if b"token_count" not in line and b"turn_context" not in line and b"session_meta" not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        kind, p = d.get("type"), d.get("payload") or {}
        if kind == "session_meta":
            sid = p.get("id") or p.get("session_id") or sid
            project = Path(p["cwd"]).name if p.get("cwd") else project
            cwd = p.get("cwd") or cwd
            branch = (p.get("git") or {}).get("branch") or branch
            sub = sub or bool(p.get("parent_thread_id"))
            who = provider_of(p.get("model_provider")) if "model_provider" in p else who
            if p.get("creator_account_id") or p.get("creator_user_id"):
                acct = account_hash(p.get("creator_account_id"), p.get("creator_user_id"))
        elif kind == "turn_context":
            model = p.get("model") or model
        elif kind == "event_msg" and p.get("type") == "token_count":
            ts = _ts(d.get("timestamp"))
            if ts is None:
                continue
            rl = p.get("rate_limits") or {}
            main = rl.get("limit_id") in (None, MAIN_LIMIT)
            for slot in ("primary", "secondary"):
                w = rl.get(slot)
                if (main and isinstance(w, dict) and w.get("window_minutes") in WINDOWS
                        and w.get("used_percent") is not None and w.get("resets_at")):
                    readings.append((ts, WINDOWS[w["window_minutes"]], float(w["used_percent"]), float(w["resets_at"])))
                    planned = True
            info = p.get("info") or {}
            last = info.get("last_token_usage") or {}
            total = (info.get("total_token_usage") or {}).get("total_tokens")
            if last and total != last_total and sid:   # Codex repeats the event when nothing new was used
                last_total = total
                cached = int(last.get("cached_input_tokens") or 0)
                e = Usage(HARNESS, sid, f"{sid}:{total}", ts, model=model, subagent=sub,
                          input=max(0, int(last.get("input_tokens") or 0) - cached), cache_read=cached,
                          cache_write_5m=int(last.get("cache_write_input_tokens") or 0),
                          output=int(last.get("output_tokens") or 0), project=project, thread=sid, cwd=cwd,
                          branch=branch, account=acct,
                          reasoning=int(last["reasoning_output_tokens"]) if "reasoning_output_tokens" in last else None)
                e.cost_usd = pricing.cost(model, input=e.input, output=e.output, cache_read=e.cache_read,
                                          cache_write_5m=e.cache_write_5m)
                usage.append(e)
    api = billing == "api" or (not planned and billing is None and auth_mode() == "apikey")
    for e in usage:
        e.billing = "api" if api or who else None
        e.provider = who
    added = store.add_usage(usage)
    _add_readings(store, readings, acct)
    store.set_meta(f"codex_file:{path.name}", {"sid": sid, "model": model, "project": project, "sub": sub, "cwd": cwd, "branch": branch,
                                              "planned": planned, "total": last_total, "provider": who, "account": acct})
    store.set_offset(path, offset + end, size)
    store.conn.commit()
    return added


def account_hash(account_id, user_id) -> str:
    """One ChatGPT login (a workspace and its user, as each has its own limits), as a short hash: never the ids."""
    return hashlib.sha1(f"{account_id or ''}:{user_id or ''}".encode()).hexdigest()[:12]


def _add_readings(store: Store, readings, account=None):
    kept, rows = {}, []
    for ts, window, pct, resets in sorted(readings):
        k = kept.get(window)
        if k and pct == k[1] and ts - k[0] < THIN_SECONDS:
            continue
        kept[window] = (ts, pct)
        rows.append((store.machine, HARNESS, account, ts, window, pct, resets))
    if rows:
        store.insert("meter", ["machine", "harness", "account", "ts", "name", "pct", "resets"], rows)


def transcripts(root: Path | None = None):
    root = root or codex_home() / "sessions"
    return sorted(root.glob("**/*.jsonl"), key=lambda p: p.stat().st_mtime) if root.exists() else []


def backfill(store: Store, root: Path | None = None, billing=None) -> int:
    return sum(ingest_file(store, p, billing) for p in transcripts(root))
