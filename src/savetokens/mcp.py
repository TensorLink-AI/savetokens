"""savetokens as an MCP server (stdio): the brief and job estimates as tools, for any agent.

Claude Code, Codex, Hermes and other MCP clients start `savetokens mcp` and call:

  pacing_brief   where each limit stands, this session's usage, and the options ranked by effect
  estimate_job   a job's size in points and how long it would take, waits for resets included
  spend_summary  tokens and $ by provider and model: today, this week, this month, and projected
  suggest_setup  what to set up (budgets, prices, ...), each with the command for the user to run
  efficiency     where tokens go and what the same work would cost done differently: facts and what-ifs
  task_status    the task this folder is on, its cost so far, its budget, and what finished tasks cost
  task_start     declare the task the work that follows belongs to (records only; changes no settings)
  task_done      mark the current task finished, so its cost joins the estimates

JSON-RPC 2.0, one message per line, on stdin and stdout. Read-only, except that task_start and task_done
record which task the work belongs to; no setting is ever changed.
"""
from __future__ import annotations

import json
import os
import sys
import time

PROTOCOL = "2025-06-18"

TOOLS = [
    {"name": "pacing_brief",
     "description": "Where the user's Claude Code / Codex / Hermes usage limits stand (plan limits or API budgets), how this"
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
    {"name": "spend_summary",
     "description": "Tokens and dollars by provider (Anthropic, OpenAI, OpenRouter, ...) and by model: so far today,"
                    " this week and this month, likely by each one's end, and over the next day, week and 30 days."
                    " Pay-as-you-go spend plus subscriptions as a fixed cost. Use when the user asks what they're"
                    " spending, where, or what it will cost.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "efficiency",
     "description": "Where the user's tokens go, for any tool and provider: facts (tokens and $ by provider, model and"
                    " project; how much of each prompt is context re-sent from earlier in the conversation and how much"
                    " of that the cache served; how far prompts grew; the share of output spent reasoning) and"
                    " what-ifs, each with its assumption stated (caching the re-sent context; keeping conversations"
                    " near their starting size; the same tokens at another model's prices, price only). Use it to"
                    " decide how to work more cheaply, e.g. when to start fresh or which model to use for a"
                    " subtask. Every number is labelled fact or what-if; don't present a what-if as a saving made.",
     "inputSchema": {"type": "object", "properties": {
         "days": {"type": "integer", "minimum": 1, "description": "how many days back (default 7)"},
         "tool": {"type": "string", "enum": ["claude-code", "codex", "hermes"]},
         "provider": {"type": "string"}, "project": {"type": "string", "description": "a project's name (as savetokens projects shows it)"},
         "model": {"type": "string", "description": "only this model"},
         "as_model": {"type": "string", "description": "price the same tokens at this model's prices"}}}},
    {"name": "task_status",
     "description": "The task the current folder is on (declared, or its git branch), what it has cost so far in"
                    " tokens and dollars, its budget if it has one, and what finished tasks in this project cost"
                    " (measured: median and 90th percentile, with how many tasks; or 'not enough data yet'). Use it"
                    " to pace a task against its budget.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "task_start",
     "description": "Declare the task the work that follows belongs to, in the current folder's project, e.g. when"
                    " the user gives you a new piece of work. Its tokens are then counted to it until task_done, and"
                    " the result includes what similar finished tasks cost. Only records the task; changes nothing"
                    " else.",
     "inputSchema": {"type": "object", "required": ["name"], "properties": {
         "name": {"type": "string", "description": "a short name for the task, e.g. an issue key or 'Add OAuth'"}}}},
    {"name": "task_done",
     "description": "Mark the current task finished (or the named one), so its cost counts toward estimates for"
                    " future tasks. Call when the user's piece of work is complete.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
    {"name": "suggest_setup",
     "description": "What would make savetokens more useful here (a budget for a tool paying by API, a price for a"
                    " model it can't price, the forecaster, the browser view), each with the exact command and what"
                    " to ask the user for. Changes nothing: show the user the command, fill in what they tell you,"
                    " and run it only after they agree.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _harness(client):
    name = (client or {}).get("name", "").lower()
    for key, harness in (("codex", "codex"), ("hermes", "hermes"), ("claude", "claude-code")):
        if key in name:
            return harness
    return None


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
        if name == "spend_summary":
            from . import spend
            got = spend.summary(store, now)
            return spend.text(got) + "\n\n" + json.dumps(_clean(got), default=str)
        if name == "efficiency":
            from . import efficiency
            from . import projects
            where = {k: args.get(a) for k, a in (("harness", "tool"), ("provider", "provider"), ("model", "model"))
                     if args.get(a)}
            if args.get("project"):
                where["project_id"] = [i for i in projects.by_name(store, args["project"]) if i] or ["-"]
            r = efficiency.report(store, days=max(1, int(args.get("days") or 7)), now=now, where=where or None,
                                  model=args.get("as_model"))
            return efficiency.text(r) + "\n\n" + json.dumps(_clean(r), default=str)
        if name in ("task_status", "task_start", "task_done"):
            from . import forecast, tasks
            cwd = os.getcwd()
            if name == "task_start":
                got = tasks.start(store, cwd, str(args.get("name") or "").strip()[:120] or "task")
                return (f"Started {got['name']!r} in {got['project']}. Estimate: {got['estimate']['text']}."
                        + "\n\n" + json.dumps(got, default=str))
            if name == "task_done":
                got = tasks.done(store, cwd, args.get("name"))
                if not got:
                    return "No task here to finish."
                return (f"Done: {got['name']}, ${got['usd']:,.2f}, {got['tokens'] / 1e6:,.1f}M tokens."
                        + "\n\n" + json.dumps(got, default=str))
            cur = tasks.current(store, cwd)
            if not cur:
                return "No task here: call task_start with a name, or work on a git branch."
            tot, est = tasks.totals(store, cur["id"]), tasks.estimate(store, cur["project_id"])
            goals = store.meta("goals") or {}
            mine = {f"goal:{g}" for g, v in goals.items() if cur["id"] in (v.get("scope") or {}).get("tasks", [])}
            budget = next((o for o in forecast.outlook(store, now, goals=True) if o["pool"] in mine), None)
            text = f"{tot['name']} ({tot['source']}): ${tot['usd']:,.2f}, {tot['tokens'] / 1e6:,.1f}M tokens so far."
            if budget:
                text += f" Budget: {budget['used']:.0f}% used."
            text += f" Estimate: {est['text']}."
            return text + "\n\n" + json.dumps({"task": tot, "estimate": est, "budget": budget}, default=str)
        if name == "suggest_setup":
            got = advise.setup(store, now)
            text = "\n".join(f"- {x['why']}: `{x['command']}`" + (f" (ask for {x['ask']})" if x["ask"] else "")
                             for x in got)
            return (text or "Nothing to set up.") + "\n\n" + json.dumps(got)
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
                      "instructions": "Usage limits for Claude Code, Codex and Hermes: call pacing_brief when limits or"
                                      " usage come up, estimate_job before a large job, and suggest_setup when"
                                      " the user wants to set savetokens up (budgets, prices)."}
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
