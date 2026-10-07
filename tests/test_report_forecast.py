from savetokens import report
from savetokens.adapters import claude_code as cc
from savetokens.store import ToolEvent, UsageEvent

from conftest import T0


def rules(r):
    return {f.rule: f for f in r["findings"]}


def test_each_waste_rule_fires_on_its_seeded_fixture(store, transcript):
    t = transcript()
    ts = T0
    # long context carried for many turns
    for i in range(10):
        t.turn(ts, read=300_000)
        ts += 30
    # idle 20 minutes, then a cache rebuild
    ts += 1200
    t.turn(ts, read=0, write5=200_000)
    # a 60k-character tool output carried for 5 turns
    t.turn(ts + 5, tools=[("big", "Bash", {"command": "cat log"})])
    t.result(ts + 6, "big", chars=60_000)
    for i in range(5):
        t.turn(ts + 10 + i)
    # the same unchanged file read three times
    for i in range(3):
        t.turn(ts + 20 + i, tools=[(f"rd{i}", "Read", {"file_path": "/work/proj/a.py"})])
        t.result(ts + 20.5 + i, f"rd{i}", chars=8000)
    # an Opus subagent
    from conftest import Transcript
    sub = Transcript(t.path.with_suffix("") / "subagents" / "agent-x.jsonl", session="s1", subagent=True,
                     agent_id="x")
    sub.turn(ts + 30, read=40_000, output=4000)
    cc.backfill(store)
    r = report.build(store, days=7, now=ts + 60)
    found = rules(r)
    assert {"context_carry", "cache_rebuild", "big_output", "reread", "subagent_model"} <= set(found)
    assert found["cache_rebuild"].usd > 0 and found["subagent_model"].fix == "subagent-model"
    text = report.render(r)
    assert "Long contexts re-read every turn" in text and "savetokens fixes apply" in text
    assert r["totals"]["usd"] > 0


def test_clean_session_has_no_findings(store, transcript):
    t = transcript()
    for i in range(5):
        t.turn(T0 + i * 10, read=20_000, write5=500)
    cc.backfill(store)
    assert report.build(store, now=T0 + 100)["findings"] == []
