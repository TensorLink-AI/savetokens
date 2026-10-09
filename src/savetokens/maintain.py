"""Upkeep: refresh forecasts when due, raise alerts, sync with the server.

Runs in the background: kicked by the statusline and hooks while Claude Code is
open (at most every few minutes), and hourly from cron when it isn't (that is
also when Codex sessions are read, as Codex has no hooks to kick it).

Forecast cadence (refresh_due): hourly while you work, every 3 hours otherwise,
at once (but not within 15 minutes of the last) when demand breaks above the
forecast's p90, and only every 12 hours while nothing new arrives.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

from . import alerts, ephemeris, forecast, meter, pools
from .store import Store, home, load_config

KICK_SECONDS = 300


def breakout(store, now, source="ephemeris") -> bool:
    """In some pool, the last complete hour's demand rose above the forecast's p90 for that hour."""
    for pool in pools.pools(store, now):
        p = forecast.load_paths(store, pool.key, source)
        hist = pools.history(store, pool, now, hours=2)[0] if p else None
        if not hist:
            continue
        h, v = hist[-1]
        i = int((h - p["start"]) // meter.HOUR)
        if not 0 <= i < p["hours"]:
            continue
        vals = sorted(p["data"][k * p["hours"] + i] for k in range(p["n"]))
        if v > max(vals[int(0.9 * (len(vals) - 1))], 0.01):
            return True
    return False


def refresh_due(store, made_at, now, check_breakout=False) -> bool:
    if not made_at:
        return True
    last = store.last_activity()
    age = now - made_at
    if last is None or last <= made_at:
        return age >= ephemeris.MAX_AGE_SECONDS
    if age >= ephemeris.REFRESH_SECONDS:
        return True
    if age >= ephemeris.ACTIVE_SECONDS and last >= now - 3600:
        return True
    return check_breakout and age >= ephemeris.BREAKOUT_MIN_SECONDS and breakout(store, now)


def update(store, now=None, use_ephemeris=None, log=lambda *_: None) -> list[dict]:
    """The engine, the same on a machine and on the server: forecasts when due, then alerts. New alerts."""
    now = now or time.time()
    if use_ephemeris is None:
        use_ephemeris = ephemeris.enabled()
    if refresh_due(store, store.meta("forecast_made_at"), now, check_breakout=use_ephemeris):
        forecast.refresh(store, now, use_ephemeris=use_ephemeris, log=log)
    return alerts.check(store, now)


def run(store: Store, now=None, log=lambda *_: None) -> list[dict]:
    """A machine's upkeep: capture, then sync with the server if connected, else forecast here."""
    from . import capture, sync
    now = now or time.time()
    capture.backfill(store)
    cfg = load_config()
    if sync.connected(cfg):
        try:
            sync.push(store, cfg)
            sync.pull(store, cfg)
            store.conn.execute("DELETE FROM meta WHERE key = 'sync_error'")
            store.set_meta("synced_at", now)
        except Exception as e:
            log(f"sync failed: {e}")
            store.set_meta("sync_error", {"at": now, "error": str(e)[:200]})
        new = alerts.check(store, now)    # on the server's forecast paths, with the freshest local reading
    else:
        new = update(store, now, log=log)
    if new and cfg.get("desktop_notifications", True):
        alerts.desktop([a["message"] for a in new])
    return new


def kick(store: Store, now=None):
    """Start upkeep in the background, at most every KICK_SECONDS. Never blocks the caller."""
    now = now or time.time()
    if now - (store.meta("kicked_at") or 0) < KICK_SECONDS:
        return
    store.set_meta("kicked_at", now)
    log = open(home() / "upkeep.log", "a")
    subprocess.Popen([sys.executable, "-m", "savetokens", "maintain", "--quiet"], stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, start_new_session=True, env=os.environ.copy())
