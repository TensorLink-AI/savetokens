"""Codex adapter: usage and rate-limit readings from Codex's own session logs.

Codex records, on every turn, the token usage and its limit windows (`rate_limits`:
used_percent, window length, reset time) in ~/.codex/sessions/**/rollout-*.jsonl.
Those readings are the series savetokens forecasts for Codex: hourly increments of
the limit percentage, so no price table is needed for OpenAI models.

Logs are read incrementally (offsets per file) and only lines that carry usage,
limits or the turn's model are parsed.
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime
from pathlib import Path

from ..store import Store, UsageEvent

HARNESS = "codex"
WINDOWS = {300: "five_hour", 10080: "seven_day"}
UNIT = {"five_hour": "codex_5h", "seven_day": "codex_week"}
THIN_SECONDS = 300   # keep at most one reading per window per 5 minutes unless the percentage moves


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ingest_file(store: Store, path: Path, state: dict | None = None) -> int:
    """New usage rows from one rollout file; limit readings go to the limits table."""
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
    state = state if state is not None else {}
    sid = state.get(str(path), {}).get("sid") or path.stem.split("-", 6)[-1]
    model = effort = None
    usage, readings = [], []
    last_total = None
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n") + 1
    for line in data[:end].splitlines():
        if b"token_count" not in line and b"turn_context" not in line and b"session_meta" not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        kind, p = d.get("type"), d.get("payload") or {}
        if kind == "session_meta":
            sid = p.get("id") or p.get("session_id") or sid
        elif kind == "turn_context":
            model, effort = p.get("model") or model, p.get("effort") or effort
        elif kind == "event_msg" and p.get("type") == "token_count":
            ts = _ts(d.get("timestamp")) or time.time()
            info = p.get("info") or {}
            last, total = info.get("last_token_usage") or {}, (info.get("total_token_usage") or {}).get("total_tokens")
            if last and total != last_total:     # Codex repeats the event when nothing new was used
                last_total = total
                cached = int(last.get("cached_input_tokens") or 0)
                usage.append(UsageEvent(HARNESS, sid, f"{sid}:{total}", ts, model=model,
                                        input=max(0, int(last.get("input_tokens") or 0) - cached),
                                        cache_read=cached,
                                        cache_write_5m=int(last.get("cache_write_input_tokens") or 0),
                                        output=int(last.get("output_tokens") or 0), provider="openai"))
            rl = p.get("rate_limits") or {}
            for slot in ("primary", "secondary"):
                w = rl.get(slot)
                if isinstance(w, dict) and WINDOWS.get(w.get("window_minutes")) and w.get("used_percent") is not None:
                    readings.append((ts, sid, WINDOWS[w["window_minutes"]], float(w["used_percent"]),
                                     w.get("resets_at"), rl.get("plan_type")))
    added = store.add_usage(usage)
    _add_readings(store, readings)
    state[str(path)] = {"sid": sid, "model": model, "effort": effort}
    store.set_offset(path, offset + end, size)
    store.conn.commit()
    return added


def _add_readings(store: Store, readings):
    """Readings go to the limits table under harness "codex" (Claude Code's limit queries exclude them)."""
    last = store.meta("codex_last_reading") or {}    # the newest reading per window, for status and risk
    rows = {"five_hour": [], "seven_day": []}
    kept = {}                                         # thinning, within this batch
    for ts, sid, window, pct, resets, plan in sorted(readings):
        k = kept.get(window)
        if k and pct == k[1] and ts - k[0] < THIN_SECONDS:
            continue
        kept[window] = (ts, pct)
        rows[window].append((ts, HARNESS, sid, pct, resets))
        if ts >= (last.get(window) or {}).get("ts", 0):
            last[window] = {"ts": ts, "pct": pct, "resets": resets, "plan": plan}
    for window, rs in rows.items():
        if rs:
            store.conn.executemany(f"INSERT INTO limits (ts, harness, session_id, {window}_pct, {window}_resets)"
                                   " VALUES (?,?,?,?,?)", rs)
    if readings:
        store.set_meta("codex_last_reading", last)


def transcripts(root: Path | None = None):
    root = root or codex_home() / "sessions"
    return sorted(root.glob("**/*.jsonl"), key=lambda p: p.stat().st_mtime) if root.exists() else []


def backfill(store: Store, root: Path | None = None, progress=None) -> int:
    files = transcripts(root)
    total, state = 0, {}
    for i, p in enumerate(files):
        total += ingest_file(store, p, state)
        if progress:
            progress(i + 1, len(files))
    return total


def latest(store: Store) -> dict:
    """{window: {"ts", "pct", "resets", "plan"}} from the most recent readings."""
    return store.meta("codex_last_reading") or {}


# ── forecasting the limit percentage itself ─────────────────────────────────

def hourly_increments(store: Store, window: str, now, hours=28 * 24):
    """(hour, percentage points added) oldest first: what each hour cost of this window.

    Codex's windows roll (each starts at first use after the last one ended), and long-running
    sessions keep reporting the window they started in, so readings are followed per window
    ("track", keyed by its reset time): increases count only within a track, and a new track's
    first reading counts only if it resets later than any track before it.
    """
    from ..windows import HOUR, hour_floor
    rows = store.conn.execute(
        f"SELECT ts, {window}_pct AS pct, {window}_resets AS resets FROM limits WHERE harness = ? AND"
        f" {window}_pct IS NOT NULL AND ts >= ? AND ts < ? ORDER BY ts", (HARNESS, now - hours * HOUR, now)).fetchall()
    tracks, newest, inc = {}, None, {}
    for r in rows:
        key = next((k for k in tracks if r["resets"] and abs(k - r["resets"]) <= 120), r["resets"] or 0)
        if key in tracks:
            d = max(0.0, r["pct"] - tracks[key])
            tracks[key] = max(tracks[key], r["pct"])
        else:
            d = r["pct"] if newest is None or key > newest else 0.0
            tracks[key] = r["pct"]
            newest = key if newest is None else max(newest, key)
        if d:
            h = hour_floor(r["ts"])
            inc[h] = inc.get(h, 0.0) + d
    if not inc:
        return []
    first, end = min(inc), hour_floor(now)
    return [(h, inc.get(h, 0.0)) for h in range(int(first), int(end), HOUR)]


def refresh(store: Store, now=None, use_ephemeris=False, log=lambda *_: None):
    """Forecast paths for each Codex window seen, in percentage points per hour."""
    from .. import ephemeris, windows
    now = now or time.time()
    start = windows.hour_floor(now)
    for window, reading in latest(store).items():
        unit = UNIT[window]
        resets = reading.get("resets") or now + 7 * 86400
        horizon = int(min(windows.MAX_HORIZON, max(6, math.ceil((resets - start) / windows.HOUR))))
        hist = hourly_increments(store, window, now)
        if not hist:
            continue
        p = windows.baseline_paths(hist, start, horizon)
        if p:
            windows.save_paths(store, "baseline", unit, now, start, horizon, p)
        if use_ephemeris and len(hist) >= windows.MIN_EPHEMERIS_HOURS and any(v for _, v in hist):
            r = ephemeris.forecast_hourly({unit: [round(v, 4) for _, v in hist]}, horizon, retries=2)
            paths = windows.copula_paths(r["quantiles"][unit])
            windows.save_paths(store, "ephemeris", unit, now, start, horizon, paths)
            store.set_meta(f"ephemeris_{unit}", {"made_at": now, "credits": r["credits"], "horizon": horizon})
            log(f"ephemeris: Codex {window} forecast, {horizon}h ({r['credits']:g} credits)")


def pressure(store: Store, now=None) -> list[dict]:
    """Codex limits in the same shape as steer.pressure: used %, forecast at reset, chance of a hit."""
    from .. import windows
    from ..steer import WINDOW_NAMES
    now = now or time.time()
    out = []
    for window, r in latest(store).items():
        if not r.get("resets") or r["resets"] <= now:
            continue
        unit = UNIT[window]
        src = "ephemeris" if store.meta(f"ephemeris_{unit}") else "baseline"
        paths = windows.load_paths(store, src, unit)
        rem = windows.remaining(paths, now, r["resets"]) if paths else None
        fc = p_hit = None
        if rem:
            totals = [r["pct"] + x for x in rem]
            fc = [round(v, 1) for v in windows.band(totals)]
            p_hit = round(sum(t >= 100 for t in totals) / len(totals), 2)
        out.append({"window": window, "name": f"Codex {WINDOW_NAMES.get(window if window != 'seven_day' else 'week')}",
                    "unit": "%", "used": r["pct"], "forecast": fc, "p_hit": p_hit, "source": src,
                    "resets": r["resets"], "harness": HARNESS, "plan": r.get("plan")})
    return out
