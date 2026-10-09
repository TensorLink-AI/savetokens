"""savetokens as an MCP server (stdio): the brief and job estimates as tools, for any agent.

Claude Code, Codex and other MCP clients start `savetokens mcp` and call:

  pacing_brief   where each limit stands, this session's usage, and the options ranked by effect
  estimate_job   a job's size in points and how long it would take, waits for resets included

JSON-RPC 2.0, one message per line, on stdin and stdout. Read-only: nothing is changed.
"""
from __future__ import annotations

import json
import os
import sys
import time

PROTOCOL = "2025-06-18"

TOOLS = [
    {"name": "pacing_brief",
     "description": "Where the user's Claude Code / Codex usage limits stand (plan limits or API budgets), how this"
                    " session is using them, and the options to pace usage, ranked by measured effect with the exact"
                    " change for each. Call before large jobs, when the user asks about limits or usage, or when a"
                    " limit alert appears. Suggest options to the user; never change settings without asking.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string", "description": "This session's id, if known (else the latest session in"
                                                         " the current folder is used)."}}}},
    {"name": "estimate_job",
     "description": "Estimate a job before starting it: its size in points of the limit (% of the weekly limit, or"
                    " of an API budget), the hours of work at the user's pace, and when it would finish given the"
                    " limits (including waits for a 5-hour reset), plus a better start time if waiting can be"
                    " avoided. Size the job one way: points, usd (API-equivalent dollars), like ('small', 'typical',"
                    " 'big' compared with past sessions in this project, or a session id), or hours of work.",
     "inputSchema": {"type": "object", "properties": {
         "points": {"type": "number"}, "usd": {"type": "number"},
         "like": {"type": "string", "description": "small | typical | big | a past session id"},
         "hours": {"type": "number", "description": "hours of work at the current pace"},
         "parallel": {"type": "integer", "minimum": 1, "description": "sessions or subagents working at once"},
         "session_id": {"type": "string"}}}},
]


def _harness(client):
    name = (client or {}).get("name", "").lower()
    return "codex" if "codex" in name else "claude-code" if "claude" in name else None


def call(name, args, client=None, store=None):
    """A tool's result as text (JSON for the numbers, then a plain summary)."""
    from . import advise, capture
    from .store import Store
    own = store is None
    store = store or Store()
    try:
        try:
            capture.backfill(store)   # fresh numbers; incremental, so cheap
        except Exception:
            pass
        now = time.time()
        if name == "pacing_brief":
            b = advise.brief(store, now, args.get("session_id"), os.getcwd(), _harness(client))
            return advise.brief_text(b) + "\n\n" + json.dumps(_clean(b), default=str)
        if name == "estimate_job":
            e = advise.estimate(store, now, points=args.get("points"), usd=args.get("usd"), like=args.get("like"),
                                hours=args.get("hours"), parallel=max(1, int(args.get("parallel") or 1)),
                                session_id=args.get("session_id"), cwd=os.getcwd())
            return (e.get("summary") or e.get("error", "")) + "\n\n" + json.dumps(_clean(e), default=str)
        raise KeyError(name)
    finally:
        if own:
            store.close()


def _clean(x):
    """Round floats so the agent reads less."""
    if isinstance(x, float):
        return round(x, 2)
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items() if k != "session_full"}
    if isinstance(x, list):
        return [_clean(v) for v in x]
    return x


def handle(msg, state):
    """One JSON-RPC message in, the response (or None for a notification)."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    try:
        if method == "initialize":
            from . import __version__
            state["client"] = (msg.get("params") or {}).get("clientInfo")
            result = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion") or PROTOCOL,
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "savetokens", "version": __version__},
                      "instructions": "Usage limits for Claude Code and Codex: call pacing_brief when limits or"
                                      " usage come up, and estimate_job before a large job."}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            p = msg.get("params") or {}
            try:
                text = call(p.get("name"), p.get("arguments") or {}, state.get("client"))
                result = {"content": [{"type": "text", "text": text}]}
            except KeyError:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}}
            except Exception as e:
                result = {"content": [{"type": "text", "text": f"savetokens failed: {e}"}], "isError": True}
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)[:200]}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=sys.stdin, stdout=sys.stdout):
    state = {}
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                     "error": {"code": -32700, "message": "parse error"}}) + "\n")
            stdout.flush()
            continue
        out = handle(msg, state)
        if out is not None:
            stdout.write(json.dumps(out) + "\n")
            stdout.flush()
