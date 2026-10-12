"""Tasks: declared, tracker keys and branches; stopped versus done; measured estimates; task budgets."""
from __future__ import annotations

import io
import json

import pytest

from savetokens import capture, cli, forecast, mcp, pools, projects, tasks
from savetokens.store import Usage

from conftest import H, T0


def _repo(path, default="main"):
    git = path / ".git"
    (git / "refs" / "remotes" / "origin").mkdir(parents=True)
    (git / "config").write_text('[remote "origin"]\n\turl = git@github.com:org/webapp.git\n')
    (git / "refs" / "remotes" / "origin" / "HEAD").write_text(f"ref: refs/remotes/origin/{default}\n")
    (git / "HEAD").write_text("ref: refs/heads/feature/ENG-142-oauth\n")
    return path


def _use(store, cwd, branch, at, usd=1.0, n=1, sid="s"):
    store.add_usage([Usage("claude-code", sid, f"{sid}{branch}{at}{i}", at + i, "m", cost_usd=usd / n, output=100,
                           cwd=cwd, branch=branch, thread=sid) for i in range(n)])


def _task_of(store, branch):
    r = store.conn.execute("SELECT t.name FROM usage u LEFT JOIN tasks t ON t.id = u.task_id WHERE u.branch = ?",
                           (branch,)).fetchone()
    return r[0] if r else None


def test_a_branch_is_a_task_but_not_the_default_one(store, tmp_path):
    repo = str(_repo(tmp_path / "webapp", default="trunk"))
    _use(store, repo, "feature/x", T0)
    _use(store, repo, "trunk", T0)
    _use(store, repo, "HEAD", T0)                 # detached: no branch
    _use(store, repo, "main", T0)                 # not the default here (origin's HEAD is trunk)
    assert _task_of(store, "feature/x") == "feature/x"
    assert _task_of(store, "trunk") is None and _task_of(store, "HEAD") is None
    assert _task_of(store, "main") == "main"


def test_a_tracker_key_in_the_branch_name_is_the_task(store, tmp_path):
    repo = str(_repo(tmp_path / "webapp"))
    store.set_meta("task_settings", {"branches": True, "pattern": r"[A-Z]+-\d+"})
    _use(store, repo, "feature/ENG-142-oauth", T0)
    _use(store, repo, "fix/ENG-142-followup", T0 + 60)
    assert _task_of(store, "feature/ENG-142-oauth") == _task_of(store, "fix/ENG-142-followup") == "ENG-142"


def test_a_declared_task_wins_until_another_starts_and_only_done_finishes_it(store, tmp_path):
    repo = str(_repo(tmp_path / "webapp"))
    a = tasks.start(store, repo, "Add OAuth", now=T0)
    _use(store, repo, "feature/x", T0 + 60)
    assert _task_of(store, "feature/x") == "Add OAuth"
    tasks.start(store, repo, "Fix login", now=T0 + 3600)            # stops Add OAuth; doesn't finish it
    _use(store, repo, "feature/y", T0 + 3700)
    assert _task_of(store, "feature/y") == "Fix login"
    assert tasks.totals(store, a["id"])["done"] is None
    got = tasks.done(store, repo, now=T0 + 7200)                     # the current one
    assert got["name"] == "Fix login" and got["usd"] == pytest.approx(1.0)


def test_estimates_are_measured_from_finished_tasks(store, tmp_path):
    repo = str(_repo(tmp_path / "webapp"))
    pid = projects.resolve(store, repo)[0]
    assert not tasks.estimate(store, pid)["enough"]
    for k, usd in enumerate([2.0, 4.0, 6.0, 30.0]):
        tasks.start(store, repo, f"t{k}", now=T0 + k * 10 * H)
        _use(store, repo, "x", T0 + k * 10 * H + 60, usd=usd, sid=f"s{k}")
        tasks.done(store, repo, now=T0 + k * 10 * H + 3600)
    e = tasks.estimate(store, pid)
    assert e["enough"] and e["n"] == 4 and e["median_usd"] == pytest.approx(5.0) and e["kind"] == "measured"
    assert "from 4 tasks" in e["text"]


def test_a_task_budget_counts_only_the_task(store, tmp_path):
    repo = str(_repo(tmp_path / "webapp"))
    t = tasks.start(store, repo, "Add OAuth", now=T0 - 10 * H)
    _use(store, repo, "feature/x", T0 - 5 * H, usd=12.0)
    tasks.done(store, repo, now=T0 - 4 * H)
    _use(store, repo, "feature/x", T0 - 3 * H, usd=50.0)             # the branch's own task, not Add OAuth
    pools.set_goal(store, "oauth", {"label": "Add OAuth", "scope": {"tasks": [t["id"]]}, "unit": "usd",
                                    "amount": 20, "by": T0 + 86400, "start": T0 - 86400, "tz": 0})
    o = next(o for o in forecast.outlook(store, T0, goals=True) if o["kind"] == "goal")
    assert o["spent"] == pytest.approx(12.0)


def test_taking_the_hubs_key_keeps_tasks_and_their_done_marks(store, tmp_path):
    repo = str(_repo(tmp_path / "webapp"))
    tasks.start(store, repo, "Add OAuth", now=T0)
    _use(store, repo, "feature/x", T0 + 60, usd=3.0)
    tasks.done(store, repo, now=T0 + 120)
    projects.set_key(store, "k" * 32)
    pid = projects.resolve(store, repo)[0]
    t = [x for x in tasks.listing(store, [pid]) if x["name"] == "Add OAuth"][0]
    assert t["id"] == tasks.task_id(store, pid, "Add OAuth") and t["done"] and t["usd"] == pytest.approx(3.0)


def test_claude_code_transcripts_give_the_branch(tmp_path):
    e, _ = capture.parse_line({"type": "assistant", "sessionId": "s", "timestamp": "2026-10-12T00:00:00Z",
                               "gitBranch": "feature/x", "cwd": "/w", "requestId": "r",
                               "message": {"id": "m", "model": "claude-opus-5-5", "usage": {"input_tokens": 1}}})
    assert e.branch == "feature/x" and e.cwd == "/w"


def test_task_commands_and_agent_tools(store, tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path / "webapp")
    store.close()
    monkeypatch.chdir(repo)
    assert cli.main(["task", "start", "Add OAuth"]) == 0
    assert "not enough data yet" in capsys.readouterr().out
    assert cli.main(["budget", "--task", "Add OAuth", "--tokens", "3M"]) == 0
    assert cli.main(["budget", "--task", "nope", "--usd", "1", "--json"]) == 1
    capsys.readouterr()
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "task_status", "arguments": {}}},
             {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "task_done", "arguments": {}}},
             {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "task_start", "arguments": {"name": "Fix login"}}}]
    out = io.StringIO()
    mcp.serve(io.StringIO("\n".join(json.dumps(m) for m in lines) + "\n"), out)
    got = {r["id"]: r["result"]["content"][0]["text"] for r in map(json.loads, out.getvalue().splitlines())
           if r.get("id") in (2, 3, 4)}
    assert got[2].startswith("Add OAuth (declared)") and "Budget:" in got[2]
    assert got[3].startswith("Done: Add OAuth") and got[4].startswith("Started 'Fix login'")
    assert cli.main(["tasks", "--project", ".", "--json"]) == 0
    names = {t["name"]: t for t in json.loads(capsys.readouterr().out)["tasks"]}
    assert names["Add OAuth"]["done"] and not names["Fix login"]["done"]
    assert cli.main(["task", "pattern", "[A-Z]+-[0-9]+"]) == 0
    assert cli.main(["task", "--json"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["task"]["name"] == "Fix login"   # declared wins
