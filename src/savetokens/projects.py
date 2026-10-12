"""Projects: the same project on every machine, without its name leaving any of them.

A folder's project is, walking up from it:
  1. a `.savetokens` file ({"id": ..., "name": ...}) in it or any folder above: what you set with
     `savetokens project link` (above a few repos, it makes them one project)
  2. the git repository's remote (origin, else the first): `github.com/org/repo` on every machine,
     however it was cloned (ssh or https, with or without .git)
  3. the git repository's folder, or failing that the folder itself: this machine only

What's stored and synced is an id: HMAC(your project key, the identity), so the hub can add up a
project across machines without learning its name. The key is your hub's (fetched when a machine
connects), or this machine's own when it works alone. Names stay on each machine.
"""
from __future__ import annotations

import configparser
import hashlib
import hmac
import json
import re
import secrets
from pathlib import Path

MARKER = ".savetokens"
_cache: dict = {}


def normalize_remote(url: str) -> str:
    """'git@github.com:Org/Repo.git', 'https://user@github.com/org/repo/' -> 'github.com/org/repo'."""
    u = url.strip()
    u = re.sub(r"^[a-z+]+://", "", u)            # scheme
    u = re.sub(r"^[^@/]+@", "", u)               # user@
    u = re.sub(r"^([^/:]+):(?!\d+/)", r"\1/", u)  # scp-style host:path (not host:port/)
    u = re.sub(r":\d+/", "/", u)                 # a port
    u = re.sub(r"\.git/?$", "", u).rstrip("/")
    return u.lower()


def _git_dir(root: Path) -> Path | None:
    g = root / ".git"
    if g.is_dir():
        return g
    if g.is_file():   # a worktree or submodule: "gitdir: <path>"
        m = re.match(r"gitdir:\s*(.+)", g.read_text().strip())
        if m:
            d = (root / m.group(1)).resolve()
            common = d / "commondir"
            return (d / common.read_text().strip()).resolve() if common.is_file() else d
    return None


def _remote(git_dir: Path) -> str | None:
    cp = configparser.RawConfigParser(strict=False)
    try:
        cp.read(git_dir / "config")
    except configparser.Error:
        return None
    remotes = [s for s in cp.sections() if s.startswith('remote "')]
    remotes.sort(key=lambda s: s != 'remote "origin"')
    for s in remotes:
        if cp.has_option(s, "url"):
            return cp.get(s, "url")
    return None


def identity(cwd: str | None) -> tuple[str, str, str] | None:
    """(kind, value, name) for a folder: kind is 'file', 'git' or 'path' (this machine only)."""
    if not cwd:
        return None
    start = Path(cwd)
    ups = [start, *start.parents]
    for d in ups:   # a marker anywhere above wins: it can group several repos into one project
        try:
            marker = d / MARKER
            if marker.is_file():
                m = json.loads(marker.read_text())
                if m.get("id"):
                    return "file", str(m["id"]), m.get("name") or d.name
        except (OSError, ValueError):
            continue
    for d in ups:
        try:
            git = _git_dir(d)
        except (OSError, ValueError):
            continue
        if git:
            url = _remote(git)
            if url:
                norm = normalize_remote(url)
                return "git", norm, norm.rsplit("/", 1)[-1]
            return "path", str(d), d.name
    return "path", str(start), start.name or str(start)


def key(store) -> str:
    k = store.meta("project_key")
    if not k:
        k = secrets.token_hex(16)
        store.set_meta("project_key", k)
    return k


def project_id(store, ident) -> str:
    kind, value, _ = ident
    if kind == "path":   # a folder is only the same project on the same machine
        value = f"{store.machine}:{value}"
    return hmac.new(key(store).encode(), f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()[:16]


def resolve(store, cwd) -> tuple[str, str] | tuple[None, None]:
    """(project id, name) for a folder, remembered per folder."""
    if not cwd:
        return None, None
    k = (id(store), cwd)
    if k in _cache:
        return _cache[k]
    row = store.conn.execute("SELECT id, name FROM projects WHERE cwd = ?", (cwd,)).fetchone()
    if row:
        _cache[k] = (row[0], row[1])
        return _cache[k]
    ident = identity(cwd)
    pid, name = project_id(store, ident), ident[2]
    store.conn.execute("INSERT OR REPLACE INTO projects (cwd, id, name, kind) VALUES (?,?,?,?)", (cwd, pid, name, ident[0]))
    _cache[k] = (pid, name)
    return pid, name


def fill(store) -> int:
    """Give every request with a folder its project id (rows read before projects, or before a key change)."""
    n = 0
    for (cwd,) in store.conn.execute("SELECT DISTINCT cwd FROM usage WHERE project_id IS NULL AND cwd IS NOT NULL"
                                     ).fetchall():
        pid, _ = resolve(store, cwd)
        n += store.conn.execute("UPDATE usage SET project_id = ? WHERE cwd = ? AND project_id IS NULL",
                                (pid, cwd)).rowcount
    store.conn.commit()
    return n


def set_key(store, new: str) -> bool:
    """Use the hub's project key, so every machine names a project alike. Recomputes ids already given."""
    if not new or new == store.meta("project_key"):
        return False
    before = dict(store.conn.execute("SELECT cwd, id FROM projects").fetchall())
    store.set_meta("project_key", new)
    store.conn.execute("DELETE FROM projects")
    store.conn.execute("UPDATE usage SET project_id = NULL WHERE cwd IS NOT NULL")
    _cache.clear()
    fill(store)
    after = dict(store.conn.execute("SELECT cwd, id FROM projects").fetchall())
    from . import tasks
    tasks.rekey(store, {old: after[cwd] for cwd, old in before.items() if cwd in after})
    return True


def names(store) -> dict:
    """{project id: name} known on this machine, your own names (`project link`) first. On a hub: the names
    its machines chose to send (`savetokens projects --share-names on`)."""
    out = dict((store.meta("shared_names") or {}).get("projects") or {})
    out.update({r[0]: r[1] for r in store.conn.execute("SELECT id, name FROM projects")})
    out.update(store.meta("project_names") or {})
    return out


def task_names(store) -> dict:
    """{task id: {"name", "project_id", "source", "done"}}: this machine's tasks, else those sent to the hub."""
    out = dict((store.meta("shared_names") or {}).get("tasks") or {})
    for r in store.conn.execute("SELECT id, project_id, name, source, done FROM tasks"):
        out[r[0]] = {"name": r[2], "project_id": r[1], "source": r[3], "done": r[4]}
    return out


def shareable(store) -> dict:
    """What `--share-names on` sends to your own hub: project and task names, nothing else."""
    return {"projects": {k: v for k, v in names(store).items() if k and v},
            "tasks": {k: v for k, v in task_names(store).items() if k}}


# ── where a project's tokens went: tool, provider, and plan or API ───────────

def split(store, col, since, until=None) -> dict:
    """{id: {"tokens", "requests", "value_usd", "api_usd", "by": [...]}} for col (project_id or task_id).
    value_usd: every request at API prices (what it would have cost pay-as-you-go). api_usd: only what was
    billed pay-as-you-go: real money. by: one entry per tool, provider and billing ("plan" or "api")."""
    from . import pools, spend
    var, fixed = spend._variable(pools.plans(store))
    out = {}
    for r in store.conn.execute(
            f"SELECT {col}, harness, {spend.PROVIDER}, CASE WHEN {var} THEN 'api' ELSE 'plan' END,"
            f" SUM({spend.TOKENS}), COALESCE(SUM(cost_usd), 0), COUNT(*) FROM usage WHERE {col} IS NOT NULL"
            f" AND ts >= ? AND ts <= ? GROUP BY 1, 2, 3, 4", fixed + [since, until or float("inf")]):
        t = out.setdefault(r[0], {"tokens": 0, "requests": 0, "value_usd": 0.0, "api_usd": 0.0, "by": []})
        t["tokens"] += r[4] or 0
        t["requests"] += r[6]
        t["value_usd"] += r[5]
        if r[3] == "api":
            t["api_usd"] += r[5]
        t["by"].append({"tool": r[1], "provider": r[2], "billing": r[3], "tokens": r[4] or 0, "value_usd": r[5],
                        "requests": r[6]})
    for t in out.values():
        t["by"].sort(key=lambda b: -b["tokens"])
    return out


def report(store, since, until=None, tasks_each=10) -> list[dict]:
    """Every project with usage since `since`, biggest first: its split, and its tasks with theirs."""
    nm, tn = names(store), task_names(store)
    tasks = split(store, "task_id", since, until)
    by_project = {}
    for tid, t in tasks.items():
        info = tn.get(tid) or {}
        pid = info.get("project_id")
        if not pid:   # a task named nowhere this side: its requests say which project
            row = store.conn.execute("SELECT project_id FROM usage WHERE task_id = ? AND project_id IS NOT NULL"
                                     " LIMIT 1", (tid,)).fetchone()
            pid = row[0] if row else None
        by_project.setdefault(pid, []).append({"id": tid, "name": info.get("name"), "source": info.get("source"),
                                               "done": info.get("done"), **t})
    out = []
    for pid, t in sorted(split(store, "project_id", since, until).items(), key=lambda kv: -kv[1]["tokens"]):
        ts = sorted(by_project.get(pid, []), key=lambda x: -x["tokens"])
        out.append({"id": pid, "name": nm.get(pid), **t, "tasks": ts[:tasks_each], "more_tasks": max(0, len(ts) - tasks_each)})
    return out


def by_name(store, name) -> list[str]:
    """Project ids with this name (a name you gave, a repo's or a folder's); '.' is the current folder's."""
    import os
    if name == ".":
        pid, _ = resolve(store, os.getcwd())
        return [pid]
    return [pid for pid, n in names(store).items() if n == name]


def link(folder: Path, name: str | None, project_id_: str | None = None) -> dict:
    """Write a `.savetokens` marker: this folder (and everything under it) is one project, on any machine
    that has the file. Commit it to share the project with the repo."""
    marker = folder / MARKER
    data = json.loads(marker.read_text()) if marker.is_file() else {}
    data["id"] = project_id_ or data.get("id") or secrets.token_hex(8)
    if name:
        data["name"] = name
    marker.write_text(json.dumps(data, indent=2) + "\n")
    _cache.clear()
    return data
