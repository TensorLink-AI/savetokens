"""Ephemeris (the default forecaster): its key, credits and refresh cadence. Gnomon makes the calls.

What leaves the machine (or the sync server): one series of hourly totals, the %
of the weekly limit used each hour. No tokens, prompts, models, projects or
sessions. Calls are made in the background, never from inside a hook: hourly
while you work, every 3 hours otherwise, at once when demand breaks above the
forecast, and not at all while nothing changes (see maintain.refresh_due).
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

from .store import load_config

SITE = "https://ephemeris.cascade.industries"
API = f"{SITE}/api/v1"
ACTIVE_SECONDS = 3600           # refresh this often while agents are in use
REFRESH_SECONDS = 3 * 3600      # and this often otherwise
MAX_AGE_SECONDS = 12 * 3600     # even with no new usage, so forecasts keep reaching the window's end
BREAKOUT_MIN_SECONDS = 15 * 60  # a breakout refreshes at once, but not more often than this
KEY_NAMES = ("EPHEMERIS_API_KEY", "EPHEMERIS_API_TOKEN")
# one model at the lowest price: about 1/13 of the whole ensemble's credits. "ensemble" runs every model.
DEFAULT_MODEL = "toto2-313m"


def model(cfg=None) -> str:
    """The Ephemeris model savetokens forecasts with (config `ephemeris_model`), or "ensemble"."""
    cfg = cfg or load_config()
    return cfg.get("ephemeris_model") or DEFAULT_MODEL


def api_key(cfg=None) -> str | None:
    """From the environment, else from the env file named in config (the key is never copied)."""
    for name in KEY_NAMES:
        if os.environ.get(name):
            return os.environ[name]
    cfg = cfg or load_config()
    path = cfg.get("ephemeris_env_file")
    if not path:
        return None
    try:
        for line in Path(path).expanduser().read_text().splitlines():
            name, _, value = line.strip().removeprefix("export ").partition("=")
            if name.strip() in KEY_NAMES and value.strip():
                return value.strip().strip("'\"")
    except OSError:
        return None
    return None


def _call(path, key, body=None, timeout=60, retries=0):
    """retries: how many times to wait out a 429 (honouring Retry-After) or a network error."""
    import urllib.error
    req = urllib.request.Request(f"{API}/{path}", data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                 method="POST" if body else "GET")
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == retries:
                raise
            try:
                wait = float(e.headers.get("Retry-After") or 0)
            except ValueError:
                wait = 0
            time.sleep(min(120.0, max(wait, 5.0 * 2 ** attempt)))
        except (urllib.error.URLError, TimeoutError, ConnectionError):   # network blips
            if attempt == retries:
                raise
            time.sleep(min(120.0, 5.0 * 2 ** attempt))


def enabled(cfg=None) -> bool:
    """Ephemeris is the default forecaster; it runs whenever a key is available and the user hasn't opted out."""
    cfg = cfg or load_config()
    return cfg.get("forecaster", "ephemeris") == "ephemeris" and api_key(cfg) is not None


def save_key(key: str, cfg: dict) -> Path:
    """Store a pasted key in savetokens' own home (owner-only) and point the config at it."""
    from .store import home
    path = home() / "ephemeris.env"
    home().mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600)
    path.chmod(0o600)
    path.write_text(f"EPHEMERIS_API_KEY={key.strip()}\n")
    cfg["ephemeris_env_file"] = str(path)
    return path


def balance(key) -> float:
    """Spendable credits."""
    b = _call("balance", key)
    return (int(b["balance_mc"]) - int(b["active_holds_mc"])) / 1000


TOPUP = f"{SITE}/dashboard/billing"


def problem(store) -> dict | None:
    """Ephemeris's last call failed and none has worked since: {"kind": credits|key|other, "error", "at"}."""
    err, last = store.meta("ephemeris_error"), store.meta("ephemeris_last")
    if not err or (last and last["made_at"] >= err["at"]):
        return None
    text = err["error"].lower()
    kind = ("credits" if "402" in text or "insufficient" in text or "credit" in text else
            "key" if "401" in text or "403" in text or "unauthorized" in text or "forbidden" in text else "other")
    return {"kind": kind, "error": err["error"], "at": err["at"]}


def problem_text(p) -> str:
    return {"credits": f"Ephemeris credits ran out, so forecasts are local until you top up ({TOPUP})",
            "key": "Ephemeris didn't accept the key, so forecasts are local (savetokens setup to sign in again)",
            }.get(p["kind"], f"Ephemeris failed ({p['error'][:80]}), so forecasts are local for now")


# ── sign in from the terminal (OAuth device flow, RFC 8628) ──────────────────

class NoDeviceLogin(Exception):
    """This Ephemeris doesn't offer signing in from the terminal yet: paste a key instead."""


def _public(path, body, timeout=30):
    """(status, json) for an unauthenticated POST."""
    import urllib.error
    req = urllib.request.Request(f"{API}/{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, {}


def device_login(out=print, open_url=None, sleep=time.sleep, clock=time.time, name=None) -> str:
    """Sign in in the browser and get a new key back: show a code, open the approval page, wait.
    Raises NoDeviceLogin when the server can't, RuntimeError when it's denied or expires."""
    import platform
    status, got = _public("device/code", {"client": "savetokens",
                                          "name": name or f"savetokens on {platform.node() or 'this machine'}"})
    if status in (404, 405) or "device_code" not in got:
        raise NoDeviceLogin(got.get("error") or f"HTTP {status}")
    url = got.get("verification_uri_complete") or got["verification_uri"]
    out(f"  Your code: {got['user_code']}  (check the page shows the same code, then approve)\n  {url}")
    if open_url:
        try:
            open_url(url)
        except Exception:
            pass
    interval, until = float(got.get("interval") or 5), clock() + float(got.get("expires_in") or 600)
    while clock() < until:
        sleep(interval)
        status, r = _public("device/token", {"device_code": got["device_code"]})
        if status == 200 and r.get("key"):
            return r["key"]
        error = r.get("error")
        if error == "slow_down":
            interval += 5
        elif error != "authorization_pending":
            raise RuntimeError({"access_denied": "you didn't approve it", "expired_token": "the code expired"}
                               .get(error, error or f"HTTP {status}"))
    raise RuntimeError("the code expired")
