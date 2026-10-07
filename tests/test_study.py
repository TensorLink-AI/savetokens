"""Study skeletons carry structure, never content; the heavy follow-up rule it led to."""
from __future__ import annotations

import json

from conftest import T0
from savetokens import study, steer
from savetokens.adapters import claude_code as cc


def prompt(t, ts, text):
    d = t._base(ts, "user")
    d["message"] = {"role": "user", "content": text}
    t._write(d)


def test_skeleton_is_structure_only(transcript):
    t = transcript(session="sess-secret", project="secret-project")
    prompt(t, T0, "please refactor the billing module with key sk-123")
    t.turn(T0 + 10, read=400_000, tools=[("tu1", "Read", {"file_path": "/work/secret-project/billing.py"})])
    t.result(T0 + 11, "tu1", chars=20_000)
    t.turn(T0 + 20, read=401_000, tools=[("tu2", "Read", {"file_path": "/work/secret-project/billing.py"})])
    t.result(T0 + 21, "tu2", ok=False)
    prompt(t, T0 + 4000, "ok")
    t.turn(T0 + 4010, read=402_000)
    sk = study.skeleton(t.path)
    assert len(sk["segments"]) == 2
    first, second = sk["segments"]
    assert first["prompt_chars"] == len("please refactor the billing module with key sk-123")
    assert first["turns"] == 2 and first["big"] == 1 and first["failed"]["read"] == 1
    assert second["idle_before_min"] == 66
    text = study.render(sk)
    for secret in ("billing", "sk-123", "secret-project", "sess-secret", "refactor"):
        assert secret not in text
    assert "1 re-reads of 1 files" in text and "idle before 66m" in text


def test_study_tags_new_findings(store, transcript, monkeypatch):
    t = transcript(session="s1")
    prompt(t, T0, "go")
    t.turn(T0 + 5)
    cc.backfill(store)
    store.conn.execute("UPDATE usage SET ts = ?", (__import__("time").time() - 3600,))
    store.conn.commit()
    findings = [{"pattern": "a", "category": "context_carry"}, {"pattern": "b", "category": "new"}]

    class R:
        stdout = json.dumps({"result": "here:\n" + json.dumps(findings), "total_cost_usd": 0.01})
        stderr = ""
    r = study.run(store, runner=lambda *a, **k: R())
    assert r["sessions"] == 1 and [f["pattern"] for f in r["new"]] == ["b"]
    dry = study.run(store, dry_run=True)
    assert "session " in dry["prompt"] and "Known waste categories" in dry["prompt"]


def test_heavy_followup_warns_once_with_the_cost(store, transcript):
    t = transcript(session="s1")
    t.turn(T0, read=600_000, write5=0, write1h=1_000)
    cc.backfill(store)
    assert steer.heavy_followup(store, "s1", "x" * 500, now=T0 + 60) is None     # a real brief, not a follow-up
    msg = steer.heavy_followup(store, "s1", "and now?", now=T0 + 60)
    assert "re-read 601k tokens" in msg and "expired" not in msg and "$0.12" in msg   # warm: cache-read price
    assert steer.heavy_followup(store, "s1", "and now?", now=T0 + 120) is None    # once per step
    cold = steer.heavy_followup(store, "s1", "back", now=T0 + 7200)
    assert "rebuild the expired cache" in cold and "$4.81" in cold                   # 1h cache write: 2x input


def test_heavy_followup_can_stop_the_prompt_when_asked(store, transcript):
    from savetokens.store import load_config, save_config
    cfg = load_config()
    cfg["heavy_followup"] = "block"
    save_config(cfg)
    t = transcript(session="s1")
    t.turn(T0, read=900_000)
    out = cc.handle_hook("UserPromptSubmit", {"session_id": "s1", "prompt": "hi", "transcript_path": str(t.path)},
                         store)
    assert out["decision"] == "block" and "Send it again" in out["reason"]
    again = cc.handle_hook("UserPromptSubmit", {"session_id": "s1", "prompt": "hi", "transcript_path": str(t.path)},
                           store)
    assert again is None or "decision" not in again
