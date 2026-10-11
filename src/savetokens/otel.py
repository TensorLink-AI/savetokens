"""OpenTelemetry in: usage from any agent or app that exports it, with no reader written for it.

A hub accepts OTLP over HTTP with JSON (OTEL_EXPORTER_OTLP_PROTOCOL=http/json) at its own address:
POST /v1/logs and /v1/traces, with the same token as any machine (OTEL_EXPORTER_OTLP_HEADERS=
"Authorization=Bearer <token>"). Two shapes are understood:

  Claude Code's `claude_code.api_request` events (model, input/output/cache tokens, request and session ids)
  the GenAI conventions, on spans or log records: gen_ai.provider.name (or the older gen_ai.system),
    gen_ai.request.model / gen_ai.response.model, gen_ai.response.id, gen_ai.conversation.id,
    gen_ai.agent.name, gen_ai.usage.input_tokens (which includes cached tokens),
    gen_ai.usage.output_tokens, gen_ai.usage.cache_read.input_tokens and
    gen_ai.usage.cache_creation.input_tokens (and the older prompt_tokens / completion_tokens)

A tool savetokens also reads on the machine is counted once: its reader's rows replace the telemetry for the
same session (they carry more: the conversation, the folder, the branch).
"""
from __future__ import annotations

import hashlib
from datetime import datetime

from . import pricing
from .store import Usage

CLAUDE_EVENTS = ("claude_code.api_request", "api_request")


def _value(v):
    if not isinstance(v, dict):
        return v
    for k in ("stringValue", "boolValue", "doubleValue"):
        if k in v:
            return v[k]
    if "intValue" in v:
        return int(v["intValue"])
    if "arrayValue" in v:
        return [_value(x) for x in v["arrayValue"].get("values", [])]
    return None


def _attrs(items) -> dict:
    return {a.get("key"): _value(a.get("value")) for a in items or [] if isinstance(a, dict)}


def _int(d, *keys):
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return int(float(v))
            except (TypeError, ValueError):
                pass
    return None


def _ts(a, nanos):
    t = a.get("event.timestamp")
    if isinstance(t, str):
        try:
            return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    try:
        return int(nanos) / 1e9 if nanos else None
    except (TypeError, ValueError):
        return None


def _claude(a, res, ts) -> Usage | None:
    model = a.get("model")
    if not model or ts is None:
        return None
    sid = str(a.get("session.id") or res.get("session.id") or "otel")
    rid = a.get("request_id") or a.get("client_request_id") or hashlib.sha1(
        f"{sid}{ts}{model}{a.get('event.sequence')}".encode()).hexdigest()[:20]
    agent = a.get("agent.name")
    e = Usage("claude-code", sid, f"otel:{rid}", ts, model, subagent=bool(agent),
              input=_int(a, "input_tokens") or 0, output=_int(a, "output_tokens") or 0,
              cache_read=_int(a, "cache_read_tokens") or 0, cache_write_5m=_int(a, "cache_creation_tokens") or 0,
              thread=sid + (f"/{agent}" if agent else ""), provider="anthropic")
    e.cost_usd = pricing.cost(model, input=e.input, output=e.output, cache_read=e.cache_read,
                              cache_write_5m=e.cache_write_5m, fast=a.get("speed") == "fast")
    if e.cost_usd is None and a.get("cost_usd") is not None:
        e.cost_usd = float(a["cost_usd"])   # Claude Code's own estimate, when the catalogue has no price
    return e


def _genai(a, res, ts, fallback_id) -> Usage | None:
    inp = _int(a, "gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens")
    out = _int(a, "gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens")
    if (inp is None and out is None) or ts is None:
        return None
    model = a.get("gen_ai.response.model") or a.get("gen_ai.request.model")
    read = _int(a, "gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.cache_read_input_tokens") or 0
    write = _int(a, "gen_ai.usage.cache_creation.input_tokens", "gen_ai.usage.cache_creation_input_tokens") or 0
    fresh = max(0, (inp or 0) - read - write)   # the convention's input count includes cached tokens
    conv = a.get("gen_ai.conversation.id") or fallback_id[0]
    agent = a.get("gen_ai.agent.name")
    tool = str(res.get("service.name") or "otel")[:40]
    e = Usage(tool, str(conv), f"otel:{a.get('gen_ai.response.id') or fallback_id[1]}", ts, model,
              subagent=bool(agent), input=fresh, output=out or 0, cache_read=read, cache_write_5m=write,
              reasoning=_int(a, "gen_ai.usage.reasoning.output_tokens", "gen_ai.usage.reasoning_tokens"),
              thread=str(conv) + (f"/{agent}" if agent else ""),
              provider=str(a.get("gen_ai.provider.name") or a.get("gen_ai.system") or "") or None)
    e.cost_usd = pricing.cost(model, input=e.input, output=e.output, cache_read=read, cache_write_5m=write)
    return e


def parse(payload: dict) -> list[Usage]:
    """OTLP JSON (logs or traces) -> usage records."""
    out = []
    for kind, scopes_key, items_key in (("resourceLogs", "scopeLogs", "logRecords"),
                                        ("resourceSpans", "scopeSpans", "spans")):
        for rs in payload.get(kind) or []:
            res = _attrs((rs.get("resource") or {}).get("attributes"))
            for sc in rs.get(scopes_key) or []:
                for item in sc.get(items_key) or []:
                    a = _attrs(item.get("attributes"))
                    ts = _ts(a, item.get("timeUnixNano") or item.get("endTimeUnixNano")
                             or item.get("observedTimeUnixNano"))
                    body = _value(item.get("body"))
                    name = item.get("eventName") or a.get("event.name") or (body if isinstance(body, str) else None)
                    if name in CLAUDE_EVENTS:
                        e = _claude(a, res, ts)
                    else:
                        e = _genai(a, res, ts, (item.get("traceId") or "otel",
                                                item.get("spanId") or hashlib.sha1(repr(sorted(a.items())).encode())
                                                .hexdigest()[:20]))
                    if e:
                        e.machine = "otel:" + str(res.get("host.name") or res.get("service.instance.id")
                                                  or res.get("service.name") or "unknown")[:60]
                        out.append(e)
    return out


def ingest(store, payload) -> int:
    """Add telemetry usage, except sessions a machine's reader already sends (those are counted from there)."""
    events = parse(payload)
    keep = []
    for e in events:
        read = store.conn.execute("SELECT 1 FROM usage WHERE session_id = ? AND harness = ? AND machine NOT LIKE"
                                  " 'otel:%' LIMIT 1", (e.session_id, e.harness)).fetchone()
        if not read:
            keep.append(e)
    return store.add_usage(keep) if keep else 0


def replace_with_reader(store, cols, rows):
    """A machine's reader sent rows: telemetry for the same sessions goes, so nothing counts twice."""
    if "session_id" not in cols or "harness" not in cols:
        return
    i, h = cols.index("session_id"), cols.index("harness")
    pairs = {(r[i], r[h]) for r in rows}
    store.conn.executemany("DELETE FROM usage WHERE machine LIKE 'otel:%' AND session_id = ? AND harness = ?",
                           list(pairs))
    store.conn.commit()
