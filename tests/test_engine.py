"""Forecasting through Gnomon: Ephemeris first, the baseline as fallback, every forecast scored."""
from __future__ import annotations

import json
import urllib.request

import pytest

from savetokens import engine, forecast, install, maintain, server, sync

from conftest import H, T0, week_of_readings


class FakeEphemeris:
    """Stands in for Gnomon's EphemerisProvider: 1% an hour, and no network."""
    calls = 0

    def __init__(self, *a, **k):
        from gnomon import AdapterCapabilities
        self.capabilities = AdapterCapabilities(quantiles=True, min_history=2)
        self.revision = None

    def forecast(self, request):
        from gnomon import ForecastResult
        FakeEphemeris.calls += 1
        q = tuple({p: 0.5 + p for p in request.quantiles} for _ in range(request.horizon))
        return ForecastResult(point=tuple(1.0 for _ in range(request.horizon)), quantiles=q,
                              series_id=request.series_id, unit=request.unit, timestamps=request.future_timestamps)


@pytest.fixture
def fake_ephemeris(monkeypatch):
    import gnomon
    monkeypatch.setattr(gnomon, "EphemerisProvider", FakeEphemeris)
    monkeypatch.setenv("EPHEMERIS_API_KEY", "test-key")
    FakeEphemeris.calls = 0


def test_ephemeris_forecasts_through_gnomon_and_lands_in_the_ledger(store, fake_ephemeris):
    week_of_readings(store, T0 - 72 * H, 72, per_hour=0.5, resets=T0 + 86400)
    assert forecast.refresh(store, T0, use_ephemeris=True) == ["ephemeris", "baseline"]
    assert FakeEphemeris.calls == 1
    o = {x["name"]: x for x in forecast.outlook(store, T0)}["seven_day"]
    assert o["source"] == "ephemeris" and 55 < o["p50"] < 70        # 35.5% now + ~1% an hour for ~24h
    eng = engine.Engine(forecast.ledger_path(store), use_ephemeris=False)
    found = eng.ledger.search(series_id=engine.series_id("a1"))["items"]
    assert {r["provider"] for r in found} == {"ephemeris", "baseline"}


def test_a_failing_ephemeris_leaves_the_baseline(store, monkeypatch):
    import gnomon

    class Down(FakeEphemeris):
        def forecast(self, request):
            raise RuntimeError("service unavailable")
    monkeypatch.setattr(gnomon, "EphemerisProvider", Down)
    monkeypatch.setenv("EPHEMERIS_API_KEY", "k")
    week_of_readings(store, T0 - 72 * H, 72, per_hour=0.5, resets=T0 + 86400)
    assert forecast.refresh(store, T0, use_ephemeris=True) == ["baseline"]
    assert store.meta("ephemeris_error")      # Gnomon withholds the provider's text, in case it holds secrets


def test_forecasts_are_scored_once_their_hours_pass(store):
    week_of_readings(store, T0 - 96 * H, 96, per_hour=0.5, resets=T0 + 86400)
    store.conn.execute("DELETE FROM meter WHERE ts > ?", (T0 - 30 * H,))      # as it was 30 hours ago
    forecast.refresh(store, T0 - 30 * H, use_ephemeris=False)
    week_of_readings(store, T0 - 96 * H, 96, per_hour=0.5, resets=T0 + 86400)  # the rest arrives
    forecast.refresh(store, T0, use_ephemeris=False)
    tr = store.meta("track_record")
    assert tr["baseline"]["forecasts"] >= 1 and tr["baseline"]["mae"] >= 0


def test_fresh_server_makes_a_first_token_and_answers_health(tmp_path):
    import threading
    from http.server import ThreadingHTTPServer
    users = server.Users(tmp_path / "srv")
    token = server.first_user(users, log=lambda *_: None)
    assert token.startswith("st_") and users.who(token) == "me"
    assert (tmp_path / "srv" / "first-token.txt").read_text().strip() == token
    assert server.first_user(users, log=lambda *_: None) is None          # only once
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(users, set(), threading.Lock()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    assert json.load(urllib.request.urlopen(url + "/healthz"))["ok"]
    with users.store("me") as s:
        week_of_readings(s, T0 - 48 * H, 48, per_hour=0.5, resets=T0 + 86400)
    req = urllib.request.Request(url + "/v1/status", headers={"Authorization": f"Bearer {token}"})
    assert "outlook" in json.load(urllib.request.urlopen(req))
    httpd.shutdown()


def test_server_settings_from_the_environment(monkeypatch):
    assert not sync.connected({})
    monkeypatch.setenv("SAVETOKENS_SERVER", "https://st.example.com")
    monkeypatch.setenv("SAVETOKENS_TOKEN", "st_x")
    assert sync.connected({}) and sync.settings({})["server_url"] == "https://st.example.com"


def test_install_with_a_server_connects_and_never_asks_for_a_key(homes, monkeypatch):
    asked = []
    monkeypatch.setattr(maintain, "run", lambda s, **k: [])
    assert install.install(yes=False, server="http://127.0.0.1:9", token="st_x", cron=False,
                           out=lambda *_: None, ask=lambda q: asked.append(q) or "y")
    assert asked[0] == "Proceed? [y/N] " and not any("Ephemeris" in q for q in asked)
    from savetokens.store import load_config
    assert load_config()["server_url"] == "http://127.0.0.1:9"
