"""The meter: a subscription's own limit readings, turned into hourly demand.

Claude Code (statusline) and Codex (session logs) report each limit as % used
and a reset time. That counts usage from every device and session on the plan,
so it is the truth; captured usage only says where it went. Demand is the hourly
rise in the weekly meter, in % of the weekly limit, per tool (`harness`):

  - a rise between two readings is spread over the hours between them, in
    proportion to captured usage (evenly when nothing was captured: usage
    elsewhere);
  - before the first reading, captured usage is converted at the rate the
    meter shows (% per dollar), so history starts with the first transcript.

Captured usage is weighed in API dollars where the model has a price, else in
millions of tokens (Codex models): see WEIGHT. API-billed usage is left out;
it has no meter and goes to its own budget.

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
CLAUDE = "claude-code"
# what a request weighs, for spreading meter rises: its API price, else millions of tokens
WEIGHT = ("COALESCE(cost_usd, (input + output + cache_write_5m + cache_write_1h + 0.1 * cache_read) / 1e6)")
SUBSCRIPTION = "billing IS NULL"


def hour_floor(ts):
    return int(ts // HOUR) * HOUR


RESET_LOOKAHEAD = HOUR   # a lower reading with nothing back at the old level within this long is a reset


def readings(store, name, since=0, until=None, account=None, harness=CLAUDE):
    """[(ts, account, pct, resets)] oldest first, stale readings raised to the level they lag behind.

    Every open session reports the meter as it last saw it, so an idle session keeps sending an older,
    lower value while active ones report the current one. Such a lower reading is stale: it is raised to
    the window's running maximum. But a limit can also be reset early with its reset time unchanged: then
    the meter really drops, and no reading comes back to the old level. So a lower reading counts as stale
    only if a reading at the old level follows within RESET_LOOKAHEAD; if readings follow and all stay
    lower, the window starts again from it (until readings follow, it can't be told, and stays stale).
    """
    q = "SELECT ts, account, pct, resets FROM meter WHERE harness = ? AND name = ? AND ts >= ? AND resets IS NOT NULL"
    p = [harness, name, since]
    if until is not None:
        q += " AND ts < ?"
        p.append(until)
    if account is not None:
        q += " AND account IS ?"
        p.append(account)
    rows = _own_windows(store.conn.execute(q + " ORDER BY ts", p).fetchall(), harness)
    # per (account, window): the highest reading from each reading's time to RESET_LOOKAHEAD after it
    ahead, later = [0.0] * len(rows), [False] * len(rows)
    by = defaultdict(list)
    for i, (ts, acct, pct, resets) in enumerate(rows):
        by[(acct, round(resets / HOUR))].append(i)
    for idx in by.values():
        window = []                           # a monotone deque of (ts, pct) over the next hour, scanning back
        for k in range(len(idx) - 1, -1, -1):
            ts, pct = rows[idx[k]][0], rows[idx[k]][2]
            while window and window[0][0] > ts + RESET_LOOKAHEAD:
                window.pop(0)
            ahead[idx[k]] = max([pct] + [w[1] for w in window])
            later[idx[k]] = bool(window)       # readings after it, within the lookahead, to judge it by
            while window and window[-1][1] <= pct:
                window.pop()
            window.append((ts, pct))
    top, out = {}, []
    for i, (ts, acct, pct, resets) in enumerate(rows):
        key = (acct, round(resets / HOUR))
        if key in top and pct < top[key] and later[i] and ahead[i] < top[key]:
            top[key] = pct                    # reset early: what follows stays below the old level
        top[key] = max(top.get(key, pct), pct)
        out.append((ts, acct, top[key], resets))
    return out


def _own_windows(rows, harness):
    """One Claude account's windows follow each other: once a later window has started, a reading from an
    earlier one isn't this account's current meter (another account's, misattributed, or a stale session's).
    Codex accounts can't be told apart, so their windows interleave and are kept."""
    if harness != CLAUDE:
        return rows
    newest, out = {}, []
    for r in rows:
        ts, acct, pct, resets = r
        if acct is not None and resets < newest.get(acct, resets) - 1800:
            continue
        newest[acct] = max(newest.get(acct, resets), resets)
        out.append(r)
    return out


def latest(store, name, now, account=None, harness=CLAUDE):
    """The open window of a limit, from its newest reading: {ts, account, pct, resets}, pct being the
    highest reading in that window (lower ones are stale, from idle sessions)."""
    q = "SELECT ts, account, pct, resets FROM meter WHERE harness = ? AND name = ? AND resets > ? AND ts <= ?"
    p = [harness, name, now, now]
    if account is not None:
        q += " AND account IS ?"
        p.append(account)
    r = store.conn.execute(q + " ORDER BY ts DESC LIMIT 1", p).fetchone()
    if not r:
        return None
    if harness == CLAUDE and r["account"] is not None:   # the account's newest window, not an earlier one's reading
        r = store.conn.execute("SELECT ts, account, pct, resets FROM meter WHERE harness = ? AND name = ? AND resets > ?"
                               " AND ts <= ? AND account IS ? ORDER BY resets DESC, ts DESC LIMIT 1",
                               (harness, name, now, now, r["account"])).fetchone()
    out = dict(r)
    seen = [x for x in readings(store, name, since=r["resets"] - WEEK - HOUR, until=now + 1, account=r["account"],
                                harness=harness) if abs(x[3] - r["resets"]) < 1800]
    out["pct"] = seen[-1][2] if seen else r["pct"]   # stale readings raised; an early reset followed
    return out


def active_account(store, now=None, harness=CLAUDE):
    q = "SELECT account FROM meter WHERE harness = ?" + (" AND ts <= ?" if now else "") + " ORDER BY ts DESC LIMIT 1"
    r = store.conn.execute(q, (harness, now) if now else (harness,)).fetchone()
    return r[0] if r else None


def recent_accounts(store, since, harness=CLAUDE):
    """The named accounts with limit readings since `since`, the latest read first."""
    return [r[0] for r in store.conn.execute(
        "SELECT account FROM meter WHERE harness = ? AND ts >= ? AND account IS NOT NULL GROUP BY account"
        " ORDER BY MAX(ts) DESC", (harness, since))]


def accounts(store, harness=CLAUDE):
    return [r[0] for r in store.conn.execute("SELECT DISTINCT account FROM meter WHERE harness = ?", (harness,))]


def harnesses(store, since=0):
    """Tools with meter readings since `since`: each has a subscription pool."""
    return [r[0] for r in store.conn.execute("SELECT DISTINCT harness FROM meter WHERE ts >= ? ORDER BY harness",
                                             (since,))]


def tier_multiple(tier) -> float:
    m = re.search(r"(\d+)x", tier or "")
    return float(m.group(1)) if m else 1.0


def _rises(rows):
    """[(t0, t1, rise)] between readings of the same window, per account.

    Each window is followed on its own, so readings from two windows that interleave (two accounts
    that can't be told apart, as with Codex) don't look like a window opening again and again."""
    by_acct = defaultdict(list)
    for ts, acct, pct, resets in rows:
        by_acct[acct].append((ts, pct, resets))
    out = []
    for seq in by_acct.values():
        last, prev_ts, first = {}, None, True
        for ts, pct, resets in seq:
            window = round(resets / HOUR)
            if window in last:
                if pct > last[window][1]:
                    out.append((last[window][0], ts, pct - last[window][1]))
            elif not first and pct > 0:   # a new window: its usage so far happened since it opened, or since the last reading
                out.append((max(resets - WEEK, prev_ts), ts, pct))
            if window not in last or pct != last[window][1]:   # higher: used; lower: reset early
                last[window] = (ts, pct)
            prev_ts, first = ts, False
    return out


def _hourly_usd(store, since, until, harness=CLAUDE, account=None):
    """Captured subscription usage per hour, by WEIGHT (one account's, with `account`)."""
    out = defaultdict(float)
    one = " AND account IS ?" if account is not None else ""
    for ts, c in store.conn.execute(f"SELECT ts, {WEIGHT} FROM usage WHERE harness = ? AND {SUBSCRIPTION}"
                                    f" AND ts >= ? AND ts < ?{one}",
                                    (harness, since, until) + ((account,) if account is not None else ())):
        if c and c > 0:
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


def rate(store, until=None, gap=2 * HOUR, harness=CLAUDE, account=None) -> float | None:
    """% of the weekly limit per captured dollar (per WEIGHT unit), where both are known.

    Measured over runs of readings (same account and window, no gap over `gap`), so whole-%
    steps and the lag between a request and its reading average out. With `account`, from that
    account's readings and usage only (a Max 5x and a Max 20x account have different rates), falling
    back to every account's together until it has enough of its own.
    """
    if account is not None:
        own = _rate(store, until, gap, harness, account)
        return own if own is not None else _rate(store, until, gap, harness, None)
    return _rate(store, until, gap, harness, None)


def _rate(store, until, gap, harness, account):
    """(% points, dollars) summed over each account's runs of readings, each against its own usage; as a rate."""
    pct = dollars = 0.0
    # every named account on its own (readings without one only when there's no named account at all:
    # readings(account=None) is every reading, so mixing it in would count them twice)
    accts = [account] if account is not None else ([a for a in accounts(store, harness) if a] or [None])
    for acct in accts:
        p, d = _account_rate(store, until, gap, harness, acct)
        pct += p
        dollars += d
    return pct / dollars if dollars > 0 else None


def _account_rate(store, until, gap, harness, account):
    import bisect
    runs, cur, key = [], None, None
    for ts, _, pct, resets in readings(store, "seven_day", until=until, account=account, harness=harness):
        k = round(resets / HOUR)
        if cur and k == key and ts - cur[1] <= gap and pct >= cur[3]:
            cur[1], cur[3] = ts, pct
        else:
            if cur:
                runs.append(cur)
            cur, key = [ts, ts, pct, pct], k
    if cur:
        runs.append(cur)
    runs = [r for r in runs if r[3] > r[2]]
    if not runs:
        return 0.0, 0.0
    rows = store.conn.execute(f"SELECT ts, {WEIGHT} AS w FROM usage WHERE harness = ? AND {SUBSCRIPTION}"
                              f" AND ts > ? AND ts <= ? AND {WEIGHT} > 0 AND account IS ? ORDER BY ts",
                              (harness, min(r[0] for r in runs), max(r[1] for r in runs), account)).fetchall()
    if not rows and account is not None:   # usage read before accounts were kept: count it all
        rows = store.conn.execute(f"SELECT ts, {WEIGHT} AS w FROM usage WHERE harness = ? AND {SUBSCRIPTION}"
                                  f" AND ts > ? AND ts <= ? AND {WEIGHT} > 0 ORDER BY ts",
                                  (harness, min(r[0] for r in runs), max(r[1] for r in runs))).fetchall()
    ts, cum = [r[0] for r in rows], [0.0]
    for r in rows:
        cum.append(cum[-1] + r[1])
    pct = dollars = 0.0
    for t0, t1, p0, p1 in runs:
        d = cum[bisect.bisect_right(ts, t1)] - cum[bisect.bisect_right(ts, t0)]
        if d > 0:
            pct += p1 - p0
            dollars += d
    return pct, dollars


def five_hour_ratio(store, until=None, harness=CLAUDE) -> float:
    """How fast the 5-hour meter moves per weekly %: from readings taken together."""
    five = {round(ts): (pct, resets) for ts, _, pct, resets in readings(store, "five_hour", until=until,
                                                                        harness=harness)}
    week = [(round(ts), pct, resets) for ts, _, pct, resets in readings(store, "seven_day", until=until,
                                                                        harness=harness)]
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


def demand(store, now, hours=HISTORY_HOURS, harness=CLAUDE, account=None):
    """Complete hours before now: [(hour, % of the weekly limit used)], oldest first, plus the $ rate used.
    Every account's together, or one account's (when several are in use, each has its own limits)."""
    end = hour_floor(now)
    start = end - hours * HOUR
    rows = readings(store, "seven_day", until=end, account=account, harness=harness)
    rises = [r for r in _rises(rows) if r[1] > start]
    usd = _hourly_usd(store, start - WEEK, end, harness, account)
    series = defaultdict(float)
    for t0, t1, rise in rises:
        _spread(max(t0, start - WEEK), min(t1, end - 1), rise, usd, series)
    r = rate(store, until=end, harness=harness, account=account)
    first = rows[0][0] if rows else end
    if r:
        for h, v in usd.items():
            if h < hour_floor(first):
                series[h] += v * r
    used = [h for h, v in series.items() if v > 0 and start <= h < end]
    if not used:
        return [], r
    return [(h, series.get(h, 0.0)) for h in range(min(used), end, HOUR)], r


def spend(store, now, hours=HISTORY_HOURS, harness=CLAUDE):
    """API-billed dollars per complete hour before now: [(hour, $)], oldest first."""
    end = hour_floor(now)
    got = defaultdict(float)
    for ts, c in store.conn.execute("SELECT ts, cost_usd FROM usage WHERE harness = ? AND billing = 'api'"
                                    " AND ts >= ? AND ts < ? AND cost_usd > 0", (harness, end - hours * HOUR, end)):
        got[hour_floor(ts)] += c
    if not got:
        return []
    return [(h, got.get(h, 0.0)) for h in range(min(got), end, HOUR)]
