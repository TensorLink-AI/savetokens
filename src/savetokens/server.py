"""The savetokens server: one place where every machine's usage and every account's meter add up.

  POST /v1/push    a machine's new rows (usage, meter, hits)
  POST /v1/settings   API budgets (the newest setting wins)
  POST /v1/pair    a short join code for more machines (authenticated; {"machines": N, "hours": H})
  POST /v1/join    a join code in, that user's new token out (no token needed; codes expire)
  POST /v1/token   a new token for this user, for CI and containers (SAVETOKENS_TOKEN)
  GET  /v1/pull    other machines' meter readings and hits, and the newest forecast paths
  GET  /v1/status     the current outlook per limit (JSON)
  GET  /v1/dashboard  everything the dashboard draws, across every machine
  GET  /join       a shell script that installs savetokens and joins: curl -fsSL URL/join | sh -s CODE
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
import os
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
INVITE_SECONDS = 24 * 3600   # a code for "add my machines": a day, up to INVITE_MACHINES of them
INVITE_MACHINES = 10
MAX_INVITE_SECONDS = 7 * 86400
MAX_INVITE_MACHINES = 50
SOURCE = "git+https://github.com/TensorLink-AI/savetokens"
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

    def pair(self, name, seconds=CODE_SECONDS, uses=1) -> str:
        """A join code for `name`: XXXX-XXXX, valid for `seconds` and `uses` machines."""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        codes = {k: v for k, v in self._codes().items() if v["expires"] > time.time()}
        codes[_hash(code)] = {"user": name, "expires": time.time() + seconds, "uses": uses}
        self._save_codes(codes)
        return f"{code[:4]}-{code[4:]}"

    def join(self, code):
        """The user's new token for a valid code (one use of it), else None."""
        key = _hash(re.sub(r"[^A-Z0-9]", "", (code or "").upper()))
        codes = self._codes()
        c = codes.pop(key, None)
        if c and c.get("uses", 1) > 1 and c["expires"] >= time.time():
            codes[key] = {**c, "uses": c["uses"] - 1}
        self._save_codes(codes)
        if not c or c["expires"] < time.time():
            return None
        return self.add(c["user"])

    def joined(self, name) -> bool:
        """Has any machine of `name`'s sent data yet?"""
        return (self.data / "users" / f"{name}.db").exists()

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
    from .teams import Teams
    failures = []   # times of failed joins, to slow down guessing
    teams = Teams(users.data)

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

        def _base(self, others=False):
            """This server's address as the caller reached it (behind a proxy too), or None if odd. others: for a
            command to paste on another machine, where 127.0.0.1 would mean that machine."""
            fixed = public_url(users.data)   # set up for remote machines: always that address
            if fixed:
                return fixed
            host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").split(",")[0].strip()
            proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
            if proto not in ("http", "https") or not re.fullmatch(r"[A-Za-z0-9.\-]+(:\d{1,5})?|\[[0-9a-fA-F:.]+\](:\d{1,5})?",
                                                                    host):
                return None
            name, _, port = host.rpartition(":") if not host.endswith("]") else (host, "", "")
            listening = self.server.server_address[0] if hasattr(self, "server") else ""
            if others and (name or host) in ("127.0.0.1", "localhost", "[::1]") and listening in ("0.0.0.0", "::"):
                from . import hub
                url, how = hub.address(int(port) if port.isdigit() else 80)
                if how != "local":
                    return url
            return f"{proto}://{host}"

        def _script(self):
            base = self._base()
            if not base:
                return self._send(400, {"error": "odd Host header"})
            data = join_script(base).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/x-shellscript; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _pair(self, user):
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)) or b"{}")
                machines = int(body.get("machines") or 1)
                seconds = float(body["hours"]) * 3600 if body.get("hours") else CODE_SECONDS
            except (ValueError, TypeError, AttributeError):
                return self._send(400, {"error": "bad body"})
            if not (1 <= machines <= MAX_INVITE_MACHINES and 60 <= seconds <= MAX_INVITE_SECONDS):
                return self._send(400, {"error": f"machines is 1 to {MAX_INVITE_MACHINES}, hours at most"
                                                 f" {MAX_INVITE_SECONDS // 3600}"})
            code, base = users.pair(user, seconds, machines), self._base(others=True)
            self._send(200, {"code": code, "expires_in": int(seconds), "machines": machines, "url": base,
                             "command": join_command(base, code) if base else None})

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
                return self._pair(user)
            if path == "/v1/token":
                return self._send(200, {"token": users.add(user), "url": self._base(others=True)})
            if path in ("/v1/logs", "/v1/traces"):   # OpenTelemetry (OTLP over HTTP, JSON)
                return self._otel(user)
            if path == "/v1/team":
                return self._team(user)
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
                added = s.upsert_usage(cols, rows) if table == "usage" else s.insert(table, cols, rows)
                if table == "usage":
                    from . import otel
                    otel.replace_with_reader(s, cols, rows)
            team, _ = teams.of(user)
            if team and table == "usage":
                with teams.store(team) as ts:
                    ts.upsert_usage(cols + ["person"], [list(r) + [user] for r in rows])
                with lock:
                    dirty.add(f"team:{team}")
            with lock:
                dirty.add(user)
            self._send(200, {"added": added})

        def _settings(self, user, body):
            if "detected" in body:
                return self._detected(user, body)
            if "names" in body:
                return self._names(user, body["names"])
            if "goals" in body:
                return self._goals(user, body)
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

        def _names(self, user, names):
            """Project and task names a machine chose to send (`savetokens projects --share-names on`), so the
            dashboard can show them; {} clears what it sent."""
            short = lambda v, n=80: isinstance(v, str) and 0 < len(v) <= n
            if not isinstance(names, dict):
                return self._send(400, {"error": "bad names"})
            got = {"projects": {k: v for k, v in list((names.get("projects") or {}).items())[:2000]
                                if short(k, 32) and short(v)},
                   "tasks": {k: {"name": v["name"], "project_id": v.get("project_id") if short(v.get("project_id"), 32)
                                 else None, "source": v.get("source") if short(v.get("source"), 16) else None,
                                 "done": v.get("done") if isinstance(v.get("done"), (int, float)) else None}
                             for k, v in list((names.get("tasks") or {}).items())[:5000]
                             if short(k, 32) and isinstance(v, dict) and short(v.get("name"), 120)}}
            with users.store(user) as s:
                s.set_meta("shared_names", got)
            self._send(200, {"ok": True})

        def _team(self, user):
            """A team's settings: budgets (admins), a shared project name (anyone), the per-person view (owner)."""
            team, role = teams.of(user)
            if not team:
                return self._send(404, {"error": "not in a team on this hub"})
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), MAX_BODY)) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad body"})
            with teams.store(team) as ts:
                if "goals" in body:
                    if role not in ("owner", "admin"):
                        return self._send(403, {"error": "only the team's owner or admins set its budgets"})
                    if not _valid_goals(body["goals"]):
                        return self._send(400, {"error": "bad budgets"})
                    ts.set_meta("goals", body["goals"])
                    ts.set_meta("goals_at", time.time())
                if isinstance(body.get("share"), dict):
                    names = ts.meta("shared_names") or {}
                    for pid, name in list(body["share"].items())[:50]:
                        if isinstance(pid, str) and len(pid) <= 32 and isinstance(name, str) and 0 < len(name) <= 80:
                            names[pid] = name
                    ts.set_meta("shared_names", names)
                if "people_visible" in body:
                    if role != "owner":
                        return self._send(403, {"error": "only the team's owner decides that"})
                    teams.set(team, people_visible=bool(body["people_visible"]))
                goals = ts.meta("goals") or {}
            with lock:
                dirty.add(f"team:{team}")
            self._send(200, {"ok": True, "goals": goals})

        def _otel(self, user):
            from . import otel
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._send(413, {"error": "too large"})
            raw = self.rfile.read(n)
            if self.headers.get("Content-Encoding", "").lower() == "gzip":
                import gzip
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    return self._send(400, {"error": "bad gzip"})
            if "json" not in self.headers.get("Content-Type", "application/json"):
                return self._send(415, {"error": "send OTLP as JSON: OTEL_EXPORTER_OTLP_PROTOCOL=http/json"})
            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad body"})
            with users.store(user) as s:
                added = otel.ingest(s, payload)
            if added:
                with lock:
                    dirty.add(user)
            self._send(200, {"partialSuccess": {}})

        def _goals(self, user, body):
            """The budgets you set (`savetokens budget`); the newest setting wins."""
            goals, at = body["goals"], float(body["goals_at"])
            if not _valid_goals(goals):
                return self._send(400, {"error": "bad budgets"})
            with users.store(user) as s:
                if at > (s.meta("goals_at") or 0):
                    s.set_meta("goals", goals)
                    s.set_meta("goals_at", at)
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
            if urllib.parse.urlparse(self.path).path == "/join":
                return self._script()
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
                    out.update(_hub_projects(s))
                    out["hub_url"] = public_url(users.data)   # where machines should reach this hub, if set
                    if machine:   # when each machine last synced: the dashboard says which have gone quiet
                        seen = s.meta("machines_synced") or {}
                        seen[machine[:64]] = time.time()
                        s.set_meta("machines_synced", seen)
                    team, _ = teams.of(user)
                    if team:   # the team's key, and its alerts (its budgets) for every member
                        out["project_key"] = teams.get(team)["key"]
                        with teams.store(team) as ts:
                            after = int(q.get("team_alerts", 0))
                            out["team_alerts"] = [{"id": r[0], "ts": r[1], "message": r[2]} for r in ts.conn.execute(
                                "SELECT rowid, ts, message FROM alerts WHERE rowid > ? ORDER BY rowid", (after,))]
                    return self._send(200, out)
                if url.path == "/v1/project-key":
                    from . import projects
                    team, _ = teams.of(user)
                    return self._send(200, {"project_key": teams.get(team)["key"] if team else projects.key(s)})
                if url.path == "/v1/team":
                    team, role = teams.of(user)
                    if not team:
                        return self._send(404, {"error": "not in a team on this hub"})
                    from .teams import summary
                    with teams.store(team) as ts:
                        out = summary(ts, teams.get(team), role, teams.sees_people(team, role), time.time(),
                                      int(q.get("days", 30)))
                    return self._send(200, {"team": team, "you": user, **out})
                if url.path == "/v1/dashboard":
                    from . import dashboard
                    return self._send(200, dashboard.snapshot(s, on_server=True))
                if url.path == "/v1/status":
                    return self._send(200, {"outlook": forecast.outlook(s), "made_at": s.meta("forecast_made_at")})
            self._send(404, {"error": "not found"})
    return Handler


def _valid_goals(goals) -> bool:
    from .pools import UNITS
    ok = lambda g: (isinstance(g, dict) and isinstance(g.get("label"), str) and len(g["label"]) <= 80
                    and g.get("unit") in UNITS and isinstance(g.get("amount"), (int, float)) and g["amount"] > 0
                    and (g.get("period") in PERIODS or isinstance(g.get("by"), (int, float)))
                    and isinstance(g.get("start"), (int, float)) and isinstance(g.get("scope"), dict)
                    and g.get("count") in (None, "api", "all")
                    and all(isinstance(g["scope"].get(k) or [], list) and len(g["scope"].get(k) or []) <= 50
                            and all(isinstance(x, str) and len(x) <= 32 for x in g["scope"].get(k) or [])
                            for k in ("projects", "tasks", "providers", "tools")))
    return isinstance(goals, dict) and len(goals) <= 100 and all(
        isinstance(k, str) and len(k) <= 32 and ok(g) for k, g in goals.items())


def _hub_projects(s, now=None):
    """What only the hub knows: every machine's totals per project and per budget, and the project key that
    makes a project's id the same on every machine."""
    from . import pools, projects
    now = now or time.time()
    spent = {}
    for p in pools.pools(s, now, goals=True):
        if p.kind == "goal":
            since, _ = pools.period(p.budget, now)
            spent[p.id] = {"spent": pools.spent(s, p, since, now), "since": since, "at": now}
    # "usd" (everything at API prices) for machines from before the split
    totals = [{"id": pid, "usd": t["value_usd"], **t} for pid, t in projects.split(s, "project_id",
                                                                                   now - 30 * 86400).items()]
    return {"project_key": projects.key(s), "goals": s.meta("goals"), "goals_at": s.meta("goals_at"),
            "goal_spent": spent, "projects": totals, "projects_at": now}


def engine_loop(users: Users, dirty: set, lock: threading.Lock, stop: threading.Event, log=print):
    """Every minute: run the engine for users with new data, and for everyone whose forecast is due."""
    from . import pricing
    seen = None
    while not stop.is_set():
        with lock:
            fresh = set(dirty)
            dirty.clear()
        pricing.refresh()                # the price catalogue, once a day, as machines do
        cat = pricing.catalogue_at()
        if cat != seen:                  # newer on disk (fetched here or by a machine sharing this home)
            pricing.reset()
            seen = cat

        def reprice(s, key):
            # requests that came in unpriced (a model newer than the machine's catalogue) or were priced
            # on the machine after they were sent: priced here once a newer catalogue knows them
            if key in fresh or s.meta("repriced_with") != cat:
                if pricing.reprice(s):
                    fresh.add(key)
                s.set_meta("repriced_with", cat)
        for name in users.names():
            try:
                with users.store(name) as s:
                    now = time.time()
                    from . import providers   # opted in with an admin key: API usage no reader saw
                    if providers.sync(s, users.data, name, now, log=lambda m, n=name: log(f"{n}: {m}")):
                        fresh.add(name)
                    reprice(s, name)
                    if name in fresh or maintain.refresh_due(s, s.meta("forecast_made_at"), now):
                        maintain.update(s, now, log=lambda m, n=name: log(f"{n}: {m}"))
            except Exception as e:   # one user's failure must not stop the others
                log(f"{name}: engine failed: {e}")
        from .teams import Teams
        tm = Teams(users.data)
        for team in tm.names():
            try:
                with tm.store(team) as s:
                    now = time.time()
                    reprice(s, f"team:{team}")
                    if f"team:{team}" in fresh or maintain.refresh_due(s, s.meta("forecast_made_at"), now):
                        maintain.update(s, now, log=lambda m, n=team: log(f"team {n}: {m}"))
            except Exception as e:
                log(f"team {team}: engine failed: {e}")
        stop.wait(ENGINE_SECONDS)


def join_command(base, code):
    return f"curl -fsSL {base}/join | sh -s {code}"


INSTALL_URL = "https://raw.githubusercontent.com/TensorLink-AI/savetokens/main/install.sh"


def install_script(base=None):
    """The one-line installer: install savetokens if it's missing, then set this machine up.
    With a hub (what GET /join serves) it also joins it, with the code given; install.sh is the one without."""
    if base:
        head = f"""# savetokens: install it (if needed) and join the hub at {base}
#   curl -fsSL {base}/join | sh -s CODE
set -eu
HUB='{base}'
CODE="${{1:-}}"
if [ -z "$CODE" ]; then
  echo "usage: curl -fsSL $HUB/join | sh -s CODE   (a code: savetokens pair, or 'Add a machine' in $HUB)" >&2
  exit 2
fi
"""
        run = 'savetokens install --server "$HUB" --code "$CODE"'
    else:
        head = f"""# savetokens: install it (if needed) and set up this machine
#   curl -fsSL {INSTALL_URL} | sh
#   ... | sh -s -- --no-ephemeris      (any `savetokens install` options after --)
set -eu
"""
        run = 'savetokens install "$@"'
    return f"""#!/bin/sh
{head}SOURCE="${{SAVETOKENS_SOURCE:-{SOURCE}}}"
PATH="$HOME/.local/bin:$PATH"; export PATH
if ! command -v savetokens >/dev/null 2>&1; then
  echo "Installing savetokens..."
  if command -v uv >/dev/null 2>&1; then uv tool install --quiet "$SOURCE"
  elif command -v pipx >/dev/null 2>&1; then pipx install --quiet "$SOURCE"
  else
    echo "(first uv, the Python installer: https://docs.astral.sh/uv)"
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
    uv tool install --quiet "$SOURCE"
  fi
fi
if ( : </dev/tty ) 2>/dev/null; then
  exec {run} </dev/tty
else
  exec {run} --yes
fi
"""


def join_script(base):
    """What GET /join serves: install savetokens if it's missing, then join this server with the code."""
    return install_script(base)


def public_url(data=None):
    """The address machines join this hub at: SAVETOKENS_PUBLIC_URL, else what `server --setup` or
    `server --public-url` saved with the hub's data."""
    env = (os.environ.get("SAVETOKENS_PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
    if env or data is None:
        return env or None
    try:
        return (Path(data) / PUBLIC_URL_FILE).read_text().strip().rstrip("/") or None
    except OSError:
        return None


PUBLIC_URL_FILE = "public_url"


def set_public_url(data, url):
    """Save (or, with None, forget) the address every join command uses."""
    path = Path(data) / PUBLIC_URL_FILE
    if not url:
        path.unlink(missing_ok=True)
        return None
    url = url.strip().rstrip("/")
    if not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[A-Za-z0-9._~\-/]*)?", url):
        raise ValueError(f"not an http(s) address: {url!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(url + "\n")
    return url


def first_user(users: Users, log=print):
    """On a fresh server, make the first user (its token saved to first-token.txt, for SAVETOKENS_TOKEN).
    Until a machine has joined, every start also prints a join line for the first machines."""
    token = None
    if not users.names():
        token = users.add("me")
        path = users.data / "first-token.txt"
        path.write_text(token + "\n")
        path.chmod(0o600)
        log("Your token, for containers and cloud sessions (SAVETOKENS_TOKEN), is in first-token.txt.")
    if users.names() == ["me"] and not users.joined("me"):
        code = users.pair("me", seconds=7 * 86400, uses=INVITE_MACHINES)
        base = public_url(users.data) or "<this server's URL>"
        log(f"Add your machines (this code works for a week, for up to {INVITE_MACHINES} machines):")
        log(f"  {join_command(base, code)}")
        log("More codes later: `savetokens pair` on a joined machine, or 'Add a machine' in the browser view.")
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
