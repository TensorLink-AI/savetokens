"""Measured effects: comparisons across finished tasks, and before against after each suggestion."""
from __future__ import annotations

import pytest

from savetokens import advise, effects, efficiency, tasks
from savetokens.store import Usage

from conftest import H, T0

D = 86400


def _repo(path):
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "config").write_text('[remote "origin"]\n\turl = git@github.com:org/webapp.git\n')
    return str(path)


def _task(store, repo, name, at, usd, fresh, model="claude-opus-5-5", n=4):
    sid = f"s-{name}"
    if not fresh:   # the session was already going before the task started
        store.add_usage([Usage("claude-code", sid, f"{sid}-pre", at - H, model, cost_usd=0.1, cwd="/elsewhere")])
    tasks.start(store, repo, name, now=at)
    store.add_usage([Usage("claude-code", sid, f"{sid}-{i}", at + 60 + i, model, cost_usd=usd / n, output=10,
                           cwd=repo, thread=sid) for i in range(n)])
    tasks.done(store, repo, now=at + H)


def test_tasks_started_fresh_against_continued_are_compared_only_with_enough_on_each_side(store, tmp_path):
    repo = _repo(tmp_path / "webapp")
    for k, usd in enumerate([2.0, 3.0]):
        _task(store, repo, f"f{k}", T0 + k * 3 * H, usd, fresh=True)
    got = {m["id"]: m for m in effects.compare(store)}
    assert not got["fresh"]["enough"] and "not enough data yet" in got["fresh"]["text"]
    for k, usd in enumerate([4.0]):
        _task(store, repo, f"f{k + 2}", T0 + (k + 2) * 3 * H, usd, fresh=True)
    for k, usd in enumerate([6.0, 8.0, 10.0]):
        _task(store, repo, f"c{k}", T0 + (k + 5) * 3 * H, usd, fresh=False)
    m = {m["id"]: m for m in effects.compare(store)}["fresh"]
    assert m["enough"] and m["n_a"] == 3 and m["n_b"] == 3 and m["kind"] == "measured"
    assert m["side_a"]["median_usd"] == pytest.approx(3.0) and m["side_b"]["median_usd"] == pytest.approx(8.0)
    assert "62% less" in m["text"] and "cost only, not quality" in m["text"]


def test_tasks_on_two_models_are_compared(store, tmp_path):
    repo = _repo(tmp_path / "webapp")
    for k in range(3):
        _task(store, repo, f"o{k}", T0 + k * 3 * H, 10.0, fresh=True, model="claude-opus-5-5")
        _task(store, repo, f"s{k}", T0 + (k + 4) * 3 * H, 4.0, fresh=True, model="claude-sonnet-5-5")
    m = next(m for m in effects.compare(store) if m["id"].startswith("model:"))
    assert m["enough"] and "mainly on claude-opus-5-5" in m["text"]


def test_a_suggestion_is_followed_up_a_week_later_before_against_after(store):
    def subagents(start, days, model):
        store.add_usage([Usage("claude-code", "s", f"{model}{start}{i}", start + i * 3600, model, subagent=True,
                               cost_usd=1.0, thread=f"s/{i}") for i in range(days * 24)])
    subagents(T0 - 14 * D, 14, "claude-opus-5-5")
    effects.record_shown(store, [{"id": "subagents", "kind": "what-if", "title": "Run subagents on claude-sonnet-5-5"}],
                         now=T0)
    assert effects.followups(store, T0 + 3 * D) == []                  # not settled yet
    subagents(T0, 8, "claude-sonnet-5-5")
    f = effects.followups(store, T0 + 8 * D)[0]
    assert f["kind"] == "measured" and f["metric_before"]["value"] == 0 and f["metric_after"]["value"] == 1.0
    assert "share of subagent $ on claude-sonnet-5-5 0% → 100%" in f["text"] and "other things changed too" in f["text"]
    effects.record_shown(store, [{"id": "subagents", "kind": "what-if", "title": "again"}], now=T0 + 9 * D)
    assert store.meta("suggested")["subagents"]["shown"] == T0                    # the first time it was shown


def test_measured_numbers_reach_efficiency_and_the_brief(store, tmp_path):
    repo = _repo(tmp_path / "webapp")
    _task(store, repo, "f0", T0, 2.0, fresh=True)
    r = efficiency.report(store, 7, T0 + D)
    assert any(m["kind"] == "measured" for m in r["measured"])
    assert "Measured (your own history):" in efficiency.text(r)
    b = advise.brief(store, T0 + D, cwd=repo)
    assert "measured" in b
