"""Background upkeep: billing default, limit-rate learning and the Ephemeris forecast.

The statusline only checks whether upkeep is due and, if so, starts a detached
`savetokens maintain` so it never waits on the network or a refit.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time

from . import ephemeris, limits, windows
from .store import Store, load_config

RETRY_SECONDS = 1800


def _last_usage(store: Store, harness=None):
    q, p = "SELECT MAX(ts) FROM usage", []
    if harness:
        q, p = q + " WHERE harness = ?", [harness]
    return store.conn.execute(q, p).fetchone()[0] or 0


def breakout(store: Store, now, source="ephemeris"):
    """The last complete hour's spend above the forecast's 90th percentile for that hour, in either unit."""
    h = windows.hour_floor(now) - 3600
    unit_of = windows.classifier(store)
    spent = {}
    for e in store.usage(since=h, until=h + 3600):
        u = unit_of(e)
        if u and e.cost_usd:
            spent[u] = spent.get(u, 0.0) + e.cost_usd
    for unit, x in spent.items():
        p = windows.load_paths(store, source, unit)
        if not p or not p["start"] <= h < p["start"] + p["hours"] * 3600:
            continue
        i = int((h - p["start"]) // 3600)
        v = sorted(p["data"][k * p["hours"] + i] for k in range(p["n"]))
        if x > max(v[int(0.9 * (len(v) - 1))], 0.01):
            return True
    return False


def refresh_due(store: Store, made_at, now, harness=None, check_breakout=False) -> bool:
    """Whether a forecast made at `made_at` should be remade: hourly while agents are in use,
    every 3 hours otherwise, at once on a breakout, and not while nothing has changed."""
    if not made_at:
        return True
    age = now - made_at
    last = _last_usage(store, harness)
    if last <= made_at:                                   # nothing new since: keep it, up to a point
        return age >= ephemeris.MAX_AGE_SECONDS
    if age >= ephemeris.REFRESH_SECONDS:
        return True
    if age >= ephemeris.ACTIVE_SECONDS and last >= now - 3600:
        return True
    return check_breakout and age >= ephemeris.BREAKOUT_MIN_SECONDS and breakout(store, now)


def _ephemeris_due(store: Store, now):
    if not ephemeris.enabled():
        return False
    cached = store.meta("ephemeris_hourly")
    return refresh_due(store, cached and cached["made_at"], now, check_breakout=True)


def due(store: Store, now=None) -> bool:
    now = now or time.time()
    if now - store.meta("maintain_last_try", 0) < 300:
        return False
    # hourly: refit limit rates, rebuild baseline paths and record window forecasts for scoring
    stale_fit = now - store.meta("calibration_at", 0) >= limits.REFIT_SECONDS
    stale_eph = _ephemeris_due(store, now) and now - store.meta("ephemeris_last_try", 0) >= RETRY_SECONDS
    return stale_fit or stale_eph


def run(store: Store, now=None, log=lambda *_: None):
    now = now or time.time()
    store.set_meta("maintain_last_try", now)
    plan = limits.account_plan()
    store.set_meta("claude_code_billing", plan["billing"])
    store.set_meta("account_plan", plan)
    cal = limits.calibrate(store, now)
    for window, f in cal.items():
        log(f"{window}: {f['pooled']:.3f}% per $ pooled over {f['n']} readings")
    use_eph = _ephemeris_due(store, now) and now - store.meta("ephemeris_last_try", 0) >= RETRY_SECONDS
    if use_eph:
        store.set_meta("ephemeris_last_try", now)
    try:
        windows.refresh(store, now, use_ephemeris=use_eph, log=log)
    except Exception as e:  # network or history: keep the baseline, try Ephemeris again later
        log(f"forecast: {e}")
        if use_eph:
            windows.refresh(store, now, use_ephemeris=False, log=log)
    from . import levers, steer
    from .adapters import codex
    if codex.transcripts():
        try:
            codex.backfill(store)
            made = (store.meta("ephemeris_codex_week") or {}).get("made_at", 0)
            codex.refresh(store, now, use_ephemeris=ephemeris.enabled() and refresh_due(store, made, now, "codex"),
                          log=log)
        except Exception as e:   # Codex logs or the network: keep going, try again next time
            log(f"codex: {e}")
    from . import budgets
    if budgets.configured():
        try:
            if budgets.openrouter_key():
                budgets.fetch_openrouter(store, now=now)
            made = min(((store.meta(f"budget_eph:{b['name']}") or {}).get("made_at", 0) for b in budgets.configured()),
                       default=0)
            budgets.refresh(store, now, use_ephemeris=ephemeris.enabled() and refresh_due(store, made, now),
                            log=log)
        except Exception as e:   # network or history: pacing falls back to the baseline
            log(f"budgets: {e}")
    mode, why = steer.effective_mode(store, now)   # caches auto's resolution for the guard
    log(f"mode: {mode} ({why})")
    for h, change in levers.update(store, now).items():
        log(f"levers {h}: {change}")


def kick(store: Store, now=None):
    if not due(store, now):
        return
    store.set_meta("maintain_last_try", now or time.time())
    exe = shutil.which("savetokens")
    cmd = [exe] if exe else [sys.executable, "-m", "savetokens"]
    try:
        subprocess.Popen(cmd + ["maintain", "--quiet"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass
