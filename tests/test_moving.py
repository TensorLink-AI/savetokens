"""Machines already running: moving to another hub, a hub that moves, and a hub that stops answering."""
from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from savetokens import dashboard, maintain, server, sync
from savetokens.store import Store, load_config, save_config

from conftest import H, T0, week_of_readings

ROOT = Path(__file__).resolve().parents[1]


def _hub(path):
    users = server.Users(path)
    token = users.add("chris")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(users, set(), threading.Lock()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return users, token, f"http://127.0.0.1:{httpd.server_address[1]}", httpd


def _rows(s):
    return sum(s.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("usage", "meter", "hits"))


def test_a_machine_moved_to_another_hub_sends_it_everything(running, tmp_path):
    users, token, url = running
    a = Store(tmp_path / "a.db")
    week_of_readings(a, T0 - 48 * H, 48, per_hour=0.5, resets=T0 + 86400)
    a.set_meta("budgets", {"hermes": {"usd": 100.0, "period": "month"}})
    a.set_meta("budgets_at", T0)
    first = sync.push(a, {"server_url": url, "server_token": token})
    assert first > 0
    users2, token2, url2, httpd = _hub(tmp_path / "srv2")
    try:
        assert sync.push(a, {"server_url": url2, "server_token": token2}) == first   # all of it, not just new rows
        with users2.store("chris") as s:
            assert _rows(s) == first
            assert s.meta("budgets")["hermes"]["usd"] == 100.0                     # the budget moved too
        assert sync.push(a, {"server_url": url2, "server_token": token2}) == 0    # and once only
    finally:
        httpd.shutdown()


def test_joining_again_resends_safely(running, tmp_path):
    users, token, url = running
    a = Store(tmp_path / "a.db")
    week_of_readings(a, T0 - 4 * H, 4, per_hour=0.5, resets=T0 + 86400)
    cfg = {"server_url": url, "server_token": token}
    n = sync.push(a, cfg)
    sync.restart(a)                                  # what join does
    assert sync.push(a, cfg) == n
    with users.store("chris") as s:                  # the hub kept one copy
        assert _rows(s) == n


def test_the_hub_says_its_address_and_when_each_machine_last_synced(running, tmp_path):
    users, token, url = running
    server.set_public_url(users.data, "https://hub.example.com")
    got = sync._call({"server_url": url, "server_token": token}, "/v1/pull?machine=laptop")
    assert got["hub_url"] == "https://hub.example.com"
    with users.store("chris") as s:
        assert "laptop" in s.meta("machines_synced")
        snap = dashboard.snapshot(s, on_server=True)
    assert [m for m in snap["machines"] if m["machine"] == "laptop"][0]["last_sync"] > 0


def test_a_machine_follows_its_hub_to_a_new_address_only_if_it_answers(tmp_path, monkeypatch):
    s = Store(tmp_path / "a.db")
    save_config({"server_url": "http://10.0.0.5:8787", "server_token": "t"})
    tried = []

    def call(cfg, path, body=None, timeout=30):
        tried.append(cfg["server_url"])
        if "unreachable" in cfg["server_url"]:
            raise OSError("timed out")
        return {}
    monkeypatch.setattr(sync, "_call", call)
    cfg = load_config()
    assert not sync.follow(s, cfg, "https://unreachable.example.com")
    assert load_config()["server_url"] == "http://10.0.0.5:8787"
    assert sync.follow(s, cfg, "https://hub.example.com/")
    assert load_config()["server_url"] == cfg["server_url"] == "https://hub.example.com"
    assert s.meta("hub_url") == "https://hub.example.com"          # the same hub: nothing resent
    assert not sync.follow(s, {"server_token": "t"}, "https://hub.example.com")   # set by environment: left alone


def test_a_hub_unreachable_for_a_day_is_said_once(tmp_path, monkeypatch, capsys):
    s = Store(tmp_path / "a.db")
    cfg = {"server_url": "http://160.0.0.1:20004", "server_token": "t"}
    s.set_meta("synced_at", T0 - 2 * 86400)
    s.set_meta("sync_error", {"at": T0, "error": "connection refused"})
    got = maintain.hub_quiet(s, cfg, T0)
    assert len(got) == 1 and "not reached for 2.0 days" in got[0]["message"]
    assert "savetokens join URL CODE" in got[0]["message"]
    assert maintain.hub_quiet(s, cfg, T0 + 600) == []              # once per outage
    s.set_meta("synced_at", T0 - 3600)
    assert maintain.hub_quiet(s, cfg, T0) == []                    # reached recently: quiet
    s.conn.execute("DELETE FROM meta WHERE key = 'sync_error'")
    assert sync.trouble(s, cfg) is None


def test_machines_that_synced_without_usage_are_listed():
    rows = [{"machine": "pc", "last": T0, "usd": 1.0, "n": 3}]
    got = dashboard.machine_list(rows, {"pc": T0 + 60, "hermes-box": T0 - 3 * 86400})
    assert [m["machine"] for m in got] == ["pc", "hermes-box"]
    assert got[1]["requests"] == 0 and got[1]["last_sync"] == T0 - 3 * 86400


def test_add_a_machine_offers_new_existing_and_container():
    html = (ROOT / "src/savetokens/web/index.html").read_text()
    for part in ("NEW MACHINE", "ALREADY HAS SAVETOKENS", "CONTAINER OR CI", 'id="add-join"'):
        assert part in html
    js = (ROOT / "src/savetokens/web/app.js").read_text()
    assert "savetokens join ${url} ${got.code}" in js and "QUIET" in js
