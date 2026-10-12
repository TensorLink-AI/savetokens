"""Teams on a hub: one project key, every member's usage added up, roles, team budgets and their alerts."""
from __future__ import annotations

import json
import urllib.request

import pytest

from savetokens import cli, maintain, projects, sync
from savetokens.store import Store, Usage
from savetokens.teams import Teams

from conftest import H, T0


def _repo(path):
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "config").write_text('[remote "origin"]\n\turl = git@github.com:acme/webapp.git\n')
    return str(path)


def _get(url, token, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def team(running, tmp_path):
    users, chris, url = running
    sam = users.add("sam")
    t = Teams(users.data)
    t.add("acme", "chris")                       # the first member owns it
    t.add("acme", "sam")
    repo = _repo(tmp_path / "webapp")
    machines = {}
    for who, token, usd in (("chris", chris, 30.0), ("sam", sam, 10.0)):
        m = Store(tmp_path / f"{who}.db")
        m.add_usage([Usage("claude-code", f"{who}-s", f"{who}-{i}", T0 - H + i, "claude-opus-5-5", cost_usd=usd / 3,
                           output=1000, cwd=repo, thread=f"{who}-s") for i in range(3)])
        cfg = {"server_url": url, "server_token": token}
        sync.push(m, cfg)
        machines[who] = (m, cfg)
    yield users, t, url, {"chris": chris, "sam": sam}, machines, repo
    for m, _ in machines.values():
        m.close()


def test_members_share_project_ids_and_the_team_adds_them_up(team):
    users, t, url, tokens, machines, repo = team
    a, b = (projects.resolve(machines[w][0], repo)[0] for w in ("chris", "sam"))
    assert a == b                                           # the team's key: one project for everyone
    with t.store("acme") as ts:
        got = dict(ts.conn.execute("SELECT person, SUM(cost_usd) FROM usage GROUP BY person").fetchall())
    assert got == pytest.approx({"chris": 30.0, "sam": 10.0})


def test_who_sees_the_people(team, monkeypatch):
    users, t, url, tokens, machines, repo = team
    monkeypatch.setattr("savetokens.server.time.time", lambda: T0)
    code, owner = _get(url + "/v1/team", tokens["chris"])
    assert code == 200 and owner["role"] == "owner" and {p["person"] for p in owner["people"]} == {"chris", "sam"}
    assert owner["projects"][0]["people"] == 2 and owner["usd"] == pytest.approx(40.0)
    code, member = _get(url + "/v1/team", tokens["sam"])
    assert member["role"] == "member" and "people" not in member and "goals" not in member
    assert _get(url + "/v1/team", tokens["sam"], {"people_visible": True})[0] == 403
    assert _get(url + "/v1/team", tokens["chris"], {"people_visible": True})[0] == 200
    assert "people" in _get(url + "/v1/team", tokens["sam"])[1]


def test_team_budgets_are_set_by_admins_forecast_and_reach_members_as_alerts(team, monkeypatch):
    users, t, url, tokens, machines, repo = team
    pid = projects.resolve(machines["chris"][0], repo)[0]
    goal = {"webapp": {"label": "Team webapp", "scope": {"projects": [pid]}, "unit": "usd", "amount": 50,
                       "by": T0 + 86400, "start": T0 - 2 * H, "tz": 0}}
    assert _get(url + "/v1/team", tokens["sam"], {"goals": goal})[0] == 403
    assert _get(url + "/v1/team", tokens["chris"], {"goals": goal})[0] == 200
    with t.store("acme") as ts:
        maintain.update(ts, T0, use_ephemeris=False)
        ts.conn.execute("INSERT INTO alerts (account, ts, name, window_end, stage, message) VALUES"
                        " ('', ?, 'goal', 1, 'act', 'Team webapp budget: 80% used')", (T0,))
        ts.conn.commit()
    monkeypatch.setattr("savetokens.server.time.time", lambda: T0)
    budgets = _get(url + "/v1/team", tokens["sam"])[1]["budgets"]
    assert budgets[0]["label"] == "Team webapp budget" and budgets[0]["spent"] == pytest.approx(40.0)
    m, cfg = machines["sam"]
    sync.pull(m, cfg)
    msgs = [r[0] for r in m.conn.execute("SELECT message FROM alerts WHERE account = 'team'")]
    assert any(x.startswith("Team: Team webapp budget") for x in msgs)


def test_a_shared_name_is_what_the_team_sees(team):
    users, t, url, tokens, machines, repo = team
    pid = projects.resolve(machines["sam"][0], repo)[0]
    assert _get(url + "/v1/team", tokens["sam"], {"share": {pid: "webapp"}})[0] == 200
    assert _get(url + "/v1/team", tokens["chris"])[1]["projects"][0]["name"] == "webapp"


def test_adding_a_member_on_the_hub(tmp_path, capsys):
    assert cli.main(["server", "--data", str(tmp_path), "--team", "acme", "--member", "ana"]) == 0
    out = capsys.readouterr().out
    assert "ana is owner of acme" in out and "join" in out
    assert cli.main(["server", "--data", str(tmp_path), "--team", "acme", "--member", "bo", "--role", "admin"]) == 0
    assert Teams(tmp_path).of("bo") == ("acme", "admin")
    assert cli.main(["server", "--data", str(tmp_path), "--team", "other", "--member", "bo"]) == 1   # one team each
