"""Claude Code adapter: transcript backfill, hooks and statusline.

Transcripts repeat one assistant message per content block with the same
message id and request id, so usage is keyed on both (as ccusage does).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .. import guard, pricing
from ..store import Store, ToolEvent, UsageEvent, load_config
from ..toolkinds import args_hash, classify

HARNESS = "claude-code"


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _ts(value) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def _chars(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(b.get("text", "")) if isinstance(b, dict) else len(str(b)) for b in content)
    return len(json.dumps(content, default=str)) if content is not None else 0


def tool_session(session_id, agent_id=None) -> str:
    """Tool events are tracked per agent so parallel subagents don't look like one loop."""
    return f"{session_id}/{agent_id}" if agent_id else session_id


def parse_compaction(d: dict):
    """(session_id, ts, trigger, pre_tokens) for a compact_boundary line, else None."""
    if d.get("type") != "system" or d.get("subtype") != "compact_boundary":
        return None
    meta = d.get("compactMetadata") or {}
    sid = d.get("sessionId") or d.get("session_id")
    if not sid:
        return None
    return sid, _ts(d.get("timestamp")), meta.get("trigger"), meta.get("preTokens")


def parse_entry(d: dict, subagent_file=False):
    """Usage and tool events from one transcript line."""
    usage, tools = [], []
    kind = d.get("type")
    msg = d.get("message") if isinstance(d.get("message"), dict) else {}
    sid = d.get("sessionId") or d.get("session_id")
    if not sid or kind not in ("assistant", "user"):
        return usage, tools
    ts = _ts(d.get("timestamp"))
    sub = bool(d.get("isSidechain")) or subagent_file
    tsid = tool_session(sid, d.get("agentId") if sub else None)
    content = msg.get("content") if isinstance(msg.get("content"), list) else []
    if kind == "assistant":
        u, model = msg.get("usage"), msg.get("model")
        if isinstance(u, dict) and model and model != "<synthetic>":
            cc = u.get("cache_creation") or {}
            write = int(u.get("cache_creation_input_tokens") or 0)
            w1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
            w5m = int(cc.get("ephemeral_5m_input_tokens") or 0) if cc else write
            if cc and w5m + w1h < write:
                w5m = write - w1h
            ev = UsageEvent(
                harness=HARNESS, session_id=sid, ts=ts, model=model, subagent=sub,
                request_id=f"{msg.get('id')}:{d.get('requestId')}",
                project=Path(d["cwd"]).name if d.get("cwd") else None,
                input=int(u.get("input_tokens") or 0), output=int(u.get("output_tokens") or 0),
                cache_read=int(u.get("cache_read_input_tokens") or 0), cache_write_5m=w5m, cache_write_1h=w1h,
                provider="anthropic")
            ev.cost_usd = pricing.cost(model, input=ev.input, output=ev.output, cache_read=ev.cache_read,
                                       cache_write_5m=w5m, cache_write_1h=w1h, fast=u.get("speed") == "fast")
            ev.cost_source = "price_table" if ev.cost_usd is not None else None
            usage.append(ev)
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                k, target = classify(b.get("name", ""), b.get("input"))
                tools.append(ToolEvent(HARNESS, tsid, ts, b.get("name", "?"), k, tool_use_id=b.get("id"),
                                       args_hash=args_hash(b.get("input")), target=target))
    else:
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tools.append(ToolEvent(HARNESS, tsid, ts, "?", "other", tool_use_id=b.get("tool_use_id"),
                                       ok=not b.get("is_error", False), output_chars=_chars(b.get("content"))))
    return usage, tools


def ingest_file(store: Store, path: Path, max_bytes: int | None = None) -> int:
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
    subagent_file = "subagents" in path.parts
    usage, tools, compactions = {}, [], []
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(max_bytes) if max_bytes else f.read()
    end = data.rfind(b"\n") + 1
    for line in data[:end].splitlines():
        if (b'"usage"' not in line and b'"tool_result"' not in line and b'"tool_use"' not in line
                and b"compact_boundary" not in line):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        c = parse_compaction(d)
        if c:
            compactions.append(c)
            continue
        u, t = parse_entry(d, subagent_file)
        for e in u:
            prev = usage.get(e.request_id)
            if prev is None or e.output > prev.output:
                usage[e.request_id] = e
        tools.extend(t)
    added = store.add_usage(usage.values())
    store.add_tools(tools)
    if compactions:
        store.conn.executemany("INSERT OR IGNORE INTO compactions VALUES (?,?,?,?,?)",
                               [(HARNESS, *c) for c in compactions])
    store.set_offset(path, offset + end, size)
    store.conn.commit()
    return added


EVAL_MARK = "-st-eval-"   # sessions run by savetokens' own evals (workspaces under /tmp/st-eval-*)


def transcripts(root: Path | None = None):
    root = root or claude_home() / "projects"
    return sorted((p for p in root.glob("**/*.jsonl") if EVAL_MARK not in str(p)), key=lambda p: p.stat().st_mtime)


def backfill(store: Store, root: Path | None = None, progress=None) -> int:
    files = transcripts(root)
    total = 0
    for i, p in enumerate(files):
        total += ingest_file(store, p)
        if progress:
            progress(i + 1, len(files))
    return total


# ── hooks ────────────────────────────────────────────────────────────────────

def _ingest_live(store, payload):
    tp = payload.get("transcript_path")
    if tp:
        ingest_file(store, Path(tp), max_bytes=64 * 1024 * 1024)
        # subagent transcripts live beside the session file
        sub = Path(tp).with_suffix("") / "subagents"
        if payload.get("agent_id") and sub.is_dir():
            for p in sub.glob("*.jsonl"):
                ingest_file(store, p, max_bytes=16 * 1024 * 1024)


def handle_hook(event: str, payload: dict, store: Store, cfg=None):
    """Hook output as a dict (printed as JSON), or None for no output."""
    cfg = cfg or load_config()
    sid = payload.get("session_id") or "unknown"
    tsid = tool_session(sid, payload.get("agent_id"))
    tool = payload.get("tool_name") or "?"
    args = payload.get("tool_input")
    kind, target = classify(tool, args)
    h = args_hash(args)

    if event == "PreToolUse":
        reason = guard.block_reason(store, HARNESS, sid, tsid, tool, h, kind, cfg)
        if reason:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": reason}}
        return None

    if event in ("PostToolUse", "PostToolUseFailure"):
        failed = event == "PostToolUseFailure"
        resp = payload.get("tool_response")
        if isinstance(resp, dict) and (resp.get("is_error") or resp.get("interrupted")):
            failed = True
        out = resp if resp is not None else payload.get("error")
        store.add_tools([ToolEvent(HARNESS, tsid, time.time(), tool, kind, tool_use_id=payload.get("tool_use_id"),
                                   args_hash=h, target=target, ok=not failed,
                                   output_chars=_chars(out) if not isinstance(out, dict)
                                   else len(json.dumps(out, default=str)))])
        _ingest_live(store, payload)
        alerts = guard.evaluate(store, HARNESS, sid, tsid, cfg)
        if not alerts:
            return None
        to_agent = " ".join(a.message for a in alerts if a.audience == guard.AGENT)
        result = {"systemMessage": " ".join(a.message for a in alerts)}
        if to_agent:
            result["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": to_agent}
        return result

    if event == "SessionStart":
        from .. import levers, steer
        try:
            levers.update(store, cfg=cfg, harnesses=(HARNESS,))
        except Exception:
            pass
        text = steer.briefing(store, cwd=payload.get("cwd"), session_id=sid, cfg=cfg)
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}} if text else None

    if event == "UserPromptSubmit":
        from .. import steer
        _ingest_live(store, payload)
        heavy = steer.heavy_followup(store, sid, payload.get("prompt"), cfg=cfg)
        if heavy and cfg.get("heavy_followup") == "block":   # opt-in: stop it; sending it again goes through
            return {"decision": "block", "reason": heavy + " (Send it again to go ahead.)"}
        n = steer.nudge(store, sid, HARNESS, cfg=cfg)
        if not n and not heavy:
            return None
        out = {"systemMessage": " ".join(x for x in (heavy, n[1] if n else None) if x)}
        if n:
            out["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": n[0]}
        return out

    if event in ("Stop", "SubagentStop", "SessionEnd", "PreCompact"):
        _ingest_live(store, payload)
        if event == "Stop":   # headless runs (claude -p) have no statusline to drive upkeep
            from .. import maintain
            maintain.kick(store)
    return None


# ── statusline ───────────────────────────────────────────────────────────────

def record_statusline(payload: dict, store: Store):
    from .. import limits
    rl = payload.get("rate_limits") or {}
    model = (payload.get("model") or {}).get("id")
    if rl.get("five_hour") or rl.get("seven_day"):
        billing, source = limits.SUBSCRIPTION, "statusline"
    elif rl.get("spend_limit") or os.environ.get("ANTHROPIC_API_KEY"):
        billing, source = limits.API, "statusline"
    else:   # limits are absent before the first response too: keep the account default
        billing, source = limits.default_billing(store, HARNESS), "account"
    account = limits.current_account(store)
    limits.record_session(store, HARNESS, payload.get("session_id"), billing=billing, model=model, source=source,
                          account=account)
    ctx = payload.get("context_window") or {}

    def g(name, field):
        v = rl.get(name)
        return v.get(field) if isinstance(v, dict) else None

    store.add_limits(HARNESS, payload.get("session_id"), account=account,
                     five_hour_pct=g("five_hour", "used_percentage"), five_hour_resets=g("five_hour", "resets_at"),
                     seven_day_pct=g("seven_day", "used_percentage"), seven_day_resets=g("seven_day", "resets_at"),
                     spend_pct=g("spend_limit", "used_percentage"), spend_resets=g("spend_limit", "resets_at"),
                     context_pct=ctx.get("used_percentage"))


def statusline(payload: dict, raw: str, store: Store) -> str:
    """Our segment, composed after the user's own statusline command if they had one."""
    from .. import forecast
    record_statusline(payload, store)
    if payload.get("transcript_path"):
        ingest_file(store, Path(payload["transcript_path"]), max_bytes=16 * 1024 * 1024)
    ours = forecast.segment(store, payload)
    try:
        from .. import maintain
        maintain.kick(store)
    except Exception:
        pass
    wrapped = load_config().get("statusline_wrapped")
    if wrapped:
        try:
            theirs = subprocess.run(wrapped, shell=True, input=raw, capture_output=True, text=True,
                                    timeout=1.5).stdout.rstrip("\n")
        except (subprocess.SubprocessError, OSError):
            theirs = ""
        if theirs:
            return f"{theirs} │ {ours}" if ours else theirs
    return ours
