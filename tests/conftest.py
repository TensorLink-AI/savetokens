"""Shared fixtures: isolated homes, a Claude Code transcript writer and a Hermes state.db."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from savetokens.store import Store

T0 = 1_790_000_000.0   # fixed clock for deterministic tests


@pytest.fixture(autouse=True)
def homes(tmp_path, monkeypatch):
    # never touch the real crontab, PATH tools or background processes from tests
    from savetokens import maintain, schedule
    monkeypatch.setattr(schedule, "available", lambda: False)
    monkeypatch.setattr(maintain, "kick", lambda *a, **k: None)
    monkeypatch.setattr("shutil.which", lambda name, *a, **k: None if name == "hermes" else f"/usr/bin/{name}")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for name in ("EPHEMERIS_API_KEY", "EPHEMERIS_API_TOKEN", "SAVETOKENS_MODE"):
        monkeypatch.delenv(name, raising=False)

    def no_network(*a, **k):
        raise RuntimeError("no network in tests")
    from savetokens import ephemeris
    monkeypatch.setattr(ephemeris, "_call", no_network)
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
    monkeypatch.setenv("SAVETOKENS_HOME", str(tmp_path / "st"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    return tmp_path


@pytest.fixture
def store():
    s = Store()
    yield s
    s.close()


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class Transcript:
    """Writes Claude Code style JSONL: one line per content block, as Claude Code does."""

    def __init__(self, path, session="s1", cwd="/work/proj", subagent=False, agent_id=None):
        self.path, self.session, self.cwd = path, session, cwd
        self.subagent, self.agent_id = subagent, agent_id
        self.n = 0
        path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, d):
        with open(self.path, "a") as f:
            f.write(json.dumps(d) + "\n")

    def _base(self, ts, kind):
        d = {"type": kind, "sessionId": self.session, "timestamp": iso(ts), "cwd": self.cwd,
             "isSidechain": self.subagent}
        if self.agent_id:
            d["agentId"] = self.agent_id
        return d

    def turn(self, ts, *, model="claude-opus-5-5", input=2, read=50_000, write5=1_000, write1h=0, output=500,
             tools=(), blocks=1):
        """An assistant message split into `blocks` text lines plus one line per tool_use."""
        self.n += 1
        tag = f"{self.session}{self.agent_id or ''}_{self.n}"
        mid, rid = f"msg_{tag}", f"req_{tag}"
        usage = {"input_tokens": input, "cache_read_input_tokens": read,
                 "cache_creation_input_tokens": write5 + write1h, "output_tokens": output,
                 "cache_creation": {"ephemeral_5m_input_tokens": write5, "ephemeral_1h_input_tokens": write1h}}
        content = [{"type": "text", "text": "x"}] * blocks + [
            {"type": "tool_use", "id": tid, "name": name, "input": inp} for tid, name, inp in tools]
        for block in content:
            d = self._base(ts, "assistant")
            d.update(requestId=rid, message={"id": mid, "model": model, "role": "assistant",
                                             "content": [block], "usage": usage})
            self._write(d)
        return mid

    def result(self, ts, tool_use_id, *, ok=True, chars=100):
        d = self._base(ts, "user")
        d["message"] = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_id,
                                                     "content": "y" * chars, "is_error": not ok}]}
        self._write(d)


@pytest.fixture
def transcript(homes):
    def make(session="s1", project="proj", **kw):
        path = homes / "claude" / "projects" / f"-work-{project}" / f"{session}.jsonl"
        return Transcript(path, session=session, cwd=f"/work/{project}", **kw)
    return make


HERMES_SCHEMA = """
CREATE TABLE session_model_usage (
    session_id TEXT NOT NULL, model TEXT NOT NULL, billing_provider TEXT NOT NULL DEFAULT '',
    billing_base_url TEXT NOT NULL DEFAULT '', billing_mode TEXT NOT NULL DEFAULT '', task TEXT NOT NULL DEFAULT '',
    api_call_count INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0, actual_cost_usd REAL NOT NULL DEFAULT 0,
    cost_status TEXT, cost_source TEXT, first_seen REAL, last_seen REAL,
    PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task));
"""
TRACKER_SCHEMA = """
CREATE TABLE calls (id INTEGER PRIMARY KEY AUTOINCREMENT, ended_at REAL NOT NULL, started_at REAL,
    session_id TEXT, platform TEXT, model TEXT, provider TEXT, base_url TEXT, api_request_id TEXT UNIQUE,
    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0, usage_missing INTEGER NOT NULL DEFAULT 0);
"""


@pytest.fixture
def hermes_home(homes):
    h = homes / "hermes"
    h.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(h / "state.db")
    con.executescript(HERMES_SCHEMA)
    rows = [
        ("old", "anthropic/claude-sonnet-5-5", "", 10, 100_000, 5_000, 400_000, 20_000, 0.0, 1.50, T0 - 86400, T0 - 80000),
        ("old", "qwen/qwen3-coder", "", 5, 200_000, 10_000, 0, 0, 0.30, 0.0, T0 - 86400, T0 - 80000),
        ("tracked", "anthropic/claude-sonnet-5-5", "", 2, 1_000, 100, 0, 0, 0.0, 0.02, T0 - 3600, T0 - 3500),
    ]
    con.executemany(
        "INSERT INTO session_model_usage (session_id, model, task, api_call_count, input_tokens, output_tokens,"
        " cache_read_tokens, cache_write_tokens, estimated_cost_usd, actual_cost_usd, first_seen, last_seen)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    tt = h / "plugin-data" / "token-tracker"
    tt.mkdir(parents=True)
    con = sqlite3.connect(tt / "calls.db")
    con.executescript(TRACKER_SCHEMA)
    con.executemany("INSERT INTO calls (ended_at, session_id, model, api_request_id, input_tokens, output_tokens)"
                    " VALUES (?,?,?,?,?,?)",
                    [(T0 - 3550, "tracked", "anthropic/claude-sonnet-5-5", "r1", 600, 60),
                     (T0 - 3500, "tracked", "anthropic/claude-sonnet-5-5", "r2", 400, 40)])
    con.commit()
    con.close()
    return h
