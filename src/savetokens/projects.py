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
    """{project id: name} known on this machine, your own names (`project link`) first."""
    out = {r[0]: r[1] for r in store.conn.execute("SELECT id, name FROM projects")}
    out.update(store.meta("project_names") or {})
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
