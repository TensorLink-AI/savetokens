"""Shared fixtures: isolated homes, no network or crontab, and a Claude Code transcript writer."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from savetokens.store import Store

T0 = 1_790_000_000.0   # fixed clock for deterministic tests
H = 3600


@pytest.fixture(autouse=True)
def homes(tmp_path, monkeypatch):
    from savetokens import alerts, ephemeris, install, maintain, schedule
    monkeypatch.setattr(schedule, "available", lambda: False)
    monkeypatch.setattr(install, "claude_cli", lambda: None)     # never the real `claude mcp add`
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(maintain, "kick", lambda *a, **k: None)
    monkeypatch.setattr(alerts, "desktop", lambda *a, **k: None)
    for name in ("EPHEMERIS_API_KEY", "EPHEMERIS_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    def no_network(*a, **k):
        raise RuntimeError("no network in tests")
    monkeypatch.setattr(ephemeris, "_call", no_network)
    monkeypatch.setenv("SAVETOKENS_HOME", str(tmp_path / "st"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    from savetokens import pricing
    pricing.reset()
    return tmp_path


@pytest.fixture
def running(tmp_path):
    """A sync server on a free port: (users, a user's token, its URL)."""
    import threading
    from http.server import ThreadingHTTPServer

    from savetokens import server
    users = server.Users(tmp_path / "srv")
    token = users.add("chris")
    dirty, lock = set(), threading.Lock()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(users, dirty, lock))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield users, token, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def store():
    s = Store()
    yield s
    s.close()


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class Transcript:
    """Claude Code style JSONL: one line per content block, as Claude Code writes it."""

    def __init__(self, path, session="s1"):
        self.path, self.session, self.n = path, session, 0
        path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, d):
        with open(self.path, "a") as f:
            f.write(json.dumps(d) + "\n")

    def turn(self, ts, *, model="claude-opus-5-5", input=2, read=50_000, write5=1_000, output=500, blocks=2):
        self.n += 1
        usage = {"input_tokens": input, "cache_read_input_tokens": read, "cache_creation_input_tokens": write5,
                 "output_tokens": output, "cache_creation": {"ephemeral_5m_input_tokens": write5,
                                                             "ephemeral_1h_input_tokens": 0}}
        for _ in range(blocks):
            self._write({"type": "assistant", "sessionId": self.session, "timestamp": iso(ts),
                         "requestId": f"req_{self.n}",
                         "message": {"id": f"msg_{self.n}", "model": model, "role": "assistant",
                                     "content": [{"type": "text", "text": "x"}], "usage": usage}})

    def limit_error(self, ts, text):
        self._write({"type": "assistant", "sessionId": self.session, "timestamp": iso(ts), "error": "rate_limit",
                     "isApiErrorMessage": True,
                     "message": {"id": "e", "model": "<synthetic>", "role": "assistant",
                                 "content": [{"type": "text", "text": text}], "usage": {"input_tokens": 0}}})


@pytest.fixture
def transcript(homes):
    def make(session="s1"):
        return Transcript(homes / "claude" / "projects" / "-work-proj" / f"{session}.jsonl", session)
    return make


def week_of_readings(store, start, hours, per_hour, resets, account="a1", five_ratio=4.0, usd_per_pct=10.0):
    """Hourly meter readings rising `per_hour` % a hour, with matching captured usage."""
    from savetokens.store import Usage
    pct = 0.0
    for i in range(hours):
        t = start + i * H
        pct += per_hour
        store.add_meter("claude-code", account, {"seven_day": (pct, resets),
                                                 "five_hour": (min(100, (pct * five_ratio) % 100), resets)}, ts=t)
        store.add_usage([Usage("claude-code", "s", f"r{i}", t - 60, "claude-opus-5-5", cost_usd=per_hour * usd_per_pct)])
    return pct
