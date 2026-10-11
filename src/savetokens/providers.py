"""Providers' own usage reports, on the hub: API spend no reader saw (your apps, CI, tools savetokens doesn't read).

Opt in on the hub machine, with an admin key (read from stdin, so it stays out of shell history; kept in a
file only you can read, beside that user's data; never synced):

  echo "$ANTHROPIC_ADMIN_KEY" | savetokens server --provider-key anthropic
  echo "$OPENAI_ADMIN_KEY"    | savetokens server --provider-key openai

Every hour the hub reads each provider's hourly usage by model and, per hour and model, records only what's
left after what readers and telemetry already sent for the same provider, model and hour (API-billed). So
nothing counts twice, and what remains is real API usage nobody else saw, labelled as such.

  anthropic  GET https://api.anthropic.com/v1/organizations/usage_report/messages   (x-api-key: admin key)
  openai     GET https://api.openai.com/v1/organization/usage/completions          (Bearer: admin key)
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import pricing
from .store import Usage

HOUR = 3600
LOOKBACK_HOURS = 48        # each sync re-reads this far back: providers settle late hours later
SYNC_SECONDS = 3600
NAMES = ("anthropic", "openai")


def keys_path(data_dir: Path, user: str) -> Path:
    return Path(data_dir) / "users" / f"{user}.provider_keys.json"


def load_keys(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_key(path: Path, provider: str, key: str | None):
    keys = load_keys(path)
    if key:
        keys[provider] = key
    else:
        keys.pop(provider, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(keys, f)
    os.chmod(path, 0o600)


def _get(url, headers):
    req = urllib.request.Request(url, headers={"User-Agent": "savetokens", **headers})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def anthropic(key, since, until, get=_get) -> list[dict]:
    """[{hour, model, input, cache_read, cache_write_5m, cache_write_1h, output}] from the Admin API."""
    out, page = [], None
    while True:
        q = [("starting_at", _iso(since)), ("ending_at", _iso(until)), ("bucket_width", "1h"),
             ("group_by[]", "model"), ("limit", "168")] + ([("page", page)] if page else [])
        d = get("https://api.anthropic.com/v1/organizations/usage_report/messages?" + urllib.parse.urlencode(q),
                {"x-api-key": key, "anthropic-version": "2023-06-01"})
        for b in d.get("data") or []:
            hour = datetime.fromisoformat(b["starting_at"].replace("Z", "+00:00")).timestamp()
            for r in b.get("results") or []:
                cc = r.get("cache_creation") or {}
                out.append({"hour": hour, "model": r.get("model"), "input": int(r.get("uncached_input_tokens") or 0),
                            "cache_read": int(r.get("cache_read_input_tokens") or 0),
                            "cache_write_5m": int(cc.get("ephemeral_5m_input_tokens") or 0),
                            "cache_write_1h": int(cc.get("ephemeral_1h_input_tokens") or 0),
                            "output": int(r.get("output_tokens") or 0)})
        if not d.get("has_more") or not d.get("next_page"):
            return out
        page = d["next_page"]


def openai(key, since, until, get=_get) -> list[dict]:
    """The same shape from OpenAI's organization usage API (input_tokens includes cached ones)."""
    out, page = [], None
    while True:
        q = [("start_time", int(since)), ("end_time", int(until)), ("bucket_width", "1h"), ("group_by", "model"),
             ("limit", 168)] + ([("page", page)] if page else [])
        d = get("https://api.openai.com/v1/organization/usage/completions?" + urllib.parse.urlencode(q),
                {"Authorization": f"Bearer {key}"})
        for b in d.get("data") or []:
            for r in b.get("results") or []:
                cached = int(r.get("input_cached_tokens") or 0)
                write = int(r.get("input_cache_write_tokens") or 0)
                total = int(r.get("input_tokens") or 0)
                out.append({"hour": float(b["start_time"]), "model": r.get("model"),
                            "input": max(0, total - cached - write), "cache_read": cached, "cache_write_5m": write,
                            "cache_write_1h": 0, "output": int(r.get("output_tokens") or 0)})
        if not d.get("has_more") or not d.get("next_page"):
            return out
        page = d["next_page"]


FETCH = {"anthropic": anthropic, "openai": openai}
FIELDS = ("input", "cache_read", "cache_write_5m", "cache_write_1h", "output")


def reconcile(store, provider, buckets, now=None) -> int:
    """Record, per hour and model, the provider's total less what's already counted from readers and telemetry
    (API-billed, same provider). Re-running replaces the earlier figures for those hours."""
    now = now or time.time()
    by = defaultdict(lambda: dict.fromkeys(FIELDS, 0))
    for b in buckets:
        if b["model"]:
            row = by[(b["hour"], pricing.normalize(b["model"]), b["model"])]
            for f in FIELDS:
                row[f] += b[f]
    machine = f"provider:{provider}"
    events = []
    for (hour, norm, model), tot in by.items():
        seen = dict.fromkeys(FIELDS, 0)
        for r in store.conn.execute(
                "SELECT model, SUM(input), SUM(cache_read), SUM(cache_write_5m), SUM(cache_write_1h), SUM(output)"
                " FROM usage WHERE provider = ? AND billing = 'api' AND machine != ? AND ts >= ? AND ts < ?"
                " GROUP BY model", (provider, machine, hour, hour + HOUR)):
            if pricing.normalize(r[0]) == norm:
                for f, v in zip(FIELDS, r[1:]):
                    seen[f] += v or 0
        left = {f: max(0, tot[f] - seen[f]) for f in FIELDS}
        store.conn.execute("DELETE FROM usage WHERE machine = ? AND request_id = ?",
                           (machine, f"{provider}:{int(hour)}:{model}"))
        if not any(left.values()):
            continue
        e = Usage("api", f"{provider}:{model}", f"{provider}:{int(hour)}:{model}", hour + HOUR / 2, model,
                  machine=machine, billing="api", provider=provider, **left)
        e.cost_usd = pricing.cost(model, **left)
        events.append(e)
    store.conn.commit()
    return store.add_usage(events) if events else 0


def sync(store, data_dir, user, now=None, fetch=None, log=lambda *_: None) -> dict:
    """For each provider with a key: its last LOOKBACK_HOURS, reconciled. Once an hour at most."""
    now = now or time.time()
    keys = load_keys(keys_path(data_dir, user))
    if not keys or now - (store.meta("providers_synced_at") or 0) < SYNC_SECONDS:
        return {}
    until = (now // HOUR) * HOUR
    since = until - LOOKBACK_HOURS * HOUR
    got = {}
    for name, key in keys.items():
        try:
            buckets = (fetch or FETCH)[name](key, since, until)
            got[name] = reconcile(store, name, buckets, now)
        except Exception as e:   # a bad key or an outage: the rest keep going; the error is kept, not the key
            store.set_meta(f"provider_error:{name}", {"at": now, "error": str(e)[:200]})
            log(f"provider {name}: {e}")
    store.set_meta("providers_synced_at", now)
    return got
