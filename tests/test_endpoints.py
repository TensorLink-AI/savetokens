"""The tool and who serves it are two things: Codex on another provider, Claude Code on a compatible endpoint."""
from __future__ import annotations

from savetokens import capture, codex, hooks, spend, sync
from savetokens.store import Store, Usage

from conftest import H, T0
from test_pools import rollout


def _rows(store, harness):
    return [tuple(r) for r in store.conn.execute("SELECT provider, billing, COUNT(*) FROM usage WHERE harness = ?"
                              " GROUP BY 1, 2 ORDER BY 1", (harness,)).fetchall()]


def test_codex_names_the_provider_each_session_ran_on(homes):
    (homes / "codex").mkdir(exist_ok=True)
    (homes / "codex" / "config.toml").write_text(
        '[model_providers.proxy]\nname = "Proxy"\nbase_url = "https://api.engy.ai/v1"\n')
    assert codex.provider_of("openai") is None and codex.provider_of(None) is None   # Codex's own
    assert codex.provider_of("proxy") == "engy.ai"                                    # its endpoint's host
    assert codex.provider_of("benchmark") == "benchmark"                              # not in config: its id
    assert codex.provider_of("ollama") == "local"


def test_a_codex_session_on_another_provider_is_api_there_not_the_plan(store, homes):
    plan = rollout(homes, "rollout-a.jsonl", sid="a", model_provider="openai")
    plan.turn(T0 - 2 * H, week=10.0)
    other = rollout(homes, "rollout-b.jsonl", sid="b", model_provider="benchmark")
    other.turn(T0 - H)
    codex.backfill(store)
    got = {(p, b): n for p, b, n in _rows(store, "codex")}
    assert got == {("openai", None): 1, ("benchmark", "api"): 1}
    assert spend.label("benchmark") == "benchmark"


def test_requests_read_before_are_put_right_once_and_sent_again(store, homes):
    r = rollout(homes, "rollout-b.jsonl", sid="b", model_provider="benchmark")
    r.turn(T0 - H)
    store.add_usage([Usage("codex", "b", "old", T0 - 2 * H, model="gpt-6-astra", input=5)])   # read as OpenAI's
    store.set_meta("pushed:usage", 10**6)
    store.set_meta("codex_file:rollout-b.jsonl", {"sid": "b"})                                # read before
    store.set_offset(r.path, r.path.stat().st_size, r.path.stat().st_size)
    assert codex.fill_providers(store) == 1
    assert _rows(store, "codex") == [("benchmark", "api", 1)]
    assert store.meta("pushed:usage") < 10**6                       # the hub gets the change
    assert codex.fill_providers(store) == 0                         # once


def test_claude_codes_endpoint_comes_from_its_environment():
    assert capture.endpoint({}) is None
    assert capture.endpoint({"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}) is None
    assert capture.endpoint({"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"}) == "z.ai"
    assert capture.endpoint({"ANTHROPIC_BASE_URL": "http://localhost:4000"}) == "local"
    assert capture.endpoint({"CLAUDE_CODE_USE_BEDROCK": "1"}) == "bedrock"
    assert capture.endpoint({"CLAUDE_CODE_USE_VERTEX": "true"}) == "vertex"


def test_a_claude_code_session_on_a_compatible_endpoint_is_not_the_plan(store, transcript, monkeypatch):
    t = transcript("s1")
    t.turn(T0 - 2 * H)
    capture.ingest_file(store, t.path)                              # read before any hook fired
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.z.ai/api/anthropic")
    hooks.handle("Stop", {"session_id": "s1", "transcript_path": str(t.path)}, store)
    t.turn(T0 - H)
    capture.ingest_file(store, t.path)
    assert _rows(store, "claude-code") == [("z.ai", "api", 2)]
    monkeypatch.delenv("ANTHROPIC_BASE_URL")
    t2 = transcript("s2")
    t2.n = 10                                                       # its own request ids
    t2.turn(T0 - H)
    hooks.handle("Stop", {"session_id": "s2", "transcript_path": str(t2.path)}, store)
    capture.ingest_file(store, t2.path)
    assert ("anthropic", None, 1) in _rows(store, "claude-code")   # the plan's own session: unchanged


def test_the_hub_takes_a_machines_correction(running, tmp_path):
    users, token, url = running
    a = Store(tmp_path / "a.db")
    a.add_usage([Usage("codex", "b", "r1", T0 - H, model="gpt-6-astra", input=5)])
    cfg = {"server_url": url, "server_token": token}
    sync.push(a, cfg)
    a.relabel("codex", "b", "benchmark")
    assert sync.push(a, cfg) == 1
    with users.store("chris") as s:
        assert _rows(s, "codex") == [("benchmark", "api", 1)]
