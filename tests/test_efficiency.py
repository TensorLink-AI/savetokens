"""The efficiency engine: facts and what-ifs from any tool's records, prices from the catalogue, no rules."""
from __future__ import annotations

import json
import sqlite3

from savetokens import efficiency, pricing
from savetokens.store import Store, Usage

from conftest import H, T0

D = 86400
OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"


def _turns(store, thread, prompts, model=OPUS, cached=True, out=100, harness="claude-code", provider=None, **kw):
    """A conversation: each request's prompt size; the part sent before comes from the cache (or not)."""
    rows, prev = [], None
    for i, p in enumerate(prompts):
        carried = min(p, prev + out) if prev is not None else 0
        read = carried if cached else 0
        e = Usage(harness, thread.split("/")[0], f"{thread}:{i}", T0 - H + i * 60, model, input=p - read,
                  cache_read=read, output=out, thread=thread, provider=provider, **kw)
        e.cost_usd = pricing.cost(model, input=e.input, cache_read=read, output=out)
        rows.append(e)
        prev = p
    store.add_usage(rows)


def test_context_facts_follow_each_conversation(store):
    _turns(store, "s1", [1000, 2000, 3000])
    _turns(store, "s1/agent-a", [500, 600], subagent=True)        # a subagent: its own conversation
    f = efficiency.facts(store, T0 - D, T0)
    c = f["context"]
    # s1: 1100 + 2100 re-sent; agent-a: 600 (min(600, 500 + 100))
    assert c["resent_tokens"] == 1100 + 2100 + 600
    assert c["resent_cached_share"] == 1.0 and c["resent_uncached_tokens"] == 0
    assert c["growth_tokens"] == (2000 - 1000) + (3000 - 1000) + (600 - 500)
    assert f["requests"] == 5 and f["by"]["model"][0]["name"] == OPUS


def test_caching_what_if_counts_only_re_sent_tokens_billed_as_plain_input(store):
    _turns(store, "s1", [10_000, 20_000], cached=False)
    f = efficiency.facts(store, T0 - D, T0)
    r = pricing.rate(OPUS)
    assert f["context"]["resent_uncached_tokens"] == 10_100
    assert abs(f["context"]["cache_saving_usd"] - 10_100 * (r.input - r.cache_read) / 1e6) < 1e-9
    w = efficiency.what_ifs(f)
    assert w[0]["kind"] == "what-if" and "assumption" in w[0]


def test_sources_with_per_session_totals_are_left_out_of_context_facts_and_said_so(store):
    store.add_usage([Usage("hermes", "h1", "h1:0", T0 - H, "x", input=5000, output=10, cost_usd=0.01)])
    f = efficiency.facts(store, T0 - D, T0)
    assert f["context"] is None or f["context"]["unthreaded_tokens"] == 5000
    _turns(store, "s1", [1000, 2000])
    text = efficiency.text(efficiency.report(store, 1, T0))
    assert "per-session totals have no context facts" in text


def test_other_models_are_ones_you_already_use_from_the_same_provider(store):
    _turns(store, "s1", [10_000, 20_000], subagent=True)
    store.add_usage([Usage("claude-code", "old", "o", T0 - 10 * D, SONNET, input=10, output=10, cost_usd=0.0001,
                           thread="old")])
    store.add_usage([Usage("codex", "c", "c", T0 - 10 * D, "gpt-6-luna", input=10, output=10, cost_usd=0.0,
                           thread="c", provider="openai")])
    swaps = efficiency.as_model(store, T0 - D, T0)
    assert [(s["model"], s["as"]) for s in swaps] == [(OPUS, SONNET)]      # not another provider's model
    s = swaps[0]
    assert s["usd_as"] < s["usd"] and s["subagent_share"] == 1.0 and "quality not compared" in s["assumption"]
    assert efficiency.as_model(store, T0 - D, T0, model="claude-haiku-4-5")[0]["as"] == "claude-haiku-4-5"


def test_the_command_labels_facts_and_what_ifs(store, capsys):
    from savetokens import cli
    import time
    rows = [Usage("claude-code", "s", f"r{i}", time.time() - 600 + i, OPUS, input=1000 * (i + 1), output=10,
                  thread="s", cost_usd=0.01) for i in range(3)]
    store.add_usage(rows)
    store.close()
    assert cli.main(["efficiency", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["ok"] and got["facts"]["requests"] == 3 and all(w["kind"] == "what-if" for w in got["what_ifs"])
    assert cli.main(["efficiency"]) == 0
    out = capsys.readouterr().out
    assert "Facts:" in out and "what-ifs" in out


# ── prices from data ────────────────────────────────────────────────────────

CATALOGUE = [
    {"id": "openai/gpt-x", "pricing": {"prompt": "0.000002", "completion": "0.00001", "input_cache_read": "0.0000002",
                                       "overrides": [{"min_prompt_tokens": 272000, "prompt": "0.000004",
                                                      "completion": "0.000015"}]}},
    {"id": "openai/gpt-x:free", "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "acme/brand-new-model", "pricing": {"prompt": "0.000001", "completion": "0.000003"}},
]


def test_the_catalogue_prices_any_provider_including_long_context(monkeypatch):
    monkeypatch.setattr(pricing, "_fetch", lambda: CATALOGUE)
    assert pricing.refresh(force=True) == 2                           # variants left out
    r = pricing.rate("gpt-x")
    assert (r.input, r.output, r.cache_read, r.source) == (2.0, 10.0, 0.2, "catalogue")
    assert pricing.cost("gpt-x", input=100_000) == 100_000 * 2.0 / 1e6
    assert pricing.cost("gpt-x", input=300_000) == 300_000 * 4.0 / 1e6     # past the long-context tier
    assert pricing.rate("brand-new-model").output == 3.0                   # a model this code has never heard of
    assert pricing.refresh() == 0                                          # a day hasn't passed


def test_unpriced_requests_get_a_price_once_the_catalogue_has_one(store, monkeypatch):
    store.add_usage([Usage("codex", "c", "c1", T0, "brand-new-model", input=1_000_000, thread="c", provider="acme"),
                     Usage("hermes", "h", "h1", T0, "brand-new-model", input=1_000_000)])   # included in a plan
    monkeypatch.setattr(pricing, "_fetch", lambda: CATALOGUE)
    pricing.refresh(force=True)
    assert pricing.reprice(store) == 1
    got = dict(store.conn.execute("SELECT harness, cost_usd FROM usage").fetchall())
    assert got == {"codex": 1.0, "hermes": None}


def test_a_v4_store_gains_the_new_columns_and_reads_its_sources_again(homes):
    from savetokens.store import home
    path = home() / "savetokens.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.executescript("CREATE TABLE usage (machine TEXT NOT NULL, harness TEXT NOT NULL, session_id TEXT NOT NULL,"
                    " request_id TEXT NOT NULL, ts REAL NOT NULL, model TEXT, subagent INTEGER NOT NULL DEFAULT 0,"
                    " input INTEGER NOT NULL DEFAULT 0, cache_read INTEGER NOT NULL DEFAULT 0, cache_write_5m INTEGER"
                    " NOT NULL DEFAULT 0, cache_write_1h INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0,"
                    " cost_usd REAL, account TEXT, project TEXT, billing TEXT, provider TEXT,"
                    " UNIQUE (machine, harness, request_id));"
                    "CREATE TABLE offsets (path TEXT PRIMARY KEY, offset INTEGER NOT NULL, size INTEGER NOT NULL);"
                    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
                    "INSERT INTO offsets VALUES ('/x.jsonl', 10, 10);"
                    "INSERT INTO meta VALUES ('codex_file:a', '{}'), ('hermes_db:h', '{}');"
                    "INSERT INTO usage (machine, harness, session_id, request_id, ts) VALUES ('m', 'claude-code', 's', 'r', 1);"
                    "PRAGMA user_version=4;")
    c.commit()
    c.close()
    with Store() as s:
        cols = {r[1] for r in s.conn.execute("PRAGMA table_info(usage)")}
        assert {"reasoning", "thread", "cwd"} <= cols
        assert s.conn.execute("SELECT COUNT(*) FROM offsets").fetchone()[0] == 0
        keys = {r[0] for r in s.conn.execute("SELECT key FROM meta")}
        assert "codex_file:a" not in keys and "hermes_db:h" in keys          # Hermes isn't read twice
        s.add_usage([Usage("claude-code", "s", "r", 1, thread="s", cwd="/w", reasoning=3, machine="m")])
        assert tuple(s.conn.execute("SELECT thread, cwd, reasoning FROM usage").fetchone()) == ("s", "/w", 3)
