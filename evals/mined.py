"""Tasks mined from a repository's own history: "make the change this commit made".

A commit qualifies when it changes source files and test files, and its new tests
fail on the parent commit but pass on the commit itself. The workspace is the
parent commit (exported with `git archive`, so the source repo is never touched);
the prompt is the commit message; the grader drops in the commit's test files and
runs them. Validated tasks are cached as JSONL, so mining runs once per repo.

  python3 evals/mined.py /path/to/repo --python /path/to/repo/.venv/bin/python --limit 40
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

from tasks import Task

HERE = Path(__file__).parent
CACHE = HERE / "mined"
TEST_RE = re.compile(r"(^|/)tests?/.*test_[^/]*\.py$|(^|/)test_[^/]*\.py$")
SKIP_LINES = re.compile(r"^(Co-Authored-By|Signed-off-by|🤖 Generated)", re.I | re.M)


def git(repo, *args, binary=False):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    return r.stdout if binary else r.stdout.decode()


def export(repo, commit, dest: Path):
    data = git(repo, "archive", "--format=tar", commit, binary=True)
    with tarfile.open(fileobj=BytesIO(data)) as tf:
        tf.extractall(dest, filter="data")


def candidates(repo, since, branch, max_src_lines):
    out = []
    for c in git(repo, "rev-list", "--no-merges", f"--since={since}", branch).split():
        rows = [l.split("\t") for l in git(repo, "diff-tree", "--no-commit-id", "--numstat", "-r", c).splitlines()]
        tests = [p for a, d, p in rows if TEST_RE.search(p) and a != "-"]
        src = [(int(a), int(d), p) for a, d, p in rows if p.endswith(".py") and not TEST_RE.search(p) and a != "-"
               and not p.startswith(("benchmarks/", "scripts/", "examples/"))]
        changed = sum(a + d for a, d, _ in src)
        if tests and src and 3 <= changed <= max_src_lines:
            out.append((c, tests, [p for _, _, p in src], changed))
    return out


def _pytest(python, ws, tests, timeout=300):
    try:
        r = subprocess.run([python, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests], cwd=ws,
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr)[-400:]
    except subprocess.TimeoutExpired:
        return False, "timeout"


def validate(repo, python, cand):
    """The commit's tests must fail on the parent and pass on the commit."""
    c, tests, src, changed = cand
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        export(repo, f"{c}^", ws)
        hidden = {}
        for t in tests:
            hidden[t] = git(repo, "show", f"{c}:{t}")
            (ws / t).parent.mkdir(parents=True, exist_ok=True)
            (ws / t).write_text(hidden[t])
        before, _ = _pytest(python, ws, tests)
        if before:
            return None
        for p in src:
            try:
                (ws / p).parent.mkdir(parents=True, exist_ok=True)
                (ws / p).write_text(git(repo, "show", f"{c}:{p}"))
            except subprocess.CalledProcessError:   # deleted in the commit
                (ws / p).unlink(missing_ok=True)
        after, note = _pytest(python, ws, tests)
        if not after:
            return None
    msg = SKIP_LINES.sub("", git(repo, "log", "-1", "--format=%B", c)).strip()
    return {"id": f"{Path(repo).name.lower()}-{c[:8]}", "repo": str(repo), "commit": c, "python": python,
            "tests": tests, "hidden": hidden, "message": msg, "src_lines": changed}


def mine(repo, python, since="2026-07-01", branch="main", limit=40, max_src_lines=150, workers=6):
    cands = candidates(repo, since, branch, max_src_lines)
    found = []
    with ThreadPoolExecutor(workers) as pool:
        for rec in pool.map(lambda c: validate(repo, python, c), cands):
            if rec:
                found.append(rec)
                print(f"  ok {rec['id']}  {rec['src_lines']} src lines  {rec['message'].splitlines()[0][:70]}")
                if len(found) >= limit:
                    break
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"{Path(repo).name.lower()}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in found))
    print(f"{len(found)} validated tasks from {len(cands)} candidates -> {path}")
    return path


def load(path, mode="tdd") -> list[Task]:
    return [as_task(json.loads(line), mode) for line in Path(path).read_text().splitlines()]


def as_task(rec, mode="tdd") -> Task:
    """mode "tdd": the commit's tests are in the workspace and define the change (well specified).
    mode "spec": only the commit message (harder, and underspecified when tests check exact names)."""
    py = rec["python"]

    def setup(ws, rec=rec):
        export(rec["repo"], f"{rec['commit']}^", ws)
        if mode == "tdd":
            for t, text in rec["hidden"].items():
                (ws / t).parent.mkdir(parents=True, exist_ok=True)
                (ws / t).write_text(text)
        subprocess.run(["git", "init", "-q"], cwd=ws)
        subprocess.run(["git", "add", "-A"], cwd=ws)
        subprocess.run(["git", "-c", "user.email=eval@local", "-c", "user.name=eval", "commit", "-qm", "base"], cwd=ws)

    def grade(ws, rec=rec):
        for t, text in rec["hidden"].items():   # the commit's tests replace whatever the agent left there
            (ws / t).parent.mkdir(parents=True, exist_ok=True)
            (ws / t).write_text(text)
        return _pytest(py, ws, rec["tests"])

    run_tests = f"Use {py} to run Python and tests (e.g. `{py} -m pytest -q tests/...`)."
    if mode == "tdd":
        prompt = (f"In this repository, make the following change.\n\n{rec['message']}\n\n"
                  f"The tests in {', '.join(rec['tests'])} describe the expected behaviour and currently fail."
                  f" Make them pass without editing them, and keep existing behaviour working. {run_tests}")
    else:
        prompt = (f"In this repository, make the following change.\n\n{rec['message']}\n\n"
                  f"{run_tests} Hidden tests will check the change; keep existing behaviour working.")
    return Task(rec["id"], f"mined-{mode}", prompt, {}, grade, setup=setup)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--python", required=True)
    ap.add_argument("--since", default="2026-07-01")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--max-src-lines", type=int, default=150)
    a = ap.parse_args()
    mine(a.repo, a.python, a.since, a.branch, a.limit, a.max_src_lines)
