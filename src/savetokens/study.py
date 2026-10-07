"""Study: an LLM reads the structure of your most expensive sessions and proposes what to change.

The model only proposes. A finding becomes a change only after it is turned into a
fixed setting, rule or lever and measured (eval, backtest or before/after).

What the model sees is a skeleton, never content: per task segment (user prompt to the
next prompt), its timing, idle gap, turns, cost, context size, compactions and tool calls
by kind and size, with failures. Prompts are reduced to their length; file paths and
project names are hashed. `savetokens study --dry-run` prints exactly what would be sent.

Each finding is tagged with one of the report's known waste categories or "new". If
the study keeps producing only known categories, the deterministic report already covers
it and the LLM step adds nothing (the check this module exists to run).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections import Counter
from pathlib import Path

from . import pricing
from .store import Store
from .toolkinds import classify

KNOWN = {
    "context_carry": "long contexts re-read every turn",
    "cache_rebuilds": "prompt cache rebuilt after idle gaps",
    "rereads": "unchanged files re-read",
    "big_outputs": "large tool outputs carried in context",
    "subagent_model": "subagents on a top-tier model",
    "cache_share": "low cache hit rate",
    "loops": "repeated identical tool calls or failing test re-runs",
}
BIG = 15_000
MAX_SEGMENTS = 30


def _h(text, n=4):
    return hashlib.sha1(str(text).encode()).hexdigest()[:n]


def _anon_target(path):
    if not path:
        return None
    p = Path(path)
    return f"file:{_h(p.parent)}/{_h(p.name)}{p.suffix}"


def _chars(content):
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(b.get("text", "")) if isinstance(b, dict) else len(str(b)) for b in content)
    return len(json.dumps(content, default=str)) if content is not None else 0


def _ts(v):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def skeleton(path: Path) -> dict:
    """Segments of one session transcript, structure only."""
    segs, cur, seen, pending = [], None, set(), {}
    last_ts = None

    def new_segment(ts, prompt_chars):
        return {"start": ts, "end": ts, "prompt_chars": prompt_chars,
                "idle_before_min": round((ts - last_ts) / 60) if last_ts and ts else 0,
                "turns": 0, "usd": 0.0, "ctx": [], "models": Counter(), "tools": Counter(), "big": 0,
                "big_max": 0, "failed": Counter(), "subagent_usd": 0.0, "compactions": [], "targets": Counter()}

    for line in open(path, "rb"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        ts = _ts(d.get("timestamp"))
        kind = d.get("type")
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        if kind == "system" and d.get("subtype") == "compact_boundary":
            meta = d.get("compactMetadata") or {}
            if cur:
                cur["compactions"].append(f"{meta.get('trigger')} at {int(meta.get('preTokens') or 0) // 1000}k")
            continue
        if kind == "user":
            content = msg.get("content")
            is_result = isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                                          for b in content)
            if is_result:
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and cur is not None:
                        k = pending.pop(b.get("tool_use_id"), "other")
                        n = _chars(b.get("content"))
                        if n >= BIG:
                            cur["big"] += 1
                            cur["big_max"] = max(cur["big_max"], n)
                        if b.get("is_error"):
                            cur["failed"][k] += 1
            elif not d.get("isMeta") and not d.get("isCompactSummary") and not d.get("isSidechain"):
                if cur:
                    segs.append(cur)
                cur = new_segment(ts, _chars(content))
        elif kind == "assistant" and cur is not None:
            u, model = msg.get("usage"), msg.get("model")
            rid = f"{msg.get('id')}:{d.get('requestId')}"
            if isinstance(u, dict) and model and model != "<synthetic>" and rid not in seen:
                seen.add(rid)
                cost = pricing.cost(model, input=int(u.get("input_tokens") or 0),
                                    output=int(u.get("output_tokens") or 0),
                                    cache_read=int(u.get("cache_read_input_tokens") or 0),
                                    cache_write_5m=int(u.get("cache_creation_input_tokens") or 0)) or 0.0
                cur["turns"] += 1
                cur["usd"] += cost
                if d.get("isSidechain"):
                    cur["subagent_usd"] += cost
                else:
                    cur["ctx"].append(sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                                        "cache_creation_input_tokens")))
                cur["models"][pricing.normalize(model)] += 1
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    k, target = classify(b.get("name", ""), b.get("input"))
                    cur["tools"][k] += 1
                    pending[b.get("id")] = k
                    if target and k == "read":
                        cur["targets"][_anon_target(target)] += 1
            if ts:
                cur["end"] = ts
                last_ts = ts
    if cur:
        segs.append(cur)
    return {"session": _h(path.stem, 8), "project": f"project-{_h(path.parent.name)}", "segments": segs}


def render(sk: dict) -> str:
    segs = sorted(sk["segments"], key=lambda s: -s["usd"])[:MAX_SEGMENTS]
    segs.sort(key=lambda s: s["start"] or 0)
    total = sum(s["usd"] for s in sk["segments"])
    lines = [f"session {sk['session']} ({sk['project']}): {len(sk['segments'])} task segments, ${total:,.2f}"
             f" (showing the {len(segs)} most expensive)"]
    t0 = sk["segments"][0]["start"] if sk["segments"] and sk["segments"][0]["start"] else 0
    for i, s in enumerate(segs):
        ctx = f"{s['ctx'][0] // 1000}k→{s['ctx'][-1] // 1000}k" if s["ctx"] else "-"
        tools = ", ".join(f"{k} {n}" for k, n in s["tools"].most_common())
        failed = ", ".join(f"{k} {n}" for k, n in s["failed"].items())
        rereads = sum(n - 1 for n in s["targets"].values() if n > 1)
        parts = [f"+{((s['start'] or t0) - t0) / 3600:.1f}h", f"idle before {s['idle_before_min']}m",
                 f"prompt {s['prompt_chars']} chars", f"{s['turns']} turns", f"${s['usd']:.2f}", f"ctx {ctx}",
                 "/".join(f"{m}" for m in s["models"]), f"tools: {tools or 'none'}"]
        if s["big"]:
            parts.append(f"{s['big']} outputs ≥15k chars (max {s['big_max'] // 1000}k)")
        if failed:
            parts.append(f"failed: {failed}")
        if rereads:
            parts.append(f"{rereads} re-reads of {sum(1 for n in s['targets'].values() if n > 1)} files")
        if s["subagent_usd"]:
            parts.append(f"subagents ${s['subagent_usd']:.2f}")
        if s["compactions"]:
            parts.append("compaction " + ", ".join(s["compactions"]))
        lines.append(f"  [{i}] " + " | ".join(parts))
    return "\n".join(lines)


PROMPT = """You are studying how a coding agent (Claude Code) spends tokens, to propose changes that cut cost
without hurting task success. Below are skeletons of the user's most expensive sessions: one line per
task segment (a user prompt up to the next one). No content is shown, only structure. Cost is
API-equivalent dollars; ctx is the context re-read on every turn (first→last turn of the segment).

Known waste categories (already detected by fixed rules): {known}

Return ONLY a JSON array (at most 6 items) of findings, each:
  {{"pattern": "...", "evidence": "session ids and [segment] numbers", "category": one of {cats} or "new",
    "est_share_of_cost": number 0..1, "change": "a concrete fixed change: a setting, hook rule, CLAUDE.md line
    or a condition for switching model/effort/compaction", "measure": "how to test it"}}
Prefer patterns that hold across sessions. Use "new" only for something none of the known categories cover.

{skeletons}
"""


def sessions_to_study(store: Store, days=7, n=6, now=None):
    from .adapters.claude_code import transcripts
    now = now or time.time()
    rows = store.conn.execute("SELECT session_id, SUM(cost_usd) usd FROM usage WHERE harness = 'claude-code' AND"
                              " ts >= ? GROUP BY 1 ORDER BY usd DESC LIMIT ?", (now - days * 86400, n)).fetchall()
    files = {p.stem: p for p in transcripts()}
    return [(r["session_id"], r["usd"], files[r["session_id"]]) for r in rows if r["session_id"] in files]


def run(store: Store, days=7, n=6, model="claude-haiku-4-5", dry_run=False, runner=subprocess.run):
    picked = sessions_to_study(store, days, n)
    text = "\n\n".join(render(skeleton(p)) for _, _, p in picked)
    prompt = PROMPT.format(known="; ".join(f"{k}: {v}" for k, v in KNOWN.items()),
                           cats=", ".join(KNOWN), skeletons=text)
    if dry_run:
        return {"prompt": prompt, "sessions": len(picked)}
    r = runner(["claude", "-p", "--model", model, "--output-format", "json", "--setting-sources", "project",
                "--strict-mcp-config", "--no-session-persistence", "--permission-mode", "dontAsk"],
               input=prompt, capture_output=True, text=True, timeout=600)
    out = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {"result": r.stdout or r.stderr}
    raw = str(out.get("result", ""))
    try:
        findings = json.loads(raw[raw.index("["):raw.rindex("]") + 1])
    except ValueError:
        findings = []
    result = {"made_at": time.time(), "sessions": len(picked), "model": model, "cost_usd": out.get("total_cost_usd"),
              "prompt_tokens_approx": len(prompt) // 4, "findings": findings,
              "new": [f for f in findings if f.get("category") == "new"]}
    store.set_meta("study_last", result)
    return result
