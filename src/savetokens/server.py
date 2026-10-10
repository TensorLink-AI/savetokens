"""The savetokens server: one place where every machine's usage and every account's meter add up.

  POST /v1/push    a machine's new rows (usage, meter, hits)
  POST /v1/settings   API budgets (the newest setting wins)
  POST /v1/pair    a short join code for another machine (authenticated)
  POST /v1/join    a join code in, that user's new token out (no token needed; codes expire in 10 minutes)
  GET  /v1/pull    other machines' meter readings and hits, and the newest forecast paths
  GET  /v1/status     the current outlook per limit (JSON)
  GET  /v1/dashboard  everything the dashboard draws, across every machine
  GET  /           the browser view (sign in with a join code; it then reads /v1/dashboard)

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
from .pools import PERIODS
from .store import SYNCED, Store

MAX_BODY = 16 * 1024 * 1024
ENGINE_SECONDS = 60
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L
CODE_SECONDS = 600
JOIN_FAILURES_PER_MINUTE = 20


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

    def pair(self, name, seconds=CODE_SECONDS) -> str:
        """A one-time join code for `name`: XXXX-XXXX, valid for `seconds`."""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        codes = {k: v for k, v in self._codes().items() if v["expires"] > time.time()}
        codes[_hash(code)] = {"user": name, "expires": time.time() + seconds}
        self._save_codes(codes)
        return f"{code[:4]}-{code[4:]}"

    def join(self, code):
        """The user's new token for a valid code (used up), else None."""
        key = _hash(re.sub(r"[^A-Z0-9]", "", (code or "").upper()))
        codes = self._codes()
        c = codes.pop(key, None)
        self._save_codes(codes)
        if not c or c["expires"] < time.time():
            return None
        return self.add(c["user"])

    def _codes(self):
        try:
            return json.loads((self.data / "codes.json").read_text())
        except (OSError, ValueError):
            return {}

    def _save_codes(self, codes):
        path = self.data / "codes.json"
        path.write_text(json.dumps(codes))
        path.chmod(0o600)

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
    failures = []   # times of failed joins, to slow down guessing

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

        def _join(self):
            now = time.time()
            with lock:
                failures[:] = [t for t in failures if t > now - 60]
                if len(failures) >= JOIN_FAILURES_PER_MINUTE:
                    return self._send(429, {"error": "too many attempts; wait a minute"})
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)))
                token = users.join(str(body["code"]))
            except (ValueError, KeyError, TypeError):
                token = None
            if not token:
                with lock:
                    failures.append(now)
                return self._send(403, {"error": "unknown or expired code"})
            self._send(200, {"token": token})

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/v1/join":
                return self._join()
            user = self._user()
            if not user:
                return self._send(401, {"error": "unknown token"})
            if path == "/v1/pair":
                return self._send(200, {"code": users.pair(user), "expires_in": CODE_SECONDS})
            if path not in ("/v1/push", "/v1/settings"):
                return self._send(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._send(413, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(n))
                if path == "/v1/settings":
                    return self._settings(user, body)
                table, cols, rows = body["table"], body["cols"], body["rows"]
            except (ValueError, KeyError, TypeError):
                return self._send(400, {"error": "bad body"})
            # an older client sends fewer columns (no billing): any known subset with the key columns is fine
            if (table not in SYNCED or not set(cols) <= set(SYNCED[table]) or len(set(cols)) != len(cols)
                    or not {"machine", "ts"} <= set(cols) or any(len(r) != len(cols) for r in rows)):
                return self._send(400, {"error": "unexpected table or columns"})
            with users.store(user) as s:
                added = s.insert(table, cols, rows)
            with lock:
                dirty.add(user)
            self._send(200, {"added": added})

        def _settings(self, user, body):
            if "detected" in body:
                return self._detected(user, body)
            budgets, plans, at = body["budgets"], body.get("plans") or {}, float(body["at"])
            if not all(isinstance(d, dict) and all(
                    isinstance(b, dict) and isinstance(b.get("usd"), (int, float)) and b.get("period") in PERIODS
                    for b in d.values()) for d in (budgets, plans)):
                return self._send(400, {"error": "bad budgets"})
            with users.store(user) as s:
                if at > (s.meta("budgets_at") or 0):
                    for key, d in (("budgets", budgets), ("plans", plans)):
                        s.set_meta(key, {h: {"usd": float(b["usd"]), "period": b["period"],
                                             "tz": int(b.get("tz") or 0)} for h, b in d.items()})
                    s.set_meta("budgets_at", at)
                if isinstance(body.get("tz"), int):
                    s.set_meta("tz", body["tz"])   # the user's clock, for days, weeks and months
            with lock:
                dirty.add(user)
            self._send(200, {"ok": True})

        def _detected(self, user, body):
            """A machine's tools and plans (names and list prices only), for the setup list."""
            from .pools import TOOLS
            got, machine = body["detected"], str(body.get("machine") or "")[:64]
            if not isinstance(got, dict) or not all(
                    h in TOOLS and isinstance(d, dict) and d.get("billing") in ("api", "subscription", None)
                    and isinstance(d.get("usd"), (int, float, type(None))) and isinstance(d.get("plan"), (str, type(None)))
                    for h, d in got.items()):
                return self._send(400, {"error": "bad detected"})
            with users.store(user) as s:
                all_ = s.meta("detected") or {}
                all_[machine] = {h: {"billing": d.get("billing"), "plan": (d.get("plan") or None) and d["plan"][:40],
                                     "usd": d.get("usd")} for h, d in got.items()}
                s.set_meta("detected", all_)
            self._send(200, {"ok": True})

        def do_GET(self):
            from . import web
            if web.send_static(self, self.path):
                return
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
                    out["budgets"], out["budgets_at"] = s.meta("budgets"), s.meta("budgets_at")
                    out["plans"] = s.meta("plans")
                    return self._send(200, out)
                if url.path == "/v1/dashboard":
                    from . import dashboard
                    return self._send(200, dashboard.snapshot(s, on_server=True))
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
    code = users.pair("me", seconds=3600)
    log("No users yet, so one was made. To add a machine, run on it (this code works for an hour, once;")
    log("  more codes: `savetokens pair` on a joined machine, or `savetokens server --pair me` here):")
    log(f"  savetokens join <this server's URL> {code}")
    log("Your token, for containers and cloud sessions (SAVETOKENS_TOKEN), is in first-token.txt.")
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
    log(f"savetokens server on http://{host}:{port} (data in {data}); the browser view is at /")
    try:
        httpd.serve_forever()
    finally:
        stop.set()
