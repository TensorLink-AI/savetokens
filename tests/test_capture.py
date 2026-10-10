"""Capture: usage and hits from transcripts, readings from the statusline."""
from __future__ import annotations

from savetokens import capture

from conftest import T0


def test_usage_is_counted_once_per_request_and_priced(store, transcript):
    t = transcript()
    t.turn(T0, blocks=3)
    t.turn(T0 + 60)
    assert capture.backfill(store) == 2
    rows = store.usage()
    assert len(rows) == 2 and all(r.cost_usd > 0 for r in rows)
    assert capture.backfill(store) == 0          # offsets: nothing read twice


def test_limit_errors_become_hits_without_their_text(store, transcript):
    t = transcript()
    t.limit_error(T0, "You've hit your session limit · resets 3:40am (Australia/Brisbane)")
    t.limit_error(T0 + 10, "You've reached your Fable 5 limit. Run /usage-credits to continue")
    t.limit_error(T0 + 20, "You have reached your weekly usage limit")
    capture.backfill(store)
    hits = [tuple(r) for r in store.conn.execute("SELECT kind, model FROM hits ORDER BY ts")]
    assert hits == [("session", None), ("model", "fable 5"), ("weekly", None)]
    assert not store.usage()                      # an error is not usage


def test_statusline_records_every_limit_reported(store):
    payload = {"rate_limits": {"five_hour": {"used_percentage": 12, "resets_at": T0 + 3600},
                               "seven_day": {"used_percentage": 30, "resets_at": T0 + 86400},
                               "seven_day_opus": {"used_percentage": 50, "resets_at": T0 + 86400},
                               "spend_limit": {"used_percentage": 5}}}
    assert capture.record_statusline(store, payload, "acct") == 3
    names = {r[0] for r in store.conn.execute("SELECT name FROM meter WHERE account = 'acct'")}
    assert names == {"five_hour", "seven_day", "seven_day_opus"}
    assert capture.record_statusline(store, {}, "acct") == 0


def test_status_breaks_usage_down_by_model(store):
    from savetokens.cli import models
    from savetokens.store import Usage
    store.add_usage([Usage("claude-code", "s", "1", T0, "claude-opus-5-5", cost_usd=6.0),
                     Usage("claude-code", "s", "2", T0, "claude-opus-5-5", subagent=True, cost_usd=3.0),
                     Usage("claude-code", "s", "3", T0, "claude-sonnet-5-5", cost_usd=1.0)])
    assert models(store, T0 - 1) == [("claude-opus-5-5", 0.9, 1 / 3), ("claude-sonnet-5-5", 0.1, 0.0)]
