"""Tasks: units of work inside a project, so tokens can be counted, budgeted and estimated per task.

A request's task is, first that applies:
  1. declared: `savetokens task start NAME` (or an agent's task_start) in the project, until `task done`
  2. a tracker key in its branch: a pattern you set (`savetokens task pattern '[A-Z]+-[0-9]+'`) found in
     the branch name, e.g. feature/ENG-142-oauth is the task ENG-142
  3. its branch: any branch but the repository's default one (origin's HEAD, else git's own default names);
     turn branches off with `savetokens task branches off`

What's stored and synced is an id: HMAC(your project key, project id and task name), so a task is the
same on every machine without its name leaving any of them.

Estimates come from tasks marked done (`task done`), never from rules: the cost of finished tasks in the
project, with how many there were, or "not enough data yet".
"""
from __future__ import annotations

import hashlib
import hmac
import re
import statistics
import time

from . import projects

DEFAULT_NAMES = ("main", "master")   # git's own default branch names, when origin's HEAD isn't known
MIN_DONE = 3                        # finished tasks needed before an estimate is shown


def task_id(store, project_id, name) -> str:
    return hmac.new(projects.key(store).encode(), f"task:{project_id}:{name}".encode(),
                    hashlib.sha256).hexdigest()[:16]


def _settings(store):
    return store.meta("task_settings") or {"branches": True, "pattern": None}


def default_branch(cwd) -> str | None:
    """The repository's default branch, from origin's HEAD."""
    from pathlib import Path
    for d in [Path(cwd), *Path(cwd).parents]:
        try:
            git = projects._git_dir(d)
        except OSError:
            continue
        if git:
            head = git / "refs" / "remotes" / "origin" / "HEAD"
            try:
                m = re.match(r"ref: refs/remotes/origin/(.+)", head.read_text().strip())
                return m.group(1) if m else None
            except OSError:
                return None
    return None


_defaults: dict = {}


def _is_default(cwd, branch) -> bool:
    if cwd not in _defaults:
        _defaults[cwd] = default_branch(cwd) if cwd else None
    d = _defaults[cwd]
    return branch == d if d else branch in DEFAULT_NAMES


def _declared(store, project_id, ts):
    for r in store.conn.execute("SELECT id FROM tasks WHERE project_id = ? AND source = 'declared' AND started <= ?"
                                " AND (stopped IS NULL OR stopped > ?) AND (done IS NULL OR done > ?)"
                                " ORDER BY started DESC LIMIT 1", (project_id, ts, ts, ts)):
        return r[0]
    return None


def _named(store, project_id, name, source):
    tid = task_id(store, project_id, name)
    store.conn.execute("INSERT OR IGNORE INTO tasks (id, project_id, name, source) VALUES (?,?,?,?)",
                       (tid, project_id, name, source))
    return tid


def attribute(store, e) -> str | None:
    """The task a request belongs to (an id), or None."""
    if not e.project_id:
        return None
    tid = _declared(store, e.project_id, e.ts)
    if tid:
        return tid
    b = e.branch
    if not b or b == "HEAD":
        return None
    s = _settings(store)
    if s.get("pattern"):
        try:
            m = re.search(s["pattern"], b)
        except re.error:
            m = None
        if m:
            return _named(store, e.project_id, m.group(0), "tracker")
    if s.get("branches", True) and not _is_default(e.cwd, b):
        return _named(store, e.project_id, b, "branch")
    return None


def fill(store) -> int:
    """Attribute requests read before their task was known (or before a task was declared)."""
    from .store import Usage
    n = 0
    rows = store.conn.execute("SELECT rowid, ts, cwd, branch, project_id FROM usage WHERE task_id IS NULL"
                              " AND project_id IS NOT NULL AND (branch IS NOT NULL OR project_id IN"
                              " (SELECT project_id FROM tasks WHERE source = 'declared'))").fetchall()
    for r in rows:
        tid = attribute(store, Usage("", "", "", r["ts"], cwd=r["cwd"], branch=r["branch"], project_id=r["project_id"]))
        if tid:
            store.conn.execute("UPDATE usage SET task_id = ? WHERE rowid = ?", (tid, r["rowid"]))
            n += 1
    store.conn.commit()
    return n


def start(store, cwd, name, now=None) -> dict:
    """Declare a task in this folder's project: requests from now on count to it, until done."""
    now = now or time.time()
    pid, project = projects.resolve(store, cwd)
    cur = _declared(store, pid, now)
    if cur:   # stopped, not done: only `task done` says a task is finished
        store.conn.execute("UPDATE tasks SET stopped = ? WHERE id = ?", (now, cur))
    tid = task_id(store, pid, name)
    store.conn.execute("INSERT INTO tasks (id, project_id, name, source, started) VALUES (?,?,?,?,?)"
                       " ON CONFLICT (id) DO UPDATE SET source = 'declared', started = excluded.started,"
                       " stopped = NULL, done = NULL", (tid, pid, name, "declared", now))
    store.conn.commit()
    return {"id": tid, "name": name, "project": project, "project_id": pid, "started": now,
            "estimate": estimate(store, pid)}


def current(store, cwd, now=None) -> dict | None:
    """The task this folder is on now: declared, else the branch's (from git, as it is now)."""
    from .store import Usage
    now = now or time.time()
    pid, project = projects.resolve(store, cwd)
    branch = _head_branch(cwd)
    tid = attribute(store, Usage("", "", "", now, cwd=cwd, branch=branch, project_id=pid))
    if not tid:
        return None
    r = store.conn.execute("SELECT name, source, started, done FROM tasks WHERE id = ?", (tid,)).fetchone()
    return {"id": tid, "project_id": pid, "project": project, **dict(r)} if r else None


def _head_branch(cwd):
    from pathlib import Path
    for d in [Path(cwd), *Path(cwd).parents]:
        git = projects._git_dir(d)
        if git:
            head = (git / "HEAD") if (git / "HEAD").exists() else None
            try:   # a worktree's HEAD is in its own gitdir, not the common one
                dotgit = d / ".git"
                if dotgit.is_file():
                    m = re.match(r"gitdir:\s*(.+)", dotgit.read_text().strip())
                    head = (d / m.group(1)).resolve() / "HEAD" if m else head
                m = re.match(r"ref: refs/heads/(.+)", head.read_text().strip()) if head else None
                return m.group(1) if m else "HEAD"
            except OSError:
                return None
    return None


def done(store, cwd, name=None, now=None) -> dict | None:
    """Mark a task done: the named one in this folder's project, else the current one."""
    now = now or time.time()
    if name:
        pid, _ = projects.resolve(store, cwd)
        r = store.conn.execute("SELECT id FROM tasks WHERE project_id = ? AND name = ?", (pid, name)).fetchone()
        t = {"id": r[0]} if r else None
    else:
        t = current(store, cwd, now)
    if not t:
        return None
    store.conn.execute("UPDATE tasks SET done = ? WHERE id = ?", (now, t["id"]))
    store.conn.commit()
    return totals(store, t["id"])


def totals(store, tid) -> dict:
    r = store.conn.execute(
        "SELECT t.name, t.source, t.started, t.done, t.project_id, COUNT(u.rowid) n, COALESCE(SUM(u.cost_usd), 0) usd,"
        " COALESCE(SUM(u.input + u.cache_read + u.cache_write_5m + u.cache_write_1h + u.output), 0) tokens,"
        " MIN(u.ts) first, MAX(u.ts) last, COUNT(DISTINCT u.session_id) sessions"
        " FROM tasks t LEFT JOIN usage u ON u.task_id = t.id WHERE t.id = ? GROUP BY t.id", (tid,)).fetchone()
    return {"id": tid, **dict(r)} if r else {"id": tid}


def listing(store, project_ids=None, since=None) -> list[dict]:
    q = "SELECT id FROM tasks"
    p = []
    if project_ids:
        q += f" WHERE project_id IN ({','.join('?' * len(project_ids))})"
        p = list(project_ids)
    out = [totals(store, r[0]) for r in store.conn.execute(q, p)]
    if since:
        out = [t for t in out if (t.get("last") or t.get("started") or 0) >= since]
    return sorted(out, key=lambda t: -(t.get("last") or t.get("started") or 0))


def estimate(store, project_id) -> dict:
    """What finished tasks in this project cost: measured, with how many there were."""
    done_ = [t for t in listing(store, [project_id]) if t.get("done") and t.get("n")]
    if len(done_) < MIN_DONE:
        return {"kind": "measured", "n": len(done_), "enough": False,
                "text": f"not enough data yet: {len(done_)} finished task{'s' if len(done_) != 1 else ''} in this"
                        f" project (estimates start at {MIN_DONE})"}
    usd = sorted(t["usd"] for t in done_)
    tok = sorted(t["tokens"] for t in done_)
    q = lambda v, f: v[min(len(v) - 1, int(f * len(v)))]
    return {"kind": "measured", "n": len(done_), "enough": True, "median_usd": statistics.median(usd),
            "p90_usd": q(usd, 0.9), "median_tokens": statistics.median(tok), "p90_tokens": q(tok, 0.9),
            "text": f"finished tasks in this project: median ${statistics.median(usd):,.2f}"
                    f" ({statistics.median(tok) / 1e6:,.1f}M tokens), 9 in 10 under ${q(usd, 0.9):,.2f};"
                    f" from {len(done_)} tasks"}


def rekey(store, pid_map: dict):
    """After the project key changed: each task under its project's new id, done marks kept."""
    rows = store.conn.execute("SELECT id, project_id, name FROM tasks").fetchall()
    for old, pid, name in rows:
        new_pid = pid_map.get(pid, pid)
        new = task_id(store, new_pid, name)
        if new != old:
            store.conn.execute("UPDATE OR REPLACE tasks SET id = ?, project_id = ? WHERE id = ?", (new, new_pid, old))
            store.conn.execute("UPDATE usage SET task_id = ? WHERE task_id = ?", (new, old))
    store.conn.commit()
