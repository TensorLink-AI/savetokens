"""Projects across machines, and budgets you set: overall or per project, in $ or tokens, forecast and alerted."""
from __future__ import annotations

import json

import pytest

from savetokens import cli, forecast, maintain, pools, projects, sync
from savetokens.store import Store, Usage

from conftest import H, T0


def _repo(path, url=None):
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "config").write_text('[core]\n\tbare = false\n' + (f'[remote "origin"]\n\turl = {url}\n' if url else ""))
    return path


@pytest.mark.parametrize("url", ["git@github.com:Org/Repo.git", "https://github.com/org/repo",
                                 "ssh://git@github.com/org/repo.git", "https://user@github.com/Org/Repo/"])
def test_however_a_repo_was_cloned_it_is_one_project(url):
    assert projects.normalize_remote(url) == "github.com/org/repo"


def test_a_folder_finds_its_project_walking_up(tmp_path):
    repo = _repo(tmp_path / "a" / "webapp", "git@github.com:org/webapp.git")
    (repo / "src" / "deep").mkdir(parents=True)
    assert projects.identity(str(repo / "src" / "deep")) == ("git", "github.com/org/webapp", "webapp")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {repo / '.git'}\n")                      # a worktree
    assert projects.identity(str(wt))[1] == "github.com/org/webapp"
    local = _repo(tmp_path / "scratch")                                          # no remote: this machine only
    assert projects.identity(str(local))[0] == "path"
    projects.link(tmp_path / "a", "client-x")                                    # a marker wins over the repo
    assert projects.identity(str(repo))[:3:2] == ("file", "client-x")


def test_the_same_repo_is_the_same_project_on_every_machine_with_the_hubs_key(tmp_path):
    repo = str(_repo(tmp_path / "webapp", "git@github.com:org/webapp.git"))
    a, b = Store(tmp_path / "a.db"), Store(tmp_path / "b.db")
    pa = projects.resolve(a, repo)[0]
    assert projects.resolve(b, repo)[0] != pa                                    # each machine's own key
    projects.set_key(a, "k" * 32)
    projects.set_key(b, "k" * 32)
    assert projects.resolve(a, repo)[0] == projects.resolve(b, repo)[0]          # the hub's key: one id
    scratch = str(_repo(tmp_path / "scratch"))
    assert projects.resolve(a, scratch)[0] != projects.resolve(b, scratch)[0]   # a plain folder isn't shared
    a.close(), b.close()


def test_requests_get_their_project_and_old_ones_are_filled_in(store, tmp_path):
    repo = str(_repo(tmp_path / "webapp", "git@github.com:org/webapp.git"))
    store.add_usage([Usage("claude-code", "s", "r1", T0, "m", cwd=repo)])
    pid = store.conn.execute("SELECT project_id FROM usage").fetchone()[0]
    assert pid == projects.resolve(store, repo)[0] and projects.names(store)[pid] == "webapp"
    store.conn.execute("UPDATE usage SET project_id = NULL")
    assert projects.fill(store) == 1


def _spend(store, cwd, usd, at, n=10):
    store.add_usage([Usage("claude-code", "s", f"{cwd}{at}{i}", at + i * 60, "m", cost_usd=usd / n, output=1000,
                           cwd=cwd, thread="s") for i in range(n)])


def test_a_project_budget_counts_only_that_project_and_is_forecast_and_alerted(store, tmp_path):
    web = str(_repo(tmp_path / "webapp", "git@github.com:org/webapp.git"))
    other = str(_repo(tmp_path / "other", "git@github.com:org/other.git"))
    for d in range(1, 8):                                  # $20 a day on webapp, $50 a day elsewhere
        _spend(store, web, 20, T0 - d * 86400)
        _spend(store, other, 50, T0 - d * 86400)
    pid = projects.resolve(store, web)[0]
    pools.set_goal(store, "webapp", {"label": "webapp", "scope": {"projects": [pid]}, "unit": "usd", "amount": 100,
                                     "by": T0 + 3 * 86400, "start": T0 - 8 * 86400, "tz": 0})
    o = next(o for o in forecast.outlook(store, T0, goals=True) if o["kind"] == "goal")
    assert o["spent"] == pytest.approx(140) and o["used"] == pytest.approx(140)   # already over: 7 days x $20
    maintain.update(store, T0, use_ephemeris=False)                # forecasts it like any other pool
    pools.set_goal(store, "overall", {"label": "Overall", "scope": {}, "unit": "tokens", "amount": 1e9,
                                      "period": "month", "start": T0, "tz": 0})
    maintain.update(store, T0 + 1, use_ephemeris=False)
    looks = {o["pool"]: o for o in forecast.outlook(store, T0 + 1, goals=True) if o["kind"] == "goal"}
    assert looks["goal:overall"]["p50"] is not None and looks["goal:overall"]["unit"] == "tokens"
    assert not any(o["kind"] == "goal" for o in forecast.outlook(store, T0))   # not where it isn't asked for


def test_a_budget_heading_over_raises_an_alert(store, tmp_path):
    web = str(_repo(tmp_path / "webapp", "git@github.com:org/webapp.git"))
    for d in range(1, 15):
        _spend(store, web, 30, T0 - d * 86400 + 3600)
    pid = projects.resolve(store, web)[0]
    pools.set_goal(store, "webapp", {"label": "webapp", "scope": {"projects": [pid]}, "unit": "usd", "amount": 200,
                                     "by": T0 + 6 * 86400, "start": T0 - 86400, "tz": 0})
    got = [a for a in maintain.update(store, T0, use_ephemeris=False) if a["name"] == "goal"]
    assert got and "webapp budget" in got[0]["message"] and "$" in got[0]["message"]


def test_the_hub_adds_a_project_up_across_machines(running, tmp_path, monkeypatch):
    users, token, url = running
    cfg = {"server_url": url, "server_token": token}
    repo = str(_repo(tmp_path / "webapp", "git@github.com:org/webapp.git"))
    a, b = Store(tmp_path / "a.db"), Store(tmp_path / "b.db")
    _spend(a, repo, 10, T0 - H)                        # before either knows the hub's key
    _spend(b, repo, 5, T0 - H)
    sync.push(a, cfg)
    sync.push(b, cfg)
    pid = projects.resolve(a, repo)[0]
    assert pid == projects.resolve(b, repo)[0]          # both took the hub's key before pushing
    pools.set_goal(a, "webapp", {"label": "webapp", "scope": {"projects": [pid]}, "unit": "usd", "amount": 100,
                                 "period": "month", "start": T0, "tz": 0})
    sync.push(a, cfg)
    monkeypatch.setattr("savetokens.server.time.time", lambda: T0)
    sync.pull(b, cfg)                                   # b gets the budget, and the hub's total for it
    assert "webapp" in pools.goals_(b)
    p = next(p for p in pools.pools(b, T0, goals=True) if p.kind == "goal")
    since, _ = pools.period(p.budget, T0)
    assert pools.spent(b, p, since, T0) == pytest.approx(15)       # both machines
    assert {r["id"]: r["usd"] for r in b.meta("hub_projects")["totals"]}[pid] == pytest.approx(15)
    a.close(), b.close()


def test_history_already_on_the_hub_gets_its_projects(running, tmp_path):
    users, token, url = running
    cfg = {"server_url": url, "server_token": token}
    s = Store(tmp_path / "a.db")
    s.add_usage([Usage("claude-code", "s", "r1", T0, "m", cost_usd=1.0)])          # pushed before projects
    s.set_meta("project_key_from", url)
    sync.push(s, cfg)
    s.conn.execute("UPDATE usage SET project_id = 'p1', thread = 's'")
    s.set_meta("project_key_from", None)
    s.set_meta("pushed:usage", 0)
    sync.push(s, cfg)
    with users.store("chris") as hub:
        assert tuple(hub.conn.execute("SELECT project_id, thread FROM usage").fetchone()) == ("p1", "s")
    s.close()


def test_budget_commands(store, tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path / "webapp", "git@github.com:org/webapp.git")
    _spend(store, str(repo), 10, T0 - H)
    store.close()
    monkeypatch.chdir(repo)
    assert cli.main(["budget", "--project", ".", "--tokens", "30M", "--by", "2099-12-15"]) == 0
    assert cli.main(["budget", "--usd", "500"]) == 0
    capsys.readouterr()
    assert cli.main(["budget", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)["budgets"]
    assert {o["label"] for o in got} == {"webapp budget", "Overall budget"}
    assert cli.main(["budget", "--project", "nope", "--usd", "5", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["fix"] == "savetokens projects"
    assert cli.main(["budget", "--off", "webapp"]) == 0
    assert cli.main(["projects", "--days", "3650", "--json"]) == 0
    got = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert got["projects"][0]["name"] == "webapp"
    assert cli.main(["project", "link", "client-x"]) == 0
    assert json.loads((repo / ".savetokens").read_text())["name"] == "client-x"


def test_a_budget_has_its_line_in_the_recap(store, tmp_path):
    from savetokens import recap
    web = str(_repo(tmp_path / "webapp", "git@github.com:org/webapp.git"))
    _spend(store, web, 30, T0 - 86400)
    pid = projects.resolve(store, web)[0]
    pools.set_goal(store, "webapp", {"label": "webapp", "scope": {"projects": [pid]}, "unit": "usd", "amount": 700,
                                     "period": "week", "start": T0 - 30 * 86400, "tz": 0})
    out = recap.text(recap.recap(store, T0, 7))
    assert "webapp budget: $30 of the $700 planned for these days, $670 under plan." in out
