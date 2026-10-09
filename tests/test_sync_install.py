"""The sync server round trip, install and uninstall, and the entry points."""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from savetokens import forecast, hooks, install, maintain, server, sync
from savetokens.store import Store

from conftest import H, T0, week_of_readings


@pytest.fixture
def running(tmp_path):
    users = server.Users(tmp_path / "srv")
    token = users.add("chris")
    dirty, lock = set(), threading.Lock()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(users, dirty, lock))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield users, token, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_two_machines_add_up_on_the_server(running, tmp_path):
    users, token, url = running
    cfg = {"server_url": url, "server_token": token}
    a, b = Store(tmp_path / "a.db"), Store(tmp_path / "b.db")
    week_of_readings(a, T0 - 48 * H, 48, per_hour=0.5, resets=T0 + 86400)
    b.add_meter("claude-code", "a1", {"seven_day": (30.0, T0 + 86400)}, ts=T0 - 60)   # the other machine is newer
    assert sync.push(a, cfg) > 0 and sync.push(b, cfg) == 1
    assert sync.push(a, cfg) == 0                      # cursors: nothing sent twice
    with users.store("chris") as s:
        assert s.conn.execute("SELECT COUNT(DISTINCT machine) FROM meter").fetchone()[0] == 2
        maintain.update(s, T0, use_ephemeris=False)   # the server's engine
    got = sync.pull(a, cfg)
    assert got["meter"] == 1 and got["paths"] == 1
    o = {x["name"]: x for x in forecast.outlook(a, T0)}["seven_day"]
    assert o["used"] == 30.0 and o["source"] == "baseline"   # b's reading, the server's paths


def test_server_rejects_unknown_tokens_and_tables(running):
    _, token, url = running
    with pytest.raises(Exception):
        sync._call({"server_url": url, "server_token": "nope"}, "/v1/pull")
    with pytest.raises(Exception):
        sync._call({"server_url": url, "server_token": token}, "/v1/push",
                   {"machine": "m", "table": "alerts", "cols": ["x"], "rows": [[1]]})


def test_install_keeps_the_users_statusline_and_uninstall_restores_it(homes):
    settings = homes / "claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    old_hook = {"hooks": [{"type": "command", "command": "/x/savetokens hook claude-code"}], "matcher": "*"}
    mine = {"hooks": [{"type": "command", "command": "my-hook"}]}
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "my-line"},
                                    "hooks": {"PostToolUse": [old_hook, mine]}}))
    assert install.install(yes=True, no_ephemeris=True, cron=False, out=lambda *_: None)
    data = json.loads(settings.read_text())
    assert data["statusLine"]["command"].endswith(" statusline")
    assert data["hooks"]["PostToolUse"] == [mine]           # an older version's hook is gone, theirs kept
    assert set(data["hooks"]) == {"PostToolUse", *install.HOOK_EVENTS}
    assert install.skill_path().read_text().startswith("---\nname: savetokens")
    install.uninstall(out=lambda *_: None)
    data = json.loads(settings.read_text())
    assert data["statusLine"]["command"] == "my-line" and data["hooks"] == {"PostToolUse": [mine]}
    assert not install.skill_path().exists()


def test_statusline_records_and_renders(store):
    week_of_readings(store, T0 - 48 * H, 48, per_hour=0.5, resets=T0 + 86400)
    maintain.update(store, T0, use_ephemeris=False)
    payload = {"rate_limits": {"seven_day": {"used_percentage": 25, "resets_at": 9e9}}}
    hooks.capture.record_statusline(store, payload, "a1")
    assert hooks.segment(store, T0).startswith("5h")


def test_prompt_hook_shows_new_alerts_once(store):
    store.conn.execute("INSERT INTO alerts (account, ts, name, window_end, stage, message) VALUES"
                       " ('a1', strftime('%s','now'), 'seven_day', 1, 'act', 'weekly limit: out soon')")
    store.conn.commit()
    assert hooks.handle("UserPromptSubmit", {}, store) == {"systemMessage": "weekly limit: out soon"}
    assert hooks.handle("UserPromptSubmit", {}, store) is None
