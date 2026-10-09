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

SCHEMA_VERSION = 3   # v2: usage.project (kept on the machine, never synced); v3: usage.billing
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


USAGE_COLS = [f.name for f in fields(Usage)]
SYNCED_USAGE = [c for c in USAGE_COLS if c != "project"]   # folder names stay on the machine
METER_COLS = ["machine", "harness", "account", "ts", "name", "pct", "resets"]
HIT_COLS = ["machine", "harness", "account", "session_id", "ts", "kind", "model"]
SYNCED = {"usage": SYNCED_USAGE, "meter": METER_COLS, "hits": HIT_COLS}


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
            if version == 1:   # add the project, and read transcripts again to fill it in
                self.conn.execute("ALTER TABLE usage ADD COLUMN project TEXT")
                self.conn.execute("CREATE INDEX IF NOT EXISTS usage_session ON usage(session_id, ts)")
                self.conn.execute("DELETE FROM offsets")
            if version in (1, 2):
                self.conn.execute("ALTER TABLE usage ADD COLUMN billing TEXT")
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

    def add_usage(self, events) -> int:
        events = list(events)
        machine = self.machine
        rows = []
        for e in events:
            e.machine = e.machine or machine
            rows.append([int(v) if isinstance(v, bool) else v for v in (getattr(e, c) for c in USAGE_COLS)])
        n = self.insert("usage", USAGE_COLS, rows)
        # rows read before projects were kept get theirs on the next read
        self.conn.executemany("UPDATE usage SET project = ? WHERE machine = ? AND harness = ? AND request_id = ?"
                              " AND project IS NULL", [(e.project, e.machine, e.harness, e.request_id)
                                                       for e in events if e.project])
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
