"""Watch the projected total for the window: flag a jump above its recent average, or a burst of swings.

Every refresh records the forecast of the window's total (windows.record). A calibrated forecaster's
projection only wobbles as usage comes in, so a jump means something surprised it, such as an
intense session starting, and swings well above the usual mean the forecast is less sure than
usual. Both can fire well before the projection reaches 100%, which notify's limit alert covers.

  jump        latest projection above the average of the last MA_HOURS by more than JUMP_SIGMAS
              of its usual hourly move, and by at least JUMP_MIN_PCT points of the limit
              (10% of the projection where there's no limit rate, e.g. API spend)
  volatility  the median hourly move (up or down) over the last VOL_HOURS more than VOL_RATIO x
              that over the NORM_HOURS before: repeated swings, not one jump
"""
from __future__ import annotations

import statistics
import time

from . import forecast, limits, windows
from .store import Store

JUMP_SIGMAS = 2.0
JUMP_MIN_PCT = 5.0
MA_HOURS = 24
NORM_HOURS = 72
VOL_HOURS = 6
VOL_RATIO = 2.0
MIN_POINTS = 6
LABELS = {"week": "weekly", "five_hour": "5-hour", "day": "today's"}


def series(store: Store, kind, source, unit, now):
    """[(made_at, p10, p50, p90)] for the window open now, oldest first, and the window's end."""
    wins = {k: (s, e) for k, s, e, _ in windows.current_windows(store, now)}
    if kind not in wins:
        return [], None
    start, end = wins[kind]
    rows = store.conn.execute(
        "SELECT made_at, p10, p50, p90 FROM window_forecasts WHERE source = ? AND unit = ? AND kind = ?"
        " AND made_at >= ? AND made_at <= ? AND ABS(window_end - ?) < 86400 ORDER BY made_at",
        (source, unit, kind, max(start, now - NORM_HOURS * 3600), now, end)).fetchall()
    return [tuple(r) for r in rows if r[2] is not None], end


def flags(store: Store, now=None, kind="week", unit="sub_usd", source=None) -> list[dict]:
    now = now or time.time()
    source = source or forecast.preferred_source(store)
    pts, end = series(store, kind, source, unit, now)
    if len(pts) < MIN_POINTS:
        return []
    window = {"week": "seven_day", "five_hour": "five_hour"}.get(kind)
    rate = limits.effective_rate(store, window, now) if unit == "sub_usd" and window else None
    scale = rate or 1.0
    fmt = (lambda v: f"{v:.0f}%") if rate else (lambda v: f"${v:,.2f}")
    ts = [p[0] for p in pts]
    vals = [p[2] * scale for p in pts]
    diffs = [(t, b - a) for t, a, b in zip(ts[1:], vals, vals[1:])]
    label = LABELS.get(kind, kind)
    out = []
    latest = vals[-1]
    prior = [v for t, v in zip(ts[:-1], vals[:-1]) if t >= now - MA_HOURS * 3600]
    if prior and len(diffs) >= MIN_POINTS - 1:
        ma = statistics.fmean(prior)
        move = statistics.pstdev([d for _, d in diffs[:-1]]) if len(diffs) > 2 else 0.0
        floor = JUMP_MIN_PCT if rate else 0.1 * ma
        if latest - ma > max(JUMP_SIGMAS * move, floor):
            out.append({"rule": "projection_jump", "kind": kind, "end": end, "level": latest,
                        "message": f"{label} projection jumped to {fmt(latest)}, from a recent average of {fmt(ma)}"
                                   f" (it usually moves about {fmt(move)} an hour)"})
    recent = [d for t, d in diffs if t >= now - VOL_HOURS * 3600]
    older = [d for t, d in diffs if t < now - VOL_HOURS * 3600]
    if len(recent) >= 3 and len(older) >= MIN_POINTS:
        a = statistics.median(abs(d) for d in recent)
        b = max(statistics.median(abs(d) for d in older), 1e-9)
        if a > VOL_RATIO * b and a > (0.5 if rate else 0.005 * latest):
            lo, hi = pts[-1][1] * scale, pts[-1][3] * scale
            out.append({"rule": "projection_volatility", "kind": kind, "end": end, "level": latest,
                        "message": f"usage is erratic: the {label} projection is swinging {a / b:.1f}x more than usual"
                                   f" (now {fmt(latest)}, likely {fmt(lo)}–{fmt(hi)})"})
    return out
