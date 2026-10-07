from savetokens import pricing
from savetokens.adapters import claude_code as cc

from conftest import T0


def test_price_normalisation_and_cache_rates():
    assert pricing.normalize("anthropic/claude-opus-5-5[1m]") == "claude-opus-5-5"
    assert pricing.normalize("claude-sonnet-4.6") == "claude-sonnet-4-6"
    # 1M input + 1M cache read + 1M 5m write + 1M 1h write + 1M output on Opus 5.5
    usd = pricing.cost("claude-opus-5-5", input=1e6, cache_read=1e6, cache_write_5m=1e6, cache_write_1h=1e6,
                       output=1e6)
    assert usd == 4 + 0.2 + 5 + 8 + 20
    assert pricing.cost("claude-opus-5-5", output=1e6, fast=True) == 40
    assert pricing.cost("gpt-5", input=1e6) is None
    # longest prefix wins: opus-5-5 is not priced as opus-5
    assert pricing.rates("claude-opus-5-5-preview") == pricing.PRICES["claude-opus-5-5"]


def test_backfill_dedupes_split_messages(store, transcript):
    t = transcript()
    t.turn(T0, blocks=3, tools=[("tu1", "Read", {"file_path": "/a.py"})])
    t.result(T0 + 1, "tu1", chars=1234)
    t.turn(T0 + 10)
    assert cc.backfill(store) == 2
    events = store.usage()
    assert len(events) == 2
    assert events[0].cache_write_5m == 1000 and events[0].project == "proj"
    tools = store.tools()
    assert [(x.tool, x.kind, x.target, x.ok, x.output_chars) for x in tools] == [("Read", "read", "/a.py", True, 1234)]


def test_incremental_ingest_reads_only_new_complete_lines(store, transcript):
    t = transcript()
    t.turn(T0)
    assert cc.backfill(store) == 1
    assert cc.backfill(store) == 0
    with open(t.path, "a") as f:
        f.write('{"type": "assistant", "partial')   # half-written line
    t2_before = len(store.usage())
    assert cc.backfill(store) == 0 and len(store.usage()) == t2_before
    with open(t.path, "a") as f:
        f.write('"}\n')
    t.turn(T0 + 5)
    assert cc.backfill(store) == 1


def test_subagent_transcripts_are_marked_and_tracked_per_agent(store, homes):
    from conftest import Transcript
    path = homes / "claude" / "projects" / "-work-proj" / "s1" / "subagents" / "agent-a1.jsonl"
    t = Transcript(path, session="s1", subagent=True, agent_id="a1")
    t.turn(T0, tools=[("tu9", "Grep", {"pattern": "x"})])
    cc.backfill(store)
    assert store.usage()[0].subagent is True
    assert store.tools()[0].session_id == "s1/a1"


def test_statusline_records_limits_and_composes_with_existing(store, transcript, monkeypatch):
    from savetokens.store import load_config, save_config
    cfg = load_config()
    cfg["statusline_wrapped"] = "echo theirs"
    save_config(cfg)
    payload = {"session_id": "s1", "rate_limits": {"five_hour": {"used_percentage": 40, "resets_at": T0 + 9999}},
               "context_window": {"used_percentage": 12}}
    out = cc.statusline(payload, "{}", store)
    assert out.startswith("theirs")
    row = store.latest_limits()
    assert row["five_hour_pct"] == 40 and row["context_pct"] == 12
