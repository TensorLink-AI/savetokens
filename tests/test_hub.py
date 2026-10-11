"""The hub: codes for several machines, the one-line join, `server --setup`, and the add-a-machine endpoints."""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request

import pytest

from savetokens import hub, server
from savetokens.store import load_config


def _req(url, headers=None, body=None):
    req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_a_code_can_add_several_machines_until_used_up_or_expired(tmp_path, monkeypatch):
    users = server.Users(tmp_path / "srv")
    code = users.pair("chris", seconds=60, uses=2)
    a, b = users.join(code), users.join(code.lower().replace("-", ""))
    assert users.who(a) == users.who(b) == "chris" and a != b
    assert users.join(code) is None                                    # used up
    old = users.pair("chris", seconds=60, uses=5)
    monkeypatch.setattr(server.time, "time", lambda: 9e12)
    assert users.join(old) is None                                     # expired


def test_the_hub_serves_a_join_script_for_the_address_it_was_reached_at(running, tmp_path):
    _, _, url = running
    code, body = _req(url + "/join")
    script = body.decode()
    assert code == 200 and f"HUB='{url}'" in script and "install --server" in script
    assert subprocess.run(["sh", "-n"], input=script, text=True).returncode == 0
    _, body = _req(url + "/join", {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "hub.example.com"})
    assert "HUB='https://hub.example.com'" in body.decode()
    assert _req(url + "/join", {"Host": "x'; rm -rf ~; '"})[0] == 400   # nothing odd gets into the script
    assert subprocess.run(["sh", "-c", script, "sh"], capture_output=True).returncode == 2   # no code: usage

    # with savetokens already on PATH, it joins: no install, and --yes with no terminal
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    fake = bin_ / "savetokens"
    fake.write_text(f"#!/bin/sh\necho \"$@\" > {tmp_path / 'args'}\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(tmp_path)}
    r = subprocess.run(["setsid", "sh", "-c", script, "sh", "ABCD-2345"], env=env, capture_output=True,
                       stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "args").read_text().split() == ["install", "--server", url, "--code", "ABCD-2345", "--yes"]


def test_add_a_machine_codes_and_ci_tokens(running):
    users, token, url = running
    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    code, body = _req(url + "/v1/pair", auth, json.dumps({"machines": 3, "hours": 24}).encode())
    got = json.loads(body)
    assert code == 200 and got["machines"] == 3 and got["expires_in"] == 86400
    assert got["command"] == f"curl -fsSL {url}/join | sh -s {got['code']}"
    assert all(users.join(got["code"]) for _ in range(3)) and users.join(got["code"]) is None
    assert _req(url + "/v1/pair", auth, json.dumps({"machines": 500}).encode())[0] == 400
    assert json.loads(_req(url + "/v1/pair", auth, b"")[1])["expires_in"] == server.CODE_SECONDS   # older clients
    _, body = _req(url + "/v1/token", auth, b"{}")
    ci = json.loads(body)["token"]
    assert users.who(ci) == "chris"
    assert _req(url + "/v1/token", {"Content-Type": "application/json"}, b"{}")[0] == 401


def test_a_fresh_hub_prints_the_line_for_the_first_machines_until_one_joins(tmp_path, monkeypatch):
    monkeypatch.setenv("SAVETOKENS_PUBLIC_URL", "https://st.example.com")
    users, log = server.Users(tmp_path / "srv"), []
    assert server.first_user(users, log=log.append).startswith("st_")
    line = next(l for l in log if "/join" in l).strip()
    assert line.startswith("curl -fsSL https://st.example.com/join | sh -s ")
    log.clear()
    assert server.first_user(users, log=log.append) is None and any("/join" in l for l in log)   # a restart: again
    with users.store("me"):
        pass
    log.clear()
    server.first_user(users, log=log.append)
    assert not log                                                     # a machine joined: quiet


def test_tailscale_name_is_preferred(monkeypatch):
    monkeypatch.setattr(hub.shutil, "which", lambda n: "/usr/bin/" + n)
    status = {"Self": {"DNSName": "box.tail1234.ts.net.", "TailscaleIPs": ["100.64.0.5", "fd7a::5"], "Online": True}}
    run = lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(status), "")
    assert hub.address(8787, run) == ("http://box.tail1234.ts.net:8787", "tailscale")
    down = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "not logged in")
    assert hub.tailscale(down) is None


def test_server_setup_makes_a_systemd_service_joins_this_machine_and_prints_the_line(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(hub.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(hub, "address", lambda port, run=None: (f"http://box.ts.net:{port}", "tailscale"))
    monkeypatch.setattr(hub, "healthy", lambda *a, **k: True)
    ran = []

    def run(cmd, **k):
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    log = []
    got = hub.setup(8787, tmp_path / "srv", interactive=False, out=log.append, run=run, platform="linux")
    unit = hub.systemd_path().read_text()
    assert "server --host 0.0.0.0 --port 8787 --data" in unit and "WantedBy=default.target" in unit
    assert ["systemctl", "--user", "enable", "savetokens-server"] in ran and got["started"] == "systemd"
    assert got["command"] == f"curl -fsSL http://box.ts.net:8787/join | sh -s {got['code']}"
    assert got["command"] in "\n".join(log)
    cfg = load_config()
    users = server.Users(tmp_path / "srv")
    assert cfg["server_url"] == "http://127.0.0.1:8787" and users.who(cfg["server_token"]) == "me"
    assert users.who(users.join(got["code"])) == "me"                 # the printed code works
    assert hub.stop(run) and not hub.systemd_path().exists()


def test_server_setup_says_how_to_run_it_when_nothing_can_start_it(tmp_path, monkeypatch):
    monkeypatch.setattr(hub.shutil, "which", lambda n: None)
    monkeypatch.setattr(hub, "address", lambda port, run=None: (f"http://192.168.1.4:{port}", "lan"))
    monkeypatch.setattr(hub, "healthy", lambda *a, **k: False)
    monkeypatch.setattr(hub, "wsl", lambda: True)
    log = []
    got = hub.setup(8787, tmp_path / "srv", interactive=False, out=log.append, platform="linux")
    text = "\n".join(log)
    assert got["started"] is None and "server --host 0.0.0.0" in text and "WSL" in text


@pytest.mark.parametrize("flag", ["--setup", "--stop"])
def test_server_flags_are_listed(flag, capsys):
    from savetokens import cli
    with pytest.raises(SystemExit):
        cli.main(["server", "--help"])
    assert flag in capsys.readouterr().out
