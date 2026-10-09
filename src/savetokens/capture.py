"""Claude Code capture: usage and limit hits from transcripts, limit readings from the statusline.
(Codex has its own reader, codex.py; backfill reads both.)

Transcripts repeat one assistant message per content block with the same message
id and request id, so usage is keyed on both (as ccusage does). A limit error
becomes a hit; only its kind is kept, never the message text.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from . import pricing
from .store import Store, Usage

HARNESS = "claude-code"
EVAL_MARK = "-st-eval-"   # workspaces of savetokens' own evals


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def account() -> dict:
    """The signed-in account: a short hash of its id (never names or emails) and its plan fields."""
    path = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home()) / ".claude.json"
    if not path.exists():
        path = claude_home() / ".claude.json"
    try:
        acct = json.loads(path.read_text()).get("oauthAccount") or {}
    except (OSError, ValueError):
        acct = {}
    uid = acct.get("accountUuid") or acct.get("organizationUuid")
    return {"account": hashlib.sha1(uid.encode()).hexdigest()[:12] if uid else None,
            "plan": acct.get("organizationType"),
            "tier": acct.get("organizationRateLimitTier") or acct.get("userRateLimitTier")}


def _ts(value) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def _text(msg) -> str:
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


MODEL_LIMIT = re.compile(r"reached your ([\w .-]+?) limit", re.I)


def hit_kind(text: str):
    """(kind, model) for a limit error's text."""
    t = text.lower()
    if "session limit" in t or "5-hour limit" in t:
        return "session", None
    if "weekly" in t:
        return "weekly", None
    m = MODEL_LIMIT.search(text)
    if m and m.group(1).lower() not in ("usage", "spend", "session", "specified"):
        return "model", m.group(1).strip().lower()
    return "other", None


def parse_line(d: dict, subagent_file=False, acct=None):
    """(usage or None, hit or None) from one transcript line."""
    sid = d.get("sessionId") or d.get("session_id")
    if not sid or d.get("type") != "assistant":
        return None, None
    msg = d.get("message") if isinstance(d.get("message"), dict) else {}
    ts = _ts(d.get("timestamp"))
    if d.get("error") == "rate_limit":
        kind, model = hit_kind(_text(msg))
        return None, (HARNESS, acct, sid, ts, kind, model)
    u, model = msg.get("usage"), msg.get("model")
    if not isinstance(u, dict) or not model or model == "<synthetic>":
        return None, None
    cc = u.get("cache_creation") or {}
    write = int(u.get("cache_creation_input_tokens") or 0)
    w1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
    w5m = int(cc.get("ephemeral_5m_input_tokens") or 0) if cc else write
    if cc and w5m + w1h < write:
        w5m = write - w1h
    e = Usage(harness=HARNESS, session_id=sid, ts=ts, model=model,
              subagent=bool(d.get("isSidechain")) or subagent_file,
              request_id=f"{msg.get('id')}:{d.get('requestId')}",
              input=int(u.get("input_tokens") or 0), output=int(u.get("output_tokens") or 0),
              cache_read=int(u.get("cache_read_input_tokens") or 0), cache_write_5m=w5m, cache_write_1h=w1h,
              account=acct, project=Path(d["cwd"]).name if d.get("cwd") else None)
    e.cost_usd = pricing.cost(model, input=e.input, output=e.output, cache_read=e.cache_read,
                              cache_write_5m=w5m, cache_write_1h=w1h, fast=u.get("speed") == "fast")
    return e, None


def billing(store=None) -> str | None:
    """"api" when this machine's Claude Code runs on an API key: set with `savetokens api claude-code`,
    else as detected (detect_billing)."""
    from .store import load_config
    set_ = (load_config().get("billing") or {}).get(HARNESS)
    if set_:
        return "api" if set_ == "api" else None
    return "api" if store is not None and store.meta(f"detected_billing:{HARNESS}") == "api" else None


NO_METER_PAYLOADS, NO_METER_SECONDS, METER_DAYS = 10, 900, 7


def api_key_signs() -> bool:
    """Signs that Claude Code here runs on an API key: a key in its environment, or no claude.ai login."""
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or not account()["account"])


def detect_billing(store, now=None) -> str | None:
    """Claude Code's billing on this machine, from what the statusline sends. A plan always sends its
    limit meter; on an API key there is none. Returns "api", "subscription" or None (can't tell yet)."""
    now = now or time.time()
    seen = store.meta("statusline_meter") or 0
    none = store.meta("statusline_no_meter") or {}
    if seen and now - seen < METER_DAYS * 86400:
        result = "subscription"
    elif (none.get("n", 0) >= NO_METER_PAYLOADS and none.get("last", 0) - none.get("first", 0) >= NO_METER_SECONDS
          and none.get("key_signs")):
        result = "api"
    else:
        return None
    store.set_meta(f"detected_billing:{HARNESS}", result)
    return result


def ingest_file(store: Store, path: Path, acct=None, max_bytes: int | None = None, bill=None) -> int:
    """Read new complete lines since the last call. Returns usage rows added."""
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
        data = f.read(max_bytes) if max_bytes else f.read()
    end = data.rfind(b"\n") + 1
    usage, hits = {}, []
    sub = "subagents" in path.parts
    for line in data[:end].splitlines():
        if b'"usage"' not in line and b"rate_limit" not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        e, h = parse_line(d, sub, acct)
        if e:
            e.billing = bill
        if e and (e.request_id not in usage or e.output > usage[e.request_id].output):
            usage[e.request_id] = e
        if h:
            hits.append(h)
    added = store.add_usage(usage.values())
    if hits:
        store.add_hits(hits)
    store.set_offset(path, offset + end, size)
    store.conn.commit()
    return added


def transcripts(root: Path | None = None):
    root = root or claude_home() / "projects"
    if not root.exists():
        return []
    return sorted((p for p in root.glob("**/*.jsonl") if EVAL_MARK not in str(p)), key=lambda p: p.stat().st_mtime)


def backfill(store: Store, root: Path | None = None) -> int:
    """Every Claude Code and Codex session on this machine. Older sessions' account is unknown, so it is
    left empty."""
    from . import codex
    from .store import load_config
    bill = billing(store)
    n = sum(ingest_file(store, p, bill=bill) for p in transcripts(root))
    return n + codex.backfill(store, billing=(load_config().get("billing") or {}).get(codex.HARNESS))


def ingest_session(store: Store, transcript_path, acct=None):
    """Live: the session's transcript and its subagents' (they live beside it)."""
    if not transcript_path:
        return
    p = Path(transcript_path)
    bill = billing(store)
    ingest_file(store, p, acct, max_bytes=64 * 1024 * 1024, bill=bill)
    sub = p.with_suffix("") / "subagents"
    if sub.is_dir():
        for q in sub.glob("*.jsonl"):
            ingest_file(store, q, acct, max_bytes=16 * 1024 * 1024, bill=bill)


def record_statusline(store: Store, payload: dict, acct=None) -> int:
    """Every limit Claude reports in the statusline payload: % used and reset time."""
    rl = payload.get("rate_limits") or {}
    readings = {name: (v.get("used_percentage"), v.get("resets_at"))
                for name, v in rl.items() if isinstance(v, dict) and name != "spend_limit"}
    now = time.time()
    if readings:
        store.set_meta("statusline_meter", now)
        if store.meta("statusline_no_meter"):
            store.conn.execute("DELETE FROM meta WHERE key = 'statusline_no_meter'")
        return store.add_meter(HARNESS, acct, readings)
    if payload.get("model"):    # a real session's payload with no meter in it: counts toward "on an API key"
        n = store.meta("statusline_no_meter") or {"first": now, "n": 0}
        n.update(n=n["n"] + 1, last=now, key_signs=n.get("key_signs") or api_key_signs())
        store.set_meta("statusline_no_meter", n)
    return 0
