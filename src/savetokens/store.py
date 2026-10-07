"""Local SQLite store of usage and tool events (schema v4: v1 plus day_forecasts, sessions, calibration,
account columns, forecast paths, window forecasts, compactions).

Counts and metadata only: never prompts, replies, tool output or file contents.
A tool's target (a file path) is kept so repeated reads can be named; commands
are kept only as a hash.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field, fields
from pathlib import Path

SCHEMA_VERSION = 6
SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY,
    harness TEXT NOT NULL,
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    ts REAL NOT NULL,
    model TEXT,
    project TEXT,
    subagent INTEGER NOT NULL DEFAULT 0,
    input INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    cache_write_5m INTEGER NOT NULL DEFAULT 0,
    cache_write_1h INTEGER NOT NULL DEFAULT 0,
    output INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    cost_source TEXT,
    provider TEXT,
    UNIQUE (harness, request_id)
);
CREATE INDEX IF NOT EXISTS usage_ts ON usage(ts);
CREATE INDEX IF NOT EXISTS usage_session ON usage(session_id, ts);

CREATE TABLE IF NOT EXISTS tools (
    id INTEGER PRIMARY KEY,
    harness TEXT NOT NULL,
    session_id TEXT NOT NULL,
    tool_use_id TEXT,
    ts REAL NOT NULL,
    tool TEXT NOT NULL,
    kind TEXT NOT NULL,
    args_hash TEXT,
    target TEXT,
    ok INTEGER,
    output_chars INTEGER,
    UNIQUE (harness, tool_use_id)
);
CREATE INDEX IF NOT EXISTS tools_session ON tools(session_id, ts);

CREATE TABLE IF NOT EXISTS limits (
    ts REAL NOT NULL,
    harness TEXT NOT NULL,
    session_id TEXT,
    five_hour_pct REAL, five_hour_resets REAL,
    seven_day_pct REAL, seven_day_resets REAL,
    spend_pct REAL, spend_resets REAL,
    context_pct REAL
);
CREATE INDEX IF NOT EXISTS limits_ts ON limits(ts);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    harness TEXT NOT NULL,
    session_id TEXT NOT NULL,
    ts REAL NOT NULL,
    rule TEXT NOT NULL,
    key TEXT NOT NULL,
    action TEXT NOT NULL,
    message TEXT NOT NULL,
    burn_usd_per_min REAL,
    UNIQUE (session_id, rule, key)
);

CREATE TABLE IF NOT EXISTS forecasts (
    day TEXT NOT NULL,
    made_at REAL NOT NULL,
    p10 REAL, p50 REAL, p90 REAL,
    PRIMARY KEY (day, made_at)
);

CREATE TABLE IF NOT EXISTS day_forecasts (
    source TEXT NOT NULL,
    day TEXT NOT NULL,
    made_at REAL NOT NULL,
    p10 REAL, p50 REAL, p90 REAL,
    PRIMARY KEY (source, day, made_at)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    harness TEXT NOT NULL,
    billing TEXT NOT NULL,          -- subscription | api | unknown
    plan TEXT, tier TEXT,
    model TEXT,                     -- latest model the harness reported for the session
    source TEXT NOT NULL,           -- statusline | account | hermes
    first_seen REAL, last_seen REAL
);

CREATE TABLE IF NOT EXISTS calibration (
    ts REAL NOT NULL,
    window TEXT NOT NULL,           -- five_hour | seven_day
    tier TEXT,
    model TEXT NOT NULL,            -- '*' is the pooled rate
    pct_per_usd REAL NOT NULL,
    n INTEGER NOT NULL,
    r2 REAL
);
CREATE INDEX IF NOT EXISTS calibration_ts ON calibration(window, model, ts);

CREATE TABLE IF NOT EXISTS forecast_paths (
    source TEXT NOT NULL,           -- ephemeris | baseline
    unit TEXT NOT NULL,             -- sub_usd | api_usd: API-equivalent dollars per hour
    made_at REAL NOT NULL,
    start_hour REAL NOT NULL,       -- epoch of the first forecast hour
    hours INTEGER NOT NULL,
    n INTEGER NOT NULL,
    data BLOB NOT NULL,             -- n paths x hours doubles, path-major
    PRIMARY KEY (source, unit)
);

CREATE TABLE IF NOT EXISTS window_forecasts (
    source TEXT NOT NULL,
    unit TEXT NOT NULL,
    kind TEXT NOT NULL,             -- hour | five_hour | day | week
    window_start REAL NOT NULL,
    window_end REAL NOT NULL,
    made_at REAL NOT NULL,
    so_far REAL NOT NULL,           -- local usage already in the window when the forecast was made
    p10 REAL, p50 REAL, p90 REAL,   -- forecast window total, same unit
    PRIMARY KEY (source, unit, kind, window_end, made_at)
);

CREATE TABLE IF NOT EXISTS compactions (
    harness TEXT NOT NULL,
    session_id TEXT NOT NULL,
    ts REAL NOT NULL,
    trigger TEXT,                   -- auto | manual
    pre_tokens INTEGER,             -- context size when it fired
    UNIQUE (session_id, ts)
);

CREATE TABLE IF NOT EXISTS offsets (
    path TEXT PRIMARY KEY,
    offset INTEGER NOT NULL,
    size INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fixes (
    fix_id TEXT NOT NULL,
    target TEXT NOT NULL,
    applied_at REAL NOT NULL,
    reverted_at REAL,
    backup_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def home() -> Path:
    return Path(os.environ.get("SAVETOKENS_HOME") or Path.home() / ".savetokens")


@dataclass
class UsageEvent:
    harness: str
    session_id: str
    request_id: str
    ts: float
    model: str | None = None
    project: str | None = None
    subagent: bool = False
    input: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    output: int = 0
    cost_usd: float | None = None
    cost_source: str | None = None
    provider: str | None = None     # who bills it: "anthropic", "openrouter", "api.engy.ai", ...

    @property
    def context_tokens(self) -> int:
        """Prompt size of this request: what the model re-read."""
        return self.input + self.cache_read + self.cache_write_5m + self.cache_write_1h


@dataclass
class ToolEvent:
    harness: str
    session_id: str
    ts: float
    tool: str
    kind: str                    # read | edit | test | bash | search | agent | other
    tool_use_id: str | None = None
    args_hash: str | None = None
    target: str | None = None
    ok: bool | None = None
    output_chars: int | None = None


_USAGE_COLS = [f.name for f in fields(UsageEvent)]
_TOOL_COLS = [f.name for f in fields(ToolEvent)]


class Store:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else home() / "events.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=1.0)
        self.conn.row_factory = sqlite3.Row
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            for table, col in (("sessions", "account"), ("limits", "account"), ("usage", "provider")):
                cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                if col not in cols:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
            if 0 < version < 5:   # v5 parses compactions: re-read transcripts once (rows are de-duplicated)
                self.conn.execute("DELETE FROM offsets")
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.conn.commit()
        self.close()

    # ── writes ──────────────────────────────────────────────────────────────
    def add_usage(self, events) -> int:
        rows = [tuple(int(v) if isinstance(v, bool) else v for v in (getattr(e, c) for c in _USAGE_COLS))
                for e in events]
        cur = self.conn.executemany(
            f"INSERT OR IGNORE INTO usage ({','.join(_USAGE_COLS)}) VALUES ({','.join('?' * len(_USAGE_COLS))})",
            rows)
        self.conn.commit()
        return cur.rowcount

    def add_tools(self, events) -> None:
        """Insert tool events; a later sighting of the same tool_use_id fills in outcome and size."""
        for e in events:
            row = {c: getattr(e, c) for c in _TOOL_COLS}
            if isinstance(row["ok"], bool):
                row["ok"] = int(row["ok"])
            cur = self.conn.execute(
                f"INSERT OR IGNORE INTO tools ({','.join(_TOOL_COLS)}) VALUES ({','.join('?' * len(_TOOL_COLS))})",
                [row[c] for c in _TOOL_COLS])
            if cur.rowcount == 0 and e.tool_use_id:
                self.conn.execute(
                    "UPDATE tools SET ok = COALESCE(?, ok), output_chars = COALESCE(?, output_chars)"
                    " WHERE harness = ? AND tool_use_id = ?",
                    (row["ok"], row["output_chars"], e.harness, e.tool_use_id))
        self.conn.commit()

    def add_limits(self, harness, session_id, *, ts=None, **values):
        cols = ["ts", "harness", "session_id", *values]
        self.conn.execute(f"INSERT INTO limits ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                          [ts or time.time(), harness, session_id, *values.values()])
        self.conn.commit()

    def add_alert(self, harness, session_id, rule, key, action, message, burn=None, ts=None) -> bool:
        """Record an alert; False if this (session, rule, key) already fired."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO alerts (harness, session_id, ts, rule, key, action, message, burn_usd_per_min)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (harness, session_id, ts or time.time(), rule, key, action, message, burn))
        self.conn.commit()
        return cur.rowcount == 1

    def get_offset(self, path):
        row = self.conn.execute("SELECT offset, size FROM offsets WHERE path = ?", (str(path),)).fetchone()
        return (row["offset"], row["size"]) if row else (0, 0)

    def set_offset(self, path, offset, size):
        self.conn.execute("INSERT OR REPLACE INTO offsets (path, offset, size) VALUES (?,?,?)",
                          (str(path), offset, size))

    def meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))
        self.conn.commit()

    # ── reads ───────────────────────────────────────────────────────────────
    def usage(self, since=None, until=None, session_id=None, harness=None):
        q, p = "SELECT * FROM usage WHERE 1=1", []
        for col, op, val in (("ts", ">=", since), ("ts", "<", until), ("session_id", "=", session_id),
                             ("harness", "=", harness)):
            if val is not None:
                q += f" AND {col} {op} ?"
                p.append(val)
        return [UsageEvent(**{c: (bool(r[c]) if c == "subagent" else r[c]) for c in _USAGE_COLS})
                for r in self.conn.execute(q + " ORDER BY ts", p)]

    def tools(self, session_id=None, since=None, limit=None):
        q, p = "SELECT * FROM tools WHERE 1=1", []
        if session_id is not None:
            q += " AND session_id = ?"
            p.append(session_id)
        if since is not None:
            q += " AND ts >= ?"
            p.append(since)
        q += " ORDER BY ts DESC, id DESC"
        if limit:
            q += f" LIMIT {int(limit)}"
        rows = list(self.conn.execute(q, p))[::-1]
        return [ToolEvent(**{c: (None if r[c] is None else bool(r[c])) if c == "ok" else r[c]
                             for c in _TOOL_COLS}) for r in rows]

    def latest_limits(self, harness=None):
        q = "SELECT * FROM limits" + (" WHERE harness = ?" if harness else "") + " ORDER BY ts DESC LIMIT 1"
        return self.conn.execute(q, (harness,) if harness else ()).fetchone()

    def limits_since(self, since):
        return list(self.conn.execute("SELECT * FROM limits WHERE ts >= ? ORDER BY ts", (since,)))

    def alerts(self, since=None):
        return list(self.conn.execute("SELECT * FROM alerts WHERE ts >= ? ORDER BY ts", (since or 0,)))


def load_config() -> dict:
    defaults = {
        "block": False,           # opt-in: deny the next looping tool call
        "burn_window_min": 5,
        "burn_multiple": 3.0,
        "burn_floor_usd": 1.0,
        "loop_repeats": 4,
        "reread_limit": 4,
        "failing_tests": 3,
        "context_tokens": 200_000,
        "forecaster": "ephemeris",   # the default: forecasts from the Ephemeris ensemble ("baseline" = local only)
        "mode": "auto",              # the quality knob; auto steers only when a limit is at risk
    }
    try:
        defaults.update(json.loads((home() / "config.json").read_text()))
    except (OSError, ValueError):
        pass
    return defaults


def save_config(cfg: dict):
    home().mkdir(parents=True, exist_ok=True)
    (home() / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
