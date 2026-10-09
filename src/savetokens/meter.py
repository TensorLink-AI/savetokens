"""The meter: Claude's own limit readings, turned into hourly demand.

Claude reports each limit as % used and a reset time. That counts usage from
every device, claude.ai and every account's sessions, so it is the truth;
captured usage only says where it went. Demand is the hourly rise in the weekly
meter, in % of the weekly limit:

  - a rise between two readings is spread over the hours between them, in
    proportion to captured dollars (evenly when nothing was captured: usage
    elsewhere);
  - before the first reading, captured dollars are converted at the rate the
    meter shows (% per dollar), so history starts with the first transcript.

Demand sums over accounts, so switching accounts doesn't look like a drop: it
is how much you use, whichever account carries it. The 5-hour meter moves
RATIO times as fast as the weekly one; the ratio is measured from paired readings.
"""
from __future__ import annotations

import re
from collections import defaultdict

HOUR = 3600
WEEK = 7 * 86400
LIMITS = {"five_hour": 5 * HOUR, "seven_day": WEEK}
DEFAULT_RATIO = 4.0      # 5-hour % per weekly %, until readings say otherwise
HISTORY_HOURS = 28 * 24


def hour_floor(ts):
    return int(ts // HOUR) * HOUR


def readings(store, name, since=0, until=None, account=None):
    """[(ts, account, pct, resets)] oldest first, each pct the highest seen so far in its window.

    Every open session reports the meter as it last saw it, so an idle session keeps sending an older,
    lower value. The meter never falls within a window, so a lower reading is stale: it is raised to
    the window's running maximum instead of looking like the meter went down and back up.
    """
    q = "SELECT ts, account, pct, resets FROM meter WHERE name = ? AND ts >= ? AND resets IS NOT NULL"
    p = [name, since]
    if until is not None:
        q += " AND ts < ?"
        p.append(until)
    if account is not None:
        q += " AND account IS ?"
        p.append(account)
    top, out = {}, []
    for ts, acct, pct, resets in store.conn.execute(q + " ORDER BY ts", p):
        key = (acct, round(resets / HOUR))
        top[key] = max(top.get(key, pct), pct)
        out.append((ts, acct, top[key], resets))
    return out


def latest(store, name, now, account=None):
    """The open window of a limit, from its newest reading: {ts, account, pct, resets}, pct being the
    highest reading in that window (lower ones are stale, from idle sessions)."""
    q = "SELECT ts, account, pct, resets FROM meter WHERE name = ? AND resets > ? AND ts <= ?"
    p = [name, now, now]
    if account is not None:
        q += " AND account IS ?"
        p.append(account)
    r = store.conn.execute(q + " ORDER BY ts DESC LIMIT 1", p).fetchone()
    if not r:
        return None
    out = dict(r)
    out["pct"] = store.conn.execute(
        "SELECT MAX(pct) FROM meter WHERE name = ? AND account IS ? AND ABS(resets - ?) < 1800 AND ts <= ?",
        (name, r["account"], r["resets"], now)).fetchone()[0]
    return out


def active_account(store, now=None):
    q = "SELECT account FROM meter" + (" WHERE ts <= ?" if now else "") + " ORDER BY ts DESC LIMIT 1"
    r = store.conn.execute(q, (now,) if now else ()).fetchone()
    return r[0] if r else None


def accounts(store):
    return [r[0] for r in store.conn.execute("SELECT DISTINCT account FROM meter")]


def tier_multiple(tier) -> float:
    m = re.search(r"(\d+)x", tier or "")
    return float(m.group(1)) if m else 1.0


def _rises(rows):
    """[(t0, t1, rise)] between consecutive readings of each account's windows."""
    by_acct = defaultdict(list)
    for ts, acct, pct, resets in rows:
        by_acct[acct].append((ts, pct, resets))
    out = []
    for seq in by_acct.values():
        prev = None
        for ts, pct, resets in seq:
            window = round(resets / HOUR)
            if prev is None:
                pass
            elif round(prev[2] / HOUR) == window:
                if pct > prev[1]:
                    out.append((prev[0], ts, pct - prev[1]))
            elif pct > 0:   # a new window: its usage so far happened since it opened, or since the last reading
                out.append((max(resets - WEEK, prev[0]), ts, pct))
            prev = (ts, pct, resets)
    return out


def _hourly_usd(store, since, until):
    out = defaultdict(float)
    for ts, c in store.conn.execute("SELECT ts, cost_usd FROM usage WHERE ts >= ? AND ts < ? AND cost_usd > 0",
                                    (since, until)):
        out[hour_floor(ts)] += c
    return out


def _spread(t0, t1, amount, usd, into):
    """Add `amount` over the hours touching (t0, t1], weighted by captured dollars, else by overlap."""
    hours = list(range(hour_floor(t0), hour_floor(t1) + 1, HOUR)) or [hour_floor(t1)]
    w = [usd.get(h, 0.0) for h in hours]
    if sum(w) <= 0:
        w = [max(0.0, min(t1, h + HOUR) - max(t0, h)) for h in hours]
        if sum(w) <= 0:
            w = [1.0] * len(hours)
    total = sum(w)
    for h, x in zip(hours, w):
        into[h] += amount * x / total


def rate(store, until=None, gap=2 * HOUR) -> float | None:
    """% of the weekly limit per captured dollar, where both are known.

    Measured over runs of readings (same account and window, no gap over `gap`), so whole-%
    steps and the lag between a request and its reading average out.
    """
    import bisect
    runs, cur, key = [], None, None
    for ts, acct, pct, resets in readings(store, "seven_day", until=until):
        k = (acct, round(resets / HOUR))
        if cur and k == key and ts - cur[1] <= gap:
            cur[1], cur[3] = ts, pct
        else:
            if cur:
                runs.append(cur)
            cur, key = [ts, ts, pct, pct], k
    if cur:
        runs.append(cur)
    runs = [r for r in runs if r[3] > r[2]]
    if not runs:
        return None
    rows = store.conn.execute("SELECT ts, cost_usd FROM usage WHERE ts > ? AND ts <= ? AND cost_usd > 0 ORDER BY ts",
                              (min(r[0] for r in runs), max(r[1] for r in runs))).fetchall()
    ts, cum = [r[0] for r in rows], [0.0]
    for r in rows:
        cum.append(cum[-1] + r[1])
    pct = dollars = 0.0
    for t0, t1, p0, p1 in runs:
        d = cum[bisect.bisect_right(ts, t1)] - cum[bisect.bisect_right(ts, t0)]
        if d > 0:
            pct += p1 - p0
            dollars += d
    return pct / dollars if dollars > 0 else None


def five_hour_ratio(store, until=None) -> float:
    """How fast the 5-hour meter moves per weekly %: from readings taken together."""
    five = {round(ts): (pct, resets) for ts, _, pct, resets in readings(store, "five_hour", until=until)}
    week = [(round(ts), pct, resets) for ts, _, pct, resets in readings(store, "seven_day", until=until)]
    a = b = 0.0
    prev = None
    for ts, pct, resets in week:
        if ts not in five:
            continue
        cur = (pct, resets, *five[ts])
        if prev and prev[1] == cur[1] and prev[3] == cur[3] and cur[0] >= prev[0] and cur[2] >= prev[2]:
            a += cur[2] - prev[2]
            b += cur[0] - prev[0]
        prev = cur
    return a / b if b >= 5 and a > 0 else DEFAULT_RATIO


def demand(store, now, hours=HISTORY_HOURS):
    """Complete hours before now: [(hour, % of the weekly limit used)], oldest first, plus the $ rate used."""
    end = hour_floor(now)
    start = end - hours * HOUR
    rows = readings(store, "seven_day", until=end)
    rises = [r for r in _rises(rows) if r[1] > start]
    usd = _hourly_usd(store, start - WEEK, end)
    series = defaultdict(float)
    for t0, t1, rise in rises:
        _spread(max(t0, start - WEEK), min(t1, end - 1), rise, usd, series)
    r = rate(store, until=end)
    first = rows[0][0] if rows else end
    if r:
        for h, v in usd.items():
            if h < hour_floor(first):
                series[h] += v * r
    used = [h for h, v in series.items() if v > 0 and start <= h < end]
    if not used:
        return [], r
    return [(h, series.get(h, 0.0)) for h in range(min(used), end, HOUR)], r
