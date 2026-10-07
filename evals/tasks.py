"""Layer-1 eval tasks: small repos with hidden graders.

Three kinds:
  normal     ordinary coding work, so the comparison is fair
  waste      tempts token waste (huge logs, noisy output, broad search)
  legit      the "wasteful-looking" behaviour is correct here (polling, flaky
             retries, errors at the top of long output, running the full suite);
             these catch guard false positives and quality loss

Each task: files (path -> text), a prompt, and grade(workspace) -> (passed, note).
Graders run after the agent finishes and are never visible to it.
"""
from __future__ import annotations

import hashlib
import json
import random
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class Task:
    id: str
    kind: str
    prompt: str
    files: dict
    grade: Callable[[Path], tuple]
    setup: Callable[[Path], None] | None = None   # builds the workspace instead of `files` (mined tasks)


def _py(ws: Path, code: str, timeout=60):
    r = subprocess.run([sys.executable, "-c", code], cwd=ws, capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, (r.stdout + r.stderr)[-300:]


def _sha(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


TASKS: list[Task] = []


def task(fn):
    TASKS.append(fn())
    return fn


# ── normal ───────────────────────────────────────────────────────────────────

@task
def off_by_one():
    return Task("off_by_one", "normal",
                "moving_average in stats.py returns the wrong number of windows. Fix it.",
                {"stats.py": "def moving_average(xs, n):\n"
                             "    \"\"\"Means of each full window of n consecutive values.\"\"\"\n"
                             "    return [sum(xs[i:i + n]) / n for i in range(len(xs) - n)]\n"},
                lambda ws: _py(ws, "from stats import moving_average as m\n"
                                   "assert m([1,2,3,4],2)==[1.5,2.5,3.5]\nassert m([5],1)==[5.0]\nassert m([1,2],3)==[]"))


@task
def cli_flag():
    return Task("cli_flag", "normal",
                "Add a --words flag to wc.py that prints the number of words instead of lines. Keep the default.",
                {"wc.py": "import sys\n\n\ndef main(argv):\n    path = argv[-1]\n    with open(path) as f:\n"
                          "        print(sum(1 for _ in f))\n\n\nif __name__ == '__main__':\n    main(sys.argv[1:])\n",
                 "sample.txt": "one two\nthree four five\n"},
                lambda ws: _py(ws, "import subprocess,sys\n"
                                   "o=lambda *a: subprocess.run([sys.executable,'wc.py',*a,'sample.txt'],capture_output=True,text=True).stdout.strip()\n"
                                   "assert o()=='2', o()\nassert o('--words')=='5', o('--words')"))


@task
def rename():
    files = {"app/__init__.py": "", "app/users.py": "def get_user(uid):\n    return {'id': uid}\n"}
    for i in range(5):
        files[f"app/view{i}.py"] = f"from app.users import get_user\n\n\ndef view{i}(uid):\n    return get_user(uid)['id'] + {i}\n"

    def grade(ws):
        left = [p for p in ws.rglob("*.py") if "get_user" in p.read_text()]
        if left:
            return False, f"get_user still in {left[0].name}"
        return _py(ws, "from app.users import fetch_user\nimport app.view3 as v\nassert v.view3(7)==10")
    return Task("rename", "normal", "Rename get_user to fetch_user everywhere in the app package.", files, grade)


# ── waste traps ──────────────────────────────────────────────────────────────

@task
def big_log():
    rng = random.Random(1)
    lines = [f"2026-10-0{1 + i % 5} 12:{i % 60:02d}:00 INFO worker-{rng.randint(1, 9)} heartbeat ok seq={i}"
             for i in range(40_000)]
    lines.insert(31_337, "2026-10-04 03:12:09 ERROR config: settings.ini timeout=0 is invalid; it must be"
                         " between 1 and 60 seconds")
    return Task("big_log", "waste",
                "The service fails at startup. app.log has the reason. Fix the configuration.",
                {"app.log": "\n".join(lines) + "\n", "settings.ini": "[server]\nport = 8080\ntimeout = 0\n"},
                lambda ws: _py(ws, "import configparser\nc=configparser.ConfigParser();c.read('settings.ini')\n"
                                   "t=c.getint('server','timeout');assert 1<=t<=60,t\nassert c.getint('server','port')==8080"))


@task
def noisy_tests():
    test = ("import logging\nfrom price import total\n\nlogging.basicConfig(level=logging.DEBUG)\n\n\n"
            "def test_noise():\n    for i in range(3000):\n        print(f'debug: computing line item {i} of 3000 ...')\n"
            "    assert True\n\n\ndef test_total():\n    assert total([(2, 1.5), (1, 4.0)], tax=0.1) == 7.7\n")
    return Task("noisy_tests", "waste",
                "One test in test_price.py fails. Find out why and fix the code (not the tests).",
                {"price.py": "def total(items, tax):\n    s = sum(q * p for q, p in items)\n    return round(s + tax, 2)\n",
                 "test_price.py": test},
                lambda ws: _py(ws, "from price import total\nassert total([(2,1.5),(1,4.0)],tax=0.1)==7.7\n"
                                   "assert total([],tax=0.2)==0"))


@task
def wide_search():
    files = {}
    rng = random.Random(2)
    for i in range(150):
        files[f"pkg/mod{i:03d}.py"] = "\n".join(f"VALUE_{rng.randint(0, 999)} = {rng.randint(0, 99)}"
                                                for _ in range(20)) + "\n"
    files["pkg/mod117.py"] += "RETRY_LIMIT = 0  # retries before giving up\n"
    files["client.py"] = ("import importlib\n\n\ndef retry_limit():\n    for i in range(150):\n"
                          "        m = importlib.import_module(f'pkg.mod{i:03d}')\n"
                          "        if hasattr(m, 'RETRY_LIMIT'):\n            return m.RETRY_LIMIT\n")
    files["pkg/__init__.py"] = ""
    return Task("wide_search", "waste",
                "client.retry_limit() returns 0, so requests never retry. Set the retry limit to 3 where it is defined.",
                files, lambda ws: _py(ws, "import client\nassert client.retry_limit()==3"))


# ── legitimate repeats (guard false-positive traps) ─────────────────────────

@task
def flaky_server():
    conftest = ("import pathlib\n\nC = pathlib.Path(__file__).with_name('.attempts')\n\n\n"
                "def pytest_sessionstart(session):\n    n = int(C.read_text()) if C.exists() else 0\n"
                "    C.write_text(str(n + 1))\n    if n < 3:\n"
                "        raise SystemExit('ConnectionError: test server on :9100 not ready yet (warming up),"
                " retry in a moment')\n")
    files = {"conftest.py": conftest, "api.py": "def health():\n    return {'status': 'ok'}\n",
             "test_api.py": "from api import health\n\n\ndef test_health():\n    assert health()['status'] == 'ok'\n"}
    originals = {k: hashlib.sha256(v.encode()).hexdigest() for k, v in files.items()}

    def grade(ws):
        changed = [k for k, h in originals.items() if _sha(ws / k) != h]
        attempts = int((ws / ".attempts").read_text()) if (ws / ".attempts").exists() else 0
        if changed:
            return False, f"changed {changed} (nothing was wrong with the code)"
        return attempts >= 4, f"pytest sessions: {attempts} (needs 4 to get past warm-up)"
    return Task("flaky_server", "legit",
                "Run the test suite with pytest and confirm it passes. The local test server takes a few"
                " tries to warm up; the code itself is fine, so don't change any files.", files, grade)


@task
def polling_build():
    build = ("#!/usr/bin/env bash\nS=.build_state\ncase \"$1\" in\n"
             "  start) echo 0 > $S; echo 'build started';;\n"
             "  status) n=$(( $(cat $S 2>/dev/null || echo 0) + 1 )); echo $n > $S;\n"
             "    if [ $n -ge 5 ]; then mkdir -p out; echo 2.7.1 > out/version.txt; echo 'done: out/version.txt';"
             " else echo 'building... (not ready)'; fi;;\nesac\n")

    def grade(ws):
        v = (ws / "VERSION").read_text().strip() if (ws / "VERSION").exists() else ""
        return v == "2.7.1", f"VERSION={v!r}"
    return Task("polling_build", "legit",
                "Start the build with ./build.sh start, then check ./build.sh status until it reports done"
                " (it takes several checks). Then write the version from the artifact into a VERSION file.",
                {"build.sh": build}, grade)


@task
def error_on_top():
    noise = "\n".join(f"warning: deprecated option in line {i}" for i in range(6000))
    check = ("#!/usr/bin/env bash\nif grep -q 'mode = legacy' app.cfg; then\n"
             "  echo 'FATAL: app.cfg mode=legacy is no longer supported; use mode = modern'\n"
             f"  cat <<'EOF'\n{noise}\nEOF\n  exit 1\nfi\necho OK\n")
    return Task("error_on_top", "legit",
                "./check.sh fails. Make it pass.",
                {"check.sh": check, "app.cfg": "mode = legacy\nthreads = 4\n"},
                lambda ws: (subprocess.run(["bash", "check.sh"], cwd=ws, capture_output=True).returncode == 0
                            and "threads = 4" in (ws / "app.cfg").read_text(), "check.sh"))


@task
def full_suite():
    files = {"money.py": "def to_cents(x):\n    return int(x * 100)\n",
             "invoice.py": "from money import to_cents\n\n\ndef line(qty, price):\n    return qty * to_cents(price)\n",
             "test_money.py": "from money import to_cents\n\n\ndef test_basic():\n    assert to_cents(1.25) == 125\n",
             "test_invoice.py": "from invoice import line\n\n\ndef test_line():\n    assert line(3, 0.29) == 87\n"}
    return Task("full_suite", "legit",
                "to_cents(0.29) returns 28 instead of 29. Fix to_cents in money.py.",
                files, lambda ws: _py(ws, "from money import to_cents\nfrom invoice import line\n"
                                          "assert to_cents(0.29)==29 and to_cents(1.25)==125 and to_cents(19.99)==1999\n"
                                          "assert line(3,0.29)==87\nassert isinstance(to_cents(2.5),int)"))


def materialise(t: Task, ws: Path):
    if t.setup:
        t.setup(ws)
        return
    for rel, text in t.files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        if rel.endswith(".sh"):
            p.chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=ws)
    subprocess.run(["git", "add", "-A"], cwd=ws)
    subprocess.run(["git", "-c", "user.email=eval@local", "-c", "user.name=eval", "commit", "-qm", "task"], cwd=ws)


if __name__ == "__main__":
    print(json.dumps([{"id": t.id, "kind": t.kind} for t in TASKS], indent=1))
