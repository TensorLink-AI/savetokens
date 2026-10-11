"""Providers' usage reports on the hub: only API usage no reader saw is added, and keys stay private."""
from __future__ import annotations

import os
import stat

import pytest

from savetokens import cli, providers
from savetokens.store import Usage

from conftest import H, T0

HOUR0 = (T0 // H) * H - 2 * H


def _anthropic_page(model="claude-opus-5-5", uncached=1_000_000, read=0, out=100_000):
    """As documented: GET /v1/organizations/usage_report/messages."""
    iso = lambda ts: __import__("datetime").datetime.fromtimestamp(ts, __import__("datetime").timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"data": [{"starting_at": iso(HOUR0), "ending_at": iso(HOUR0 + H),
                      "results": [{"model": model, "uncached_input_tokens": uncached, "cache_read_input_tokens": read,
                                   "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
                                   "output_tokens": out, "api_key_id": None, "workspace_id": None}]}],
            "has_more": False, "next_page": None}


def test_anthropic_and_openai_reports_are_read_as_documented():
    seen = []

    def get(url, headers):
        seen.append((url, headers))
        return _anthropic_page()
    b = providers.anthropic("sk-ant-admin-x", HOUR0, HOUR0 + H, get=get)
    assert b == [{"hour": HOUR0, "model": "claude-opus-5-5", "input": 1_000_000, "cache_read": 0,
                  "cache_write_5m": 0, "cache_write_1h": 0, "output": 100_000}]
    assert "usage_report/messages" in seen[0][0] and "bucket_width=1h" in seen[0][0]
    assert seen[0][1]["x-api-key"] == "sk-ant-admin-x" and "anthropic-version" in seen[0][1]

    pages = [{"data": [{"start_time": HOUR0, "end_time": HOUR0 + H, "results": [
        {"object": "organization.usage.completions.result", "model": "gpt-6-sol", "input_tokens": 50_000,
         "input_cached_tokens": 40_000, "input_cache_write_tokens": 0, "output_tokens": 2_000,
         "num_model_requests": 7}]}], "has_more": True, "next_page": "p2"},
             {"data": [], "has_more": False, "next_page": None}]
    urls = []

    def get2(url, headers):
        urls.append(url)
        assert headers["Authorization"] == "Bearer sk-admin-y"
        return pages[len(urls) - 1]
    b = providers.openai("sk-admin-y", HOUR0, HOUR0 + H, get=get2)
    assert b[0]["input"] == 10_000 and b[0]["cache_read"] == 40_000 and "page=p2" in urls[1]


def test_only_what_no_reader_saw_is_added_and_a_rerun_replaces_it(store):
    store.add_usage([Usage("claude-code", "s", "r1", HOUR0 + 600, "claude-opus-5-5", input=600_000, output=60_000,
                           billing="api", provider="anthropic", cost_usd=1.0)])
    b = providers.anthropic("k", HOUR0, HOUR0 + H, get=lambda u, h: _anthropic_page())
    assert providers.reconcile(store, "anthropic", b) == 1
    row = store.conn.execute("SELECT harness, input, output, billing, cost_usd FROM usage WHERE machine ="
                             " 'provider:anthropic'").fetchone()
    assert tuple(row)[:4] == ("api", 400_000, 40_000, "api") and row[4] > 0
    store.add_usage([Usage("codex", "c", "r2", HOUR0 + 900, "claude-opus-5-5", input=400_000, output=40_000,
                           billing="api", provider="anthropic", cost_usd=1.0)])   # a reader caught up
    providers.reconcile(store, "anthropic", b)
    assert store.conn.execute("SELECT COUNT(*) FROM usage WHERE machine = 'provider:anthropic'").fetchone()[0] == 0


def test_keys_are_private_and_sync_runs_hourly_keeping_errors_not_keys(store, tmp_path, monkeypatch):
    path = providers.keys_path(tmp_path, "me")
    providers.save_key(path, "anthropic", "sk-ant-admin-secret")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    calls = []

    def boom(key, since, until):
        calls.append(key)
        raise RuntimeError("HTTP 401")
    assert providers.sync(store, tmp_path, "me", T0, fetch={"anthropic": boom}) == {}
    assert "secret" not in str(store.meta("provider_error:anthropic"))
    assert providers.sync(store, tmp_path, "me", T0 + 60, fetch={"anthropic": boom}) == {} and len(calls) == 1


def test_the_server_command_takes_a_key_on_stdin(tmp_path, monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("sk-ant-admin-z\n"))
    assert cli.main(["server", "--data", str(tmp_path), "--provider-key", "anthropic"]) == 0
    assert providers.load_keys(providers.keys_path(tmp_path, "me")) == {"anthropic": "sk-ant-admin-z"}
    assert "sk-ant-admin-z" not in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["server", "--provider-key"])
