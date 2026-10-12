"""Local SQLite store. The same schema backs a machine and the sync server (one database per user).

Counts and metadata only: never prompts, replies, tool output or file contents.

  usage      one row per model request (Claude Code or Codex): tokens, dollars, subscription or API
  meter      Claude's own limit readings (% used, reset time) per account and limit
  hits       limit errors seen in transcripts: the times you actually ran out
  paths      the latest forecast sample paths of hourly demand
  outlook    every projection made for a limit window, kept so it can be scored and watched
  alerts     alerts raised, one per (account, limit, window, stage)
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, fields
from pathlib import Path

SCHEMA_VERSION = 8   # v2: usage.project (kept on the machine, never synced); v3: usage.billing; v4: usage.provider;
                     # v5: usage.reasoning, usage.thread, usage.cwd (the cwd stays on the machine);
                     # v6: usage.project_id (synced: an id, not a name) and the projects table (this machine's);
                     # v7: usage.branch (this machine's), usage.task_id (synced) and the tasks table;
                     # v8: usage.person (on a team's hub: whose usage it is)
SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    machine TEXT NOT NULL,
    harness TEXT NOT NULL,
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    ts REAL NOT NULL,
    model TEXT,
    subagent INTEGER NOT NULL DEFAULT 0,
    input INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    cache_write_5m INTEGER NOT NULL DEFAULT 0,
    cache_write_1h INTEGER NOT NULL DEFAULT 0,
    output INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    account TEXT,
    project TEXT,                   -- the session's folder name; this machine only, never synced
    billing TEXT,                   -- 'api' when paid by API key; NULL for a subscription
    provider TEXT,                  -- who serves it: anthropic, openai, openrouter, nous, an API host, ...
    reasoning INTEGER,              -- of the output, tokens spent reasoning; NULL when the source doesn't say
    thread TEXT,                    -- one conversation's requests, in order (a session, or one subagent in it);
                                    -- NULL when the source gives totals per session, not per request
    cwd TEXT,                       -- the folder it ran in; this machine only, never synced
    project_id TEXT,                -- the project, the same on every machine (projects.py); its name isn't synced
    branch TEXT,                    -- the git branch it ran on, when the tool records it; never synced
    task_id TEXT,                   -- the task (tasks.py): an id, the same on every machine; its name isn't synced
    person TEXT,                    -- on a team's hub: the member it came from
    UNIQUE (machine, harness, request_id)
);
CREATE INDEX IF NOT EXISTS usage_ts ON usage(ts);
CREATE INDEX IF NOT EXISTS usage_session ON usage(session_id, ts);

CREATE TABLE IF NOT EXISTS meter (
    machine TEXT NOT NULL,
    harness TEXT NOT NULL,
    account TEXT,
    ts REAL NOT NULL,
    name TEXT NOT NULL,             -- five_hour | seven_day | any other limit Claude reports
    pct REAL NOT NULL,
    resets REAL,
    UNIQUE (machine, account, name, ts)
);
CREATE INDEX IF NOT EXISTS meter_ts ON meter(name, ts);

CREATE TABLE IF NOT EXISTS hits (
    machine TEXT NOT NULL,
    harness TEXT NOT NULL,
    account TEXT,
    session_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,             -- session (5-hour) | weekly | model | other
    model TEXT,                     -- for a model limit: which model
    UNIQUE (machine, session_id, ts)
);

CREATE TABLE IF NOT EXISTS paths (
    account TEXT NOT NULL,
    source TEXT NOT NULL,           -- ephemeris | baseline
    made_at REAL NOT NULL,
    start_hour REAL NOT NULL,
    hours INTEGER NOT NULL,
    n INTEGER NOT NULL,
    data BLOB NOT NULL,             -- n paths x hours doubles, path-major: % of the weekly limit per hour
    PRIMARY KEY (account, source)
);

CREATE TABLE IF NOT EXISTS outlook (
    account TEXT NOT NULL,
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    made_at REAL NOT NULL,
    window_end REAL NOT NULL,
    used REAL NOT NULL,             -- % used when the projection was made
    p10 REAL, p50 REAL, p90 REAL,   -- projected % at the window's end
    p_hit REAL,                     -- chance of reaching 100% before the window ends
    eta REAL,                       -- median time of reaching 100%, if p_hit >= 0.5
    PRIMARY KEY (account, name, source, made_at)
);

CREATE TABLE IF NOT EXISTS alerts (
    account TEXT NOT NULL,
    ts REAL NOT NULL,
    name TEXT NOT NULL,
    window_end REAL NOT NULL,
    stage TEXT NOT NULL,
    message TEXT NOT NULL,
    seen INTEGER NOT NULL DEFAULT 0,  -- shown in a session already
    UNIQUE (account, name, window_end, stage)
);

CREATE TABLE IF NOT EXISTS projects (   -- this machine's folders: their project id and name (never synced)
    cwd TEXT PRIMARY KEY,
    id TEXT NOT NULL,
    name TEXT,
    kind TEXT                       -- file | git | path
);

CREATE TABLE IF NOT EXISTS tasks (      -- this machine's names for tasks (never synced)
    id TEXT PRIMARY KEY,
    project_id TEXT,
    name TEXT NOT NULL,
    source TEXT NOT NULL,           -- declared | tracker | branch
    started REAL,                   -- declared: when it was (last) started
    stopped REAL,                   -- declared: when another task was started instead
    done REAL                       -- when it was marked done
);
CREATE INDEX IF NOT EXISTS tasks_project ON tasks(project_id);

CREATE TABLE IF NOT EXISTS offsets (path TEXT PRIMARY KEY, offset INTEGER NOT NULL, size INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def home() -> Path:
    return Path(os.environ.get("SAVETOKENS_HOME") or Path.home() / ".savetokens")


@dataclass
class Usage:
    harness: str
    session_id: str
    request_id: str
    ts: float
    model: str | None = None
    subagent: bool = False
    input: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    output: int = 0
    cost_usd: float | None = None
    account: str | None = None
    machine: str = ""
    project: str | None = None
    billing: str | None = None    # "api" when paid by API key
    provider: str | None = None   # who serves it (default: the tool's own company)
    reasoning: int | None = None  # of the output, tokens spent reasoning (None: not reported)
    thread: str | None = None     # the conversation it belongs to (None: the source has per-session totals only)
    cwd: str | None = None        # the folder it ran in (never synced)
    project_id: str | None = None # the project, from the folder (projects.py)
    branch: str | None = None     # the git branch, when the tool records it (never synced)
    task_id: str | None = None    # the task (tasks.py)
    person: str | None = None     # on a team's hub: the member it came from


USAGE_COLS = [f.name for f in fields(Usage)]
SYNCED_USAGE = [c for c in USAGE_COLS if c not in ("project", "cwd", "branch", "person")]   # names stay local
NEW_USAGE = ["reasoning", "thread", "project_id", "task_id"]   # not known to hubs older than v5: pushed without them there
METER_COLS = ["machine", "harness", "account", "ts", "name", "pct", "resets"]
HIT_COLS = ["machine", "harness", "account", "session_id", "ts", "kind", "model"]
SYNCED = {"usage": SYNCED_USAGE, "meter": METER_COLS, "hits": HIT_COLS}


DEFAULT_PROVIDER = {"claude-code": "anthropic", "codex": "openai"}


def _fill_providers(conn):
    """Rows captured before v4: the tool's own company, or for Hermes the provider in the request id."""
    from .hermes import provider_name
    for h, p in DEFAULT_PROVIDER.items():
        conn.execute("UPDATE usage SET provider = ? WHERE harness = ? AND provider IS NULL", (p, h))
    rows = conn.execute("SELECT rowid, request_id FROM usage WHERE harness = 'hermes' AND provider IS NULL").fetchall()
    conn.executemany("UPDATE usage SET provider = ? WHERE rowid = ?",
                     [(provider_name(*(r[1].split("|") + ["", "", ""])[2:4]), r[0]) for r in rows])


class Store:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else home() / "savetokens.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=2.0)
        self.conn.row_factory = sqlite3.Row
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            # by the columns there, not the version: an older savetokens on the same machine sets it back
            have = {r[1] for r in self.conn.execute("PRAGMA table_info(usage)")}
            if "project" not in have:   # add the project, and read transcripts again to fill it in
                self.conn.execute("ALTER TABLE usage ADD COLUMN project TEXT")
                self.conn.execute("CREATE INDEX IF NOT EXISTS usage_session ON usage(session_id, ts)")
                self.conn.execute("DELETE FROM offsets")
            if "billing" not in have:
                self.conn.execute("ALTER TABLE usage ADD COLUMN billing TEXT")
            if "provider" not in have:
                self.conn.execute("ALTER TABLE usage ADD COLUMN provider TEXT")
            if "project_id" not in have:
                self.conn.execute("ALTER TABLE usage ADD COLUMN project_id TEXT")
            for c in ("branch", "task_id", "person"):
                if c not in have:
                    self.conn.execute(f"ALTER TABLE usage ADD COLUMN {c} TEXT")
            if "branch" not in have:   # read Claude Code and Codex again for their branches
                self.conn.execute("DELETE FROM offsets")
                self.conn.execute("DELETE FROM meta WHERE key LIKE 'codex_file:%'")
            new = [c for c in ("reasoning", "thread", "cwd") if c not in have]
            for c in new:
                self.conn.execute(f"ALTER TABLE usage ADD COLUMN {c} {'INTEGER' if c == 'reasoning' else 'TEXT'}")
            if new:   # read every source again to fill them in (rows already there are updated, not doubled)
                self.conn.execute("DELETE FROM offsets")
                self.conn.execute("DELETE FROM meta WHERE key LIKE 'codex_file:%'")
                # not Hermes: its request ids come from running totals, so reading it again would double it
            if version < SCHEMA_VERSION:
                _fill_providers(self.conn)   # also rows an older version wrote meanwhile
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.conn.commit()
        self.close()

    @property
    def machine(self) -> str:
        m = self.meta("machine")
        if not m:
            m = uuid.uuid4().hex[:12]
            self.set_meta("machine", m)
        return m

    # ── writes ──────────────────────────────────────────────────────────────
    def insert(self, table, cols, rows) -> int:
        cur = self.conn.executemany(
            f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", rows)
        self.conn.commit()
        return cur.rowcount

    def upsert_usage(self, cols, rows) -> int:
        """Rows from a machine (on the hub): new ones added, and ones seen before given any field they lacked
        (a machine sends its history again once it knows its requests' projects)."""
        fill = [c for c in ("project_id", "reasoning", "thread", "task_id", "provider", "billing") if c in cols]
        if not fill:
            return self.insert("usage", cols, rows)
        before = self.conn.total_changes
        # ids, provider and billing follow the machine (it learns them later: its project key, which endpoint
        # a session ran on); the rest only fill gaps
        self.conn.executemany(
            f"INSERT INTO usage ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
            " ON CONFLICT (machine, harness, request_id) DO UPDATE SET "
            + ", ".join(f"{c} = COALESCE(excluded.{c}, usage.{c})" if c in ("project_id", "task_id", "provider", "billing")
                        else f"{c} = COALESCE(usage.{c}, excluded.{c})" for c in fill), rows)
        self.conn.commit()
        return self.conn.total_changes - before

    def relabel(self, harness, session_id, provider) -> int:
        """A session found to have run on another provider than its tool's own: its requests put under it and
        billed as API (not the plan), and sent to the hub again."""
        default = DEFAULT_PROVIDER.get(harness)
        rows = self.conn.execute("SELECT rowid FROM usage WHERE harness = ? AND session_id = ? AND machine = ?"
                                 " AND (provider IS NULL OR provider = ?)",
                                 (harness, session_id, self.machine, default)).fetchall()
        if not rows:
            return 0
        self.conn.execute("UPDATE usage SET provider = ?, billing = 'api' WHERE harness = ? AND session_id = ?"
                          " AND machine = ? AND (provider IS NULL OR provider = ?)",
                          (provider, harness, session_id, self.machine, default))
        first = min(r[0] for r in rows) - 1
        if (self.meta("pushed:usage") or 0) > first:
            self.set_meta("pushed:usage", first)
        self.conn.commit()
        return len(rows)

    def add_usage(self, events) -> int:
        events = list(events)
        machine = self.machine
        rows = []
        from . import projects
        for e in events:
            e.machine = e.machine or machine
            e.provider = e.provider or DEFAULT_PROVIDER.get(e.harness)
            if e.cwd and not e.project_id:
                e.project_id = projects.resolve(self, e.cwd)[0]
            if e.project_id and not e.task_id:
                from . import tasks
                e.task_id = tasks.attribute(self, e)
            rows.append([int(v) if isinstance(v, bool) else v for v in (getattr(e, c) for c in USAGE_COLS)])
        n = self.insert("usage", USAGE_COLS, rows)
        # rows read before a column was kept get it on the next read
        self.conn.executemany(
            "UPDATE usage SET project = COALESCE(project, ?), reasoning = COALESCE(reasoning, ?),"
            " thread = COALESCE(thread, ?), cwd = COALESCE(cwd, ?), project_id = COALESCE(project_id, ?),"
            " branch = COALESCE(branch, ?), task_id = COALESCE(task_id, ?)"
            " WHERE machine = ? AND harness = ? AND request_id = ?",
            [(e.project, e.reasoning, e.thread, e.cwd, e.project_id, e.branch, e.task_id, e.machine, e.harness,
              e.request_id) for e in events if e.project or e.reasoning is not None or e.thread or e.cwd])
        self.conn.commit()
        return n

    def add_meter(self, harness, account, readings, ts=None) -> int:
        """readings: {name: (pct, resets)}."""
        ts = ts or time.time()
        rows = [(self.machine, harness, account, ts, name, pct, resets)
                for name, (pct, resets) in readings.items() if pct is not None]
        return self.insert("meter", METER_COLS, rows)

    def add_hits(self, hits) -> int:
        """hits: [(harness, account, session_id, ts, kind, model)]."""
        return self.insert("hits", HIT_COLS, [(self.machine, *h) for h in hits])

    def get_offset(self, path):
        row = self.conn.execute("SELECT offset, size FROM offsets WHERE path = ?", (str(path),)).fetchone()
        return (row["offset"], row["size"]) if row else (0, 0)

    def set_offset(self, path, offset, size):
        self.conn.execute("INSERT OR REPLACE INTO offsets (path, offset, size) VALUES (?,?,?)", (str(path), offset, size))

    def meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))
        self.conn.commit()

    # ── reads ───────────────────────────────────────────────────────────────
    def usage(self, since=None, until=None):
        q, p = "SELECT * FROM usage WHERE 1=1", []
        if since is not None:
            q += " AND ts >= ?"
            p.append(since)
        if until is not None:
            q += " AND ts < ?"
            p.append(until)
        return [Usage(**{c: (bool(r[c]) if c == "subagent" else r[c]) for c in USAGE_COLS})
                for r in self.conn.execute(q + " ORDER BY ts", p)]

    def last_activity(self):
        """Time of the latest usage or meter reading from any machine."""
        row = self.conn.execute("SELECT MAX(t) FROM (SELECT MAX(ts) AS t FROM usage UNION ALL"
                                " SELECT MAX(ts) FROM meter)").fetchone()
        return row[0]


def load_config() -> dict:
    defaults = {
        "forecaster": "ephemeris",   # the default; "baseline" keeps everything on this machine
    }
    try:
        defaults.update(json.loads((home() / "config.json").read_text()))
    except (OSError, ValueError):
        pass
    return defaults


def save_config(cfg: dict):
    home().mkdir(parents=True, exist_ok=True)
    path = home() / "config.json"
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    path.chmod(0o600)   # may hold the sync token
