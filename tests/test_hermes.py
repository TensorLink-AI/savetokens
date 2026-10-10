"""Hermes Agent: pay-as-you-go usage from its session store, its API budget, and install into its config."""
from __future__ import annotations

import sqlite3

from savetokens import forecast, hermes, install, maintain, mcp, pools

from conftest import H, T0


class HermesDB:
    """Hermes style state.db: running totals per session and model."""

    def __init__(self, path, legacy=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn, self.legacy = sqlite3.connect(path), legacy
        self.conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, model TEXT, parent_session_id TEXT,"
                          " started_at REAL, ended_at REAL, input_tokens INT DEFAULT 0, output_tokens INT DEFAULT 0,"
                          " cache_read_tokens INT DEFAULT 0, cache_write_tokens INT DEFAULT 0,"
                          " reasoning_tokens INT DEFAULT 0, cwd TEXT, billing_provider TEXT, billing_base_url TEXT,"
                          " billing_mode TEXT, estimated_cost_usd REAL, actual_cost_usd REAL)")
        if not legacy:
            self.conn.execute("CREATE TABLE session_model_usage (session_id TEXT, model TEXT, billing_provider TEXT"
                              " DEFAULT '', billing_base_url TEXT DEFAULT '', billing_mode TEXT DEFAULT '', task TEXT"
                              " DEFAULT '', api_call_count INT DEFAULT 0, input_tokens INT DEFAULT 0, output_tokens"
                              " INT DEFAULT 0, cache_read_tokens INT DEFAULT 0, cache_write_tokens INT DEFAULT 0,"
                              " reasoning_tokens INT DEFAULT 0, estimated_cost_usd REAL DEFAULT 0, actual_cost_usd"
                              " REAL DEFAULT 0, cost_status TEXT, cost_source TEXT, first_seen REAL, last_seen REAL,"
                              " PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode,"
                              " task))")

    def session(self, sid, start, cwd="/work/bot", parent=None):
        self.conn.execute("INSERT INTO sessions (id, source, started_at, cwd, parent_session_id) VALUES (?,?,?,?,?)",
                          (sid, "cli", start, cwd, parent))
        self.conn.commit()

    def use(self, sid, ts, model="anthropic/claude-sonnet-5-5", tokens=0, out=0, usd=0.0, mode="official_models_api",
            task=""):
        """Hermes adding to a session's running totals."""
        if self.legacy:
            self.conn.execute("UPDATE sessions SET model = ?, billing_mode = ?, input_tokens = input_tokens + ?,"
                              " output_tokens = output_tokens + ?, estimated_cost_usd = COALESCE(estimated_cost_usd,"
                              " 0) + ?, ended_at = ? WHERE id = ?", (model, mode, tokens, out, usd, ts, sid))
        else:
            self.conn.execute(
                "INSERT INTO session_model_usage (session_id, model, billing_provider, billing_mode, task,"
                " input_tokens, output_tokens, estimated_cost_usd, first_seen, last_seen)"
                " VALUES (?,?,'openrouter',?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET"
                " input_tokens = input_tokens + excluded.input_tokens,"
                " output_tokens = output_tokens + excluded.output_tokens,"
                " estimated_cost_usd = estimated_cost_usd + excluded.estimated_cost_usd, last_seen = excluded.last_seen",
                (sid, model, mode, task, tokens, out, usd, ts, ts))
        self.conn.commit()


def db(homes, **kw):
    return HermesDB(homes / "hermes" / "state.db", **kw)


def _usage(store):
    return store.conn.execute("SELECT COALESCE(SUM(input), 0), COALESCE(SUM(output), 0), COALESCE(SUM(cost_usd), 0),"
                              " COUNT(*) FROM usage WHERE harness = 'hermes'").fetchone()


def test_hermes_usage_is_what_its_totals_grew_by(store, homes):
    h = db(homes)
    h.session("s1", T0 - 3 * H)
    h.use("s1", T0 - 3 * H + 60, tokens=1000, out=100, usd=0.5)
    assert hermes.backfill(store) == 1
    row = store.conn.execute("SELECT session_id, model, billing, project, subagent, cost_usd FROM usage").fetchone()
    assert tuple(row) == ("s1", "anthropic/claude-sonnet-5-5", "api", "bot", 0, 0.5)   # Hermes's own cost
    assert hermes.backfill(store) == 0                                   # nothing new
    h.use("s1", T0 - H, tokens=500, out=50, usd=0.25)
    h.use("s1", T0 - H + 5, model="openai/gpt-6-astra", tokens=200, usd=0.1, task="compression")
    assert hermes.backfill(store) == 2
    assert tuple(_usage(store)) == (1700, 150, 0.85, 3)
    assert store.conn.execute("SELECT subagent FROM usage WHERE model LIKE 'openai/%'").fetchone()[0] == 1


def test_a_long_session_first_seen_is_spread_over_its_hours(store, homes):
    h = db(homes)
    h.session("s1", T0 - 10 * H)
    h.use("s1", T0 - 10 * H, tokens=1000)
    h.use("s1", T0, tokens=1001, usd=10.0)
    assert hermes.backfill(store) == 11
    assert tuple(_usage(store))[:3] == (2001, 0, 10.0)
    ts = [r[0] for r in store.conn.execute("SELECT ts FROM usage ORDER BY ts")]
    assert ts[0] > T0 - 10 * H and ts[-1] < T0


def test_without_a_cost_from_hermes_the_price_list_is_used(store, homes):
    from savetokens import pricing
    h = db(homes)
    h.session("s1", T0 - H)
    h.use("s1", T0 - H, tokens=1_000_000)
    hermes.backfill(store)
    assert abs(_usage(store)[2] - pricing.cost("claude-sonnet-5-5", input=1_000_000)) < 1e-9


def test_usage_included_in_a_plan_counts_against_no_budget(store, homes):
    h = db(homes)
    h.session("s1", T0 - H)
    h.use("s1", T0 - H, model="gpt-6-astra", tokens=1000, mode=hermes.INCLUDED)
    hermes.backfill(store)
    assert tuple(store.conn.execute("SELECT billing, cost_usd FROM usage").fetchone()) == (None, None)


def test_older_hermes_and_profiles_are_read(store, homes):
    h = db(homes, legacy=True)
    h.session("s1", T0 - H)
    h.use("s1", T0 - H, tokens=100, usd=0.01)
    p = HermesDB(homes / "hermes" / "profiles" / "work" / "state.db")
    p.session("w1", T0 - H)
    p.use("w1", T0 - H, tokens=300, usd=0.03)
    assert hermes.backfill(store) == 2
    assert tuple(_usage(store))[:3] == (400, 0, 0.04)


def test_a_hermes_budget_is_its_own_pool(store, homes):
    h = db(homes)
    for i in range(48):
        h.session(f"s{i}", T0 - (48 - i) * H)
        h.use(f"s{i}", T0 - (48 - i) * H + 60, tokens=10_000, usd=1.0)
    hermes.backfill(store)
    maintain.notices(store, T0, {})
    assert "savetokens api hermes" in store.conn.execute("SELECT message FROM alerts").fetchone()[0]
    pools.set_budget(store, "hermes", 100, "week", tz=0)
    assert [p.id for p in pools.pools(store, T0)] == ["hermes:api"]
    maintain.update(store, T0, use_ephemeris=False)
    o = forecast.outlook(store, T0)[0]
    assert o["label"] == "Hermes API budget" and o["spent_usd"] > 0 and o["p50"] is not None


def test_mcp_knows_a_hermes_client():
    assert mcp._harness({"name": "hermes-agent"}) == "hermes"


def test_install_adds_the_mcp_server_to_hermes_and_uninstall_removes_it(homes):
    cfg = homes / "hermes" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    before = "model:\n  default: anthropic/claude-sonnet-5-5\nmcp_servers:\n    github:\n        command: npx\n"
    cfg.write_text(before)
    assert install.install(yes=True, no_ephemeris=True, cron=False, out=lambda *_: None, mcp=True)
    text = cfg.read_text()
    assert "    savetokens:\n        command:" in text and text.count("mcp_servers:") == 1
    assert '"mcp"' in text and "github:" in text
    assert install.hermes_skill_path().read_text().startswith("---\nname: savetokens")
    install.install(yes=True, no_ephemeris=True, cron=False, out=lambda *_: None)
    assert cfg.read_text().count("savetokens:") == 1
    install.uninstall(out=lambda *_: None)
    assert cfg.read_text() == before and not install.hermes_skill_path().exists()


def test_hermes_config_without_servers_or_rewritten_by_hermes(homes):
    cfg = homes / "hermes" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("model:\n  default: x\n")
    assert install.add_hermes_mcp("savetokens")
    assert "\nmcp_servers:\n  savetokens:\n    command: \"savetokens\"\n    args: [\"mcp\"]\n" in cfg.read_text()
    # Hermes saves its config without comments: the server is still found and removed
    cfg.write_text("mcp_servers:\n  savetokens:\n    command: savetokens\n    args:\n    - mcp\nmodel:\n  default: x\n")
    install.remove_hermes_mcp()
    assert cfg.read_text() == "mcp_servers:\nmodel:\n  default: x\n"
    cfg.write_text("mcp_servers: {github: {command: npx}}\n")
    assert not install.add_hermes_mcp("savetokens")                       # inline map: left alone
    assert cfg.read_text() == "mcp_servers: {github: {command: npx}}\n"


def test_the_server_adds_up_hermes_and_the_other_tools_across_machines(running, tmp_path, monkeypatch):
    from savetokens import sync
    from savetokens.store import Store, Usage
    users, token, url = running
    cfg = {"server_url": url, "server_token": token}
    laptop, ci = Store(tmp_path / "a.db"), Store(tmp_path / "b.db")
    pools.set_budget(laptop, "hermes", 100, "week", tz=0)
    for s, harness in ((laptop, "hermes"), (ci, "hermes"), (ci, "claude-code")):
        s.add_usage([Usage(harness, "k", f"{s.machine}{harness}{i}", T0 - (24 - i) * H, "m", cost_usd=1.0,
                           billing="api") for i in range(24)])
        sync.push(s, cfg)
    with users.store("chris") as s:
        assert [tuple(r) for r in s.conn.execute("SELECT harness, COUNT(DISTINCT machine), SUM(cost_usd) FROM usage"
                                                 " GROUP BY harness ORDER BY harness")] == [("claude-code", 1, 24.0), ("hermes", 2, 48.0)]
        maintain.update(s, T0, use_ephemeris=False)
        o = {x["pool"]: x for x in forecast.outlook(s, T0)}
        start, _ = pools.period(pools.budgets(s)["hermes"], T0)
        both = 2 * sum(1 for i in range(24) if T0 - (24 - i) * H >= start)
        assert o["hermes:api"]["spent_usd"] == both                   # both machines' Hermes spend, one budget
