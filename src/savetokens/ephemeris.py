"""Ephemeris forecaster (the default): hourly usage forecasts, scored against a local baseline.

What leaves the machine: hourly API-equivalent dollar totals (one series for
subscription usage, one for API-billed usage). No tokens per request, no
prompts, no project or session names. Calls are made in the background, never
from inside a hook: hourly while agents are in use, every 3 hours otherwise, at
once when usage breaks above the forecast, and not at all while nothing changes
(see maintain.refresh_due).
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


def forecast_hourly(series: dict, horizon: int, key=None, retries=0, freq="H") -> dict:
    """One ensemble call for several hourly series.

    series: {unit: [hourly values, oldest first]}. Returns {"quantiles": {unit: [{level: value} per hour]},
    "credits": float, "models": [...]}. Only these numbers leave the machine.
    """
    from .windows import QUANTILE_GRID
    key = key or api_key()
    if not key:
        raise RuntimeError("no Ephemeris key: set EPHEMERIS_API_KEY or run `savetokens ephemeris connect`")
    units = list(series)
    body = {"mode": "ensemble", "series": [{"values": series[u], "freq": freq} for u in units],
            "horizon": horizon, "quantiles": list(QUANTILE_GRID)}
    result = _call("forecast", key, body, timeout=180, retries=retries)
    out = {}
    for unit, fc in zip(units, result["forecasts"]):
        qs = fc["quantiles"]
        out[unit] = [{p: max(0.0, qs[str(p)][i]) for p in QUANTILE_GRID} for i in range(horizon)]
    meta = result.get("meta", {})
    return {"quantiles": out, "models": meta.get("models_used", []),
            "credits": int((meta.get("billing") or {}).get("settled_mc") or 0) / 1000}
