"""The savetokens server: one place where every machine's usage and every account's meter add up.

  POST /v1/push    a machine's new rows (usage, meter, hits)
  GET  /v1/pull    other machines' meter readings and hits, and the newest forecast paths
  GET  /v1/status     the current outlook per limit (JSON)
  GET  /v1/dashboard  everything the dashboard draws, across every machine

One SQLite database per user, with the same schema and engine as a machine:
forecasts are made here (Ephemeris with the server's key) on the same cadence,
whenever new data arrives. Users authenticate with a bearer token; only a hash
of each token is stored.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import forecast, maintain
from .store import SYNCED, Store

MAX_BODY = 16 * 1024 * 1024
ENGINE_SECONDS = 60


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Users:
    def __init__(self, data: Path):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.path = self.data / "tokens.json"

    def _load(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def add(self, name) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
            raise ValueError("user names are letters, digits, . _ and - only")
        token = "st_" + secrets.token_urlsafe(32)
        tokens = self._load()
        tokens[_hash(token)] = name
        self.path.write_text(json.dumps(tokens, indent=1))
        self.path.chmod(0o600)
        return token

    def who(self, token):
        return self._load().get(_hash(token)) if token else None

    def names(self):
        return sorted(set(self._load().values()))

    def store(self, name) -> Store:
        return Store(self.data / "users" / f"{name}.db")


def _rows_after(store, table, cursor, machine):
    cols = SYNCED[table]
    rows = store.conn.execute(f"SELECT rowid, {','.join(cols)} FROM {table} WHERE rowid > ? AND machine != ?"
                              f" ORDER BY rowid LIMIT 20000", (cursor, machine)).fetchall()
    return {"cursor": rows[-1][0] if rows else cursor, "rows": [list(r)[1:] for r in rows]}


def make_handler(users: Users, dirty: set, lock: threading.Lock):
    class Handler(BaseHTTPRequestHandler):
        server_version = "savetokens"

        def log_message(self, fmt, *args):   # no request logs: they would carry user names
            pass

        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _user(self):
            auth = self.headers.get("Authorization", "")
            return users.who(auth.removeprefix("Bearer ").strip())

        def do_POST(self):
            user = self._user()
            if not user:
                return self._send(401, {"error": "unknown token"})
            if urllib.parse.urlparse(self.path).path != "/v1/push":
                return self._send(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._send(413, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(n))
                table, cols, rows = body["table"], body["cols"], body["rows"]
            except (ValueError, KeyError, TypeError):
                return self._send(400, {"error": "bad body"})
            if table not in SYNCED or cols != SYNCED[table] or any(len(r) != len(cols) for r in rows):
                return self._send(400, {"error": "unexpected table or columns"})
            with users.store(user) as s:
                added = s.insert(table, cols, rows)
            with lock:
                dirty.add(user)
            self._send(200, {"added": added})

        def do_GET(self):
            if self.path == "/healthz":
                from . import __version__
                return self._send(200, {"ok": True, "version": __version__})
            user = self._user()
            if not user:
                return self._send(401, {"error": "unknown token"})
            url = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            with users.store(user) as s:
                if url.path == "/v1/pull":
                    machine = q.get("machine", "")
                    out = {t: _rows_after(s, t, int(q.get(t, 0)), machine) for t in ("meter", "hits")}
                    after = float(q.get("paths_after", 0))
                    out["paths"] = [{"account": r["account"], "source": r["source"], "made_at": r["made_at"],
                                     "start_hour": r["start_hour"], "hours": r["hours"], "n": r["n"],
                                     "data": base64.b64encode(r["data"]).decode()}
                                    for r in s.conn.execute("SELECT * FROM paths WHERE made_at > ?", (after,))]
                    out["forecast_made_at"] = s.meta("forecast_made_at")
                    out["ephemeris_last"] = s.meta("ephemeris_last")
                    out["track_record"] = s.meta("track_record")
                    return self._send(200, out)
                if url.path == "/v1/dashboard":
                    from . import dashboard
                    return self._send(200, dashboard.snapshot(s))
                if url.path == "/v1/status":
                    return self._send(200, {"outlook": forecast.outlook(s), "made_at": s.meta("forecast_made_at")})
            self._send(404, {"error": "not found"})
    return Handler


def engine_loop(users: Users, dirty: set, lock: threading.Lock, stop: threading.Event, log=print):
    """Every minute: run the engine for users with new data, and for everyone whose forecast is due."""
    while not stop.is_set():
        with lock:
            fresh = set(dirty)
            dirty.clear()
        for name in users.names():
            try:
                with users.store(name) as s:
                    now = time.time()
                    if name in fresh or maintain.refresh_due(s, s.meta("forecast_made_at"), now):
                        maintain.update(s, now, log=lambda m, n=name: log(f"{n}: {m}"))
            except Exception as e:   # one user's failure must not stop the others
                log(f"{name}: engine failed: {e}")
        stop.wait(ENGINE_SECONDS)


def first_user(users: Users, log=print):
    """On a fresh server, make the first user and show its token once (also saved to first-token.txt)."""
    if users.names():
        return None
    token = users.add("me")
    path = users.data / "first-token.txt"
    path.write_text(token + "\n")
    path.chmod(0o600)
    log("No users yet, so one was made. Your token (also in first-token.txt in the data directory):")
    log(f"  {token}")
    log("On each machine: savetokens install --server <this server's URL> --token <token>")
    return token


def serve(data: Path, host="127.0.0.1", port=8787, log=print):
    users = Users(data)
    first_user(users, log)
    from . import ephemeris
    log("forecasts: " + ("Ephemeris (key found) with the local baseline as fallback" if ephemeris.enabled()
                         else "local baseline only; set EPHEMERIS_API_KEY to forecast with Ephemeris"))
    dirty, lock, stop = set(), threading.Lock(), threading.Event()
    threading.Thread(target=engine_loop, args=(users, dirty, lock, stop, log), daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), make_handler(users, dirty, lock))
    log(f"savetokens server on http://{host}:{port} (data in {data})")
    try:
        httpd.serve_forever()
    finally:
        stop.set()
