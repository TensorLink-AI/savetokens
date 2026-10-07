"""Projection watch: a jump above the recent average, and swings beyond the usual."""
from __future__ import annotations

from conftest import T0
from savetokens import projection, windows

H = 3600


def _record(store, now, values, source="baseline"):
    """One recorded weekly projection per hour, ending at `now`."""
    end = {k: e for k, s, e, _ in windows.current_windows(store, now)}["week"]
    n = len(values)
    store.conn.executemany("INSERT INTO window_forecasts VALUES (?,?,?,?,?,?,?,?,?,?)",
                           [(source, "sub_usd", "week", end - 7 * 86400, end, now - (n - 1 - i) * H, 0.0,
                             v * 0.8, v, v * 1.2) for i, v in enumerate(values)])
    store.conn.commit()


def test_steady_projection_raises_nothing(store):
    _record(store, T0, [100 + (i % 3) for i in range(30)])
    assert projection.flags(store, T0, source="baseline") == []


def test_jump_above_the_recent_average_is_flagged(store):
    _record(store, T0, [100 + (i % 3) for i in range(29)] + [140])
    f = projection.flags(store, T0, source="baseline")
    assert [x["rule"] for x in f] == ["projection_jump"]
    assert "$140.00" in f[0]["message"]


def test_small_jumps_within_the_floor_are_ignored(store):
    _record(store, T0, [100.0] * 29 + [105])     # under 10% of the projection
    assert projection.flags(store, T0, source="baseline") == []


def test_swings_beyond_the_usual_are_flagged(store):
    calm = [100 + (i % 2) for i in range(24)]
    wild = [100, 112, 98, 115, 101, 99]
    f = projection.flags(store, T0, source="baseline")
    _record(store, T0, calm + wild)
    rules = [x["rule"] for x in projection.flags(store, T0, source="baseline")]
    assert "projection_volatility" in rules and f == []


def test_notify_sends_each_flag_once_per_level(store, monkeypatch):
    from savetokens import forecast, notify
    monkeypatch.setattr(forecast, "preferred_source", lambda s: "baseline")
    _record(store, T0, [100 + (i % 3) for i in range(29)] + [140])
    first = [m for m in notify.alerts(store, T0) if "projection" in m]
    again = [m for m in notify.alerts(store, T0 + 60) if "projection" in m]
    assert len(first) == 1 and again == []
