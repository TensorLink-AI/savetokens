"""Teams on a hub: several people's usage added up, with the same projects, budgets and forecasts.

A team is a set of the hub's users. Its members' machines all use the team's project key, so a repository is
the same project for everyone. Every member's usage is also written to the team's own store (with whose it
is), so the team's budgets are forecast and alerted by the same engine as anyone's.

  owner   everything; decides whether members see the per-person view
  admin   sets the team's budgets; sees the per-person view
  member  sees the team's totals, its budgets and their own usage

Project names stay on machines unless a member shares one (`savetokens project share`); otherwise the team
sees an id.

  savetokens server --team acme --member sam --role admin    # on the hub
  savetokens team                                            # on any member's machine
  savetokens budget --team --project webapp --usd 2000 --per month
"""
from __future__ import annotations

import json
import secrets
import threading
from pathlib import Path

from .store import Store

ROLES = ("owner", "admin", "member")


class Teams:
    def __init__(self, data: Path):
        self.data = Path(data)
        self.path = self.data / "teams.json"
        self.lock = threading.Lock()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, d):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def names(self) -> list[str]:
        return sorted(self._load())

    def of(self, user) -> tuple[str | None, str | None]:
        for name, t in self._load().items():
            if user in t["members"]:
                return name, t["members"][user]
        return None, None

    def add(self, team, user, role="member") -> dict:
        if role not in ROLES:
            raise ValueError(f"role is one of {', '.join(ROLES)}")
        with self.lock:
            d = self._load()
            for name, t in d.items():
                if name != team and user in t["members"]:
                    raise ValueError(f"{user} is already in the team {name}")
            t = d.setdefault(team, {"key": secrets.token_hex(16), "members": {}, "people_visible": False})
            if not t["members"]:
                role = "owner"           # a team's first member owns it
            t["members"][user] = role
            self._save(d)
            return t

    def remove(self, team, user) -> bool:
        with self.lock:
            d = self._load()
            if user not in d.get(team, {}).get("members", {}):
                return False
            del d[team]["members"][user]
            self._save(d)
            return True

    def get(self, team) -> dict:
        return self._load().get(team) or {}

    def set(self, team, **kw):
        with self.lock:
            d = self._load()
            d[team].update(kw)
            self._save(d)

    def store(self, team) -> Store:
        return Store(self.data / "teams" / f"{team}.db")

    def sees_people(self, team, role) -> bool:
        return role in ("owner", "admin") or bool(self.get(team).get("people_visible"))


def copy_history(user_store, team_store, person):
    """A new member's usage so far, into the team's store."""
    from .store import SYNCED_USAGE
    cols = SYNCED_USAGE
    rows = [list(r) + [person] for r in user_store.conn.execute(f"SELECT {','.join(cols)} FROM usage")]
    if rows:
        team_store.upsert_usage(cols + ["person"], rows)


def summary(team_store, team: dict, role: str, sees_people: bool, now, days=30) -> dict:
    """What a member sees: the team's projects (shared names), budgets with forecasts, and, if allowed, people."""
    from . import forecast
    since = now - days * 86400
    names = team_store.meta("shared_names") or {}
    projects = [{"id": r[0], "name": names.get(r[0]), "usd": r[1], "tokens": r[2], "people": r[3]}
                for r in team_store.conn.execute(
                    "SELECT project_id, COALESCE(SUM(cost_usd), 0), SUM(input + cache_read + cache_write_5m"
                    " + cache_write_1h + output), COUNT(DISTINCT person) FROM usage WHERE ts >= ?"
                    " AND project_id IS NOT NULL GROUP BY project_id ORDER BY 2 DESC", (since,))]
    total = team_store.conn.execute("SELECT COALESCE(SUM(cost_usd), 0), COALESCE(SUM(input + cache_read"
                                    " + cache_write_5m + cache_write_1h + output), 0) FROM usage WHERE ts >= ?",
                                    (since,)).fetchone()
    out = {"role": role, "members": len(team["members"]), "days": days, "usd": total[0], "tokens": total[1],
           "projects": projects, "budgets": [o for o in forecast.outlook(team_store, now, goals=True)
                                             if o["kind"] == "goal"]}
    if role in ("owner", "admin"):
        out["goals"] = team_store.meta("goals") or {}
    if sees_people:
        out["people"] = [{"person": r[0], "usd": r[1], "tokens": r[2]} for r in team_store.conn.execute(
            "SELECT person, COALESCE(SUM(cost_usd), 0), SUM(input + cache_read + cache_write_5m + cache_write_1h"
            " + output) FROM usage WHERE ts >= ? GROUP BY person ORDER BY 2 DESC", (since,))]
    return out
