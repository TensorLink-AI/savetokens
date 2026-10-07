from array import array

import pytest

from savetokens import ephemeris, forecast, limits, windows
from savetokens.store import UsageEvent, load_config, save_config

from conftest import T0

H = 3600
NOW = windows.hour_floor(T0) + 1800          # half past the hour


def paths_of(rows):
    """Paths object from a list of per-path hourly lists, starting at the current hour."""
    data = array("d", [v for row in rows for v in row])
    return {"start": windows.hour_floor(NOW), "hours": len(rows[0]), "n": len(rows), "data": data}


def test_remaining_counts_partial_hours():
    p = paths_of([[2.0, 4.0, 6.0], [1.0, 1.0, 1.0]])
    start = windows.hour_floor(NOW)
    # rest of this hour (half of 2.0) + next hour (4.0)
    assert windows.remaining(p, NOW, start + 2 * H) == [1.0 + 4.0, 0.5 + 1.0]
    assert windows.remaining(p, NOW, start + 5 * H) is None          # beyond the horizon
    assert windows.remaining(p, NOW, NOW) == [0.0, 0.0]


def test_baseline_paths_replay_whole_days():
    hist = [(windows.hour_floor(T0) - (48 - i) * H, float(i % 24)) for i in range(48)]
    p = windows.baseline_paths(hist, windows.hour_floor(T0), 24, n=5)
    assert len(p) == 5 * 24 and max(p) <= 23


def test_copula_widens_window_totals():
    q = [{l: v for l, v in zip(windows.QUANTILE_GRID, (0, 1, 3, 5, 7, 9, 10))}] * 24
    indep = windows.copula_paths(q, n=400, rho=0.0, seed=1)
    corr = windows.copula_paths(q, n=400, rho=0.8, seed=1)

    def width(a):
        sums = [sum(a[p * 24:(p + 1) * 24]) for p in range(400)]
        b = windows.band(sums)
        return b[2] - b[0]
    assert width(corr) > 1.5 * width(indep)
    assert abs(windows.band(list(corr))[1] - 5) < 1


def seed(store, *, reading=True, account="acct1"):
    store.set_meta("account_plan", {"account": account, "billing": "subscription"})
    store.set_meta("claude_code_billing", "subscription")
    events = []
    for i in range(1, 15 * 24):                                   # $1/h for 15 days, two sessions
        sid = "me" if i % 2 else "other"
        events.append(UsageEvent("claude-code", sid, f"r{i}", NOW - i * H, model="claude-opus-5-5", cost_usd=1.0))
    events.append(UsageEvent("claude-code", "me", "now", NOW - 60, model="claude-opus-5-5", cost_usd=3.0))
    store.add_usage(events)
    store.set_meta("calibration", {"seven_day": {"pooled": 0.05, "models": {"claude-opus-5-5": 0.05}, "n": 5,
                                                 "r2": 0.9, "usd": {}},
                                   "five_hour": {"pooled": 1.0, "models": {"claude-opus-5-5": 1.0}, "n": 5,
                                                 "r2": 0.9, "usd": {}}})
    if reading:
        store.add_limits("claude-code", "me", ts=NOW - 10, account=account, seven_day_pct=40,
                         seven_day_resets=NOW + 2 * 86400, five_hour_pct=20, five_hour_resets=NOW + 2 * H)


def test_windows_separate_actuals_and_forecasts_at_three_levels(store):
    seed(store)
    windows.refresh(store, now=NOW, use_ephemeris=False)
    rows = {r["kind"]: r for r in windows.forecasts(store, NOW, session_id="me", sources=("baseline",))}
    assert list(rows) == ["hour", "five_hour", "day", "week"]
    hour = rows["hour"]["units"]["sub_usd"]
    assert hour["machine"] == 3.0 and hour["session"] == 3.0       # only the $3 request falls in this hour so far
    wk = rows["week"]["units"]["sub_usd"]
    assert wk["session"] < wk["machine"]                           # other sessions count for the machine only
    fc = hour["forecast"]["baseline"]
    assert fc[0] >= hour["machine"] and fc[0] <= fc[1] <= fc[2]      # forecast = so far + rest of the hour
    week = rows["week"]["pct"]
    assert week["account"] == 40                                     # Anthropic's reading, all devices
    assert week["machine"] == pytest.approx(rows["week"]["units"]["sub_usd"]["machine"] * 0.05)
    assert week["session"] < week["machine"]
    p10, p50, p90 = week["forecast"]["baseline"]
    assert 40 < p10 <= p50 <= p90                                    # reading + about $1/h * 47.5h * 0.05
    assert p50 == pytest.approx(40 + 47.5 * 0.05, rel=0.1)
    assert rows["five_hour"]["pct"]["account"] == 20 and rows["five_hour"]["pct"]["unit_of"] == "5-hour limit"


def test_other_account_readings_and_sessions_are_excluded(store):
    seed(store)
    from savetokens import limits as L
    L.record_session(store, "claude-code", "other", billing="subscription", source="statusline", account="acct2")
    store.add_limits("claude-code", "x", ts=NOW - 5, account="acct2", seven_day_pct=99,
                     seven_day_resets=NOW + 3 * 86400)
    rows = {r["kind"]: r for r in windows.forecasts(store, NOW, sources=())}
    assert rows["week"]["pct"]["account"] == 40
    assert rows["hour"]["units"]["sub_usd"]["machine"] == 3.0        # "other" session belongs to acct2


def test_refresh_with_ephemeris_sends_only_hourly_totals_and_scores(store, monkeypatch):
    seed(store)
    sent = {}

    def fake(series, horizon, key=None):
        sent.update(series=series, horizon=horizon)
        q = {l: v for l, v in zip(windows.QUANTILE_GRID, (0.2, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0))}
        return {"quantiles": {u: [q] * horizon for u in series}, "credits": 12.0, "models": ["m"]}

    monkeypatch.setattr(ephemeris, "forecast_hourly", fake)
    windows.refresh(store, now=NOW, use_ephemeris=True)
    assert set(sent["series"]) == {"sub_usd"} and all(isinstance(v, float) for v in sent["series"]["sub_usd"])
    assert 24 <= sent["horizon"] <= 7 * 24
    rows = {r["kind"]: r for r in windows.forecasts(store, NOW)}
    assert set(rows["day"]["units"]["sub_usd"]["forecast"]) == {"ephemeris", "baseline"}
    # the hour window ends; its forecasts are scored against what happened
    store.add_usage([UsageEvent("claude-code", "me", "late", NOW + 600, model="claude-opus-5-5", cost_usd=1.0)])
    sc = windows.score(store, now=NOW + H)
    assert sc[("hour", "sub_usd", "ephemeris")]["n"] >= 1 and sc[("hour", "sub_usd", "baseline")]["n"] >= 1


def test_segment_shows_actual_to_expected(store):
    seed(store)
    windows.refresh(store, now=NOW, use_ephemeris=False)
    seg = forecast.segment(store, {"session_id": "me"}, now=NOW)
    assert seg.startswith("5h 20%→") and "wk 40%→" in seg and "session" in seg and "$" not in seg


def test_per_day_outlook(store):
    seed(store)
    windows.refresh(store, now=NOW, use_ephemeris=False)
    days = windows.per_day(store, "baseline", "sub_usd", NOW)
    assert len(days) >= 2 and all(b[0] <= b[1] <= b[2] for _, b in days)
