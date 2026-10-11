"""Sync with a savetokens server, so every machine and account adds up to one forecast.

Each machine pushes what it captured (usage counts, meter readings, limit hits)
and any API budget set on it, and pulls back the other machines' meter readings
and hits, the budgets, and the server's forecast paths. Alerts are then worked out locally from the same paths and the
freshest reading, so the statusline needs no network. Nothing else is sent:
no prompts, replies, file names or projects.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .store import NEW_USAGE, SYNCED, Store

LEGACY_RETRY = 86400   # after a day, try the newer columns again (the hub may have been upgraded)

BATCH = 5000


def settings(cfg) -> dict:
    """Server URL and token from the config, or from SAVETOKENS_SERVER / SAVETOKENS_TOKEN (handy in
    containers and cloud sessions, where there's no config file)."""
    return {"server_url": cfg.get("server_url") or os.environ.get("SAVETOKENS_SERVER"),
            "server_token": cfg.get("server_token") or os.environ.get("SAVETOKENS_TOKEN")}


def connected(cfg) -> bool:
    s = settings(cfg)
    return bool(s["server_url"] and s["server_token"])


def _call(cfg, path, body=None, timeout=30):
    cfg = settings(cfg)
    req = urllib.request.Request(cfg["server_url"].rstrip("/") + path,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {cfg['server_token']}",
                                          "Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def push(store: Store, cfg) -> int:
    """This machine's new rows, in batches. Returns rows sent."""
    sent = 0
    machine = store.machine
    for table, cols in SYNCED.items():
        legacy = time.time() - (store.meta("hub_legacy_usage") or 0) < LEGACY_RETRY
        if table == "usage" and legacy:
            cols = [c for c in cols if c not in NEW_USAGE]
        cursor = store.meta(f"pushed:{table}", 0)
        while True:
            rows = store.conn.execute(f"SELECT rowid, {','.join(cols)} FROM {table} WHERE rowid > ? AND machine = ?"
                                      f" ORDER BY rowid LIMIT {BATCH}", (cursor, machine)).fetchall()
            if not rows:
                break
            try:
                _call(cfg, "/v1/push", {"machine": machine, "table": table, "cols": cols,
                                        "rows": [list(r)[1:] for r in rows]})
            except urllib.error.HTTPError as e:
                if e.code != 400 or table != "usage" or legacy:
                    raise
                legacy = True
                store.set_meta("hub_legacy_usage", time.time())   # a hub from before v5: send what it knows
                cols = [c for c in cols if c not in NEW_USAGE]
                continue
            cursor = rows[-1][0]
            store.set_meta(f"pushed:{table}", cursor)
            sent += len(rows)
    at = store.meta("budgets_at")
    if at and at > (store.meta("pushed:budgets") or 0):
        _call(cfg, "/v1/settings", {"budgets": store.meta("budgets") or {}, "plans": store.meta("plans") or {},
                                    "tz": time.localtime().tm_gmtoff, "at": at})
        store.set_meta("pushed:budgets", at)
    from . import setup
    found = setup.detected()   # plan names and list prices, so the server's setup list can name them
    if found != store.meta("pushed:detected"):
        try:
            _call(cfg, "/v1/settings", {"machine": machine, "detected": found})
            store.set_meta("pushed:detected", found)
        except Exception:   # an older server: it just can't name the plans
            pass
    return sent


def pull(store: Store, cfg) -> dict:
    """Other machines' readings and hits, and the server's newest forecast paths."""
    q = {"machine": store.machine, "meter": store.meta("pulled:meter", 0), "hits": store.meta("pulled:hits", 0),
         "paths_after": store.meta("pulled:paths", 0)}
    got = _call(cfg, "/v1/pull?" + urllib.parse.urlencode(q))
    for table in ("meter", "hits"):
        part = got.get(table) or {}
        if part.get("rows"):
            store.insert(table, SYNCED[table], part["rows"])
        store.set_meta(f"pulled:{table}", part.get("cursor", q[table]))
    newest = q["paths_after"]
    for p in got.get("paths", []):
        store.conn.execute("INSERT OR REPLACE INTO paths VALUES (?,?,?,?,?,?,?)",
                           (p["account"], p["source"], p["made_at"], p["start_hour"], p["hours"], p["n"],
                            base64.b64decode(p["data"])))
        newest = max(newest, p["made_at"])
    store.conn.commit()
    store.set_meta("pulled:paths", newest)
    if got.get("forecast_made_at"):
        store.set_meta("forecast_made_at", got["forecast_made_at"])
    for key in ("ephemeris_last", "track_record"):
        if got.get(key):
            store.set_meta(key, got[key])
    if got.get("budgets_at") and got["budgets_at"] > (store.meta("budgets_at") or 0):   # set on another machine
        store.set_meta("budgets", got.get("budgets") or {})
        store.set_meta("plans", got.get("plans") or {})
        store.set_meta("budgets_at", got["budgets_at"])
        store.set_meta("pushed:budgets", got["budgets_at"])
    return {"meter": len((got.get("meter") or {}).get("rows", [])), "paths": len(got.get("paths", []))}
