"""What each model costs, in USD per million tokens, from data rather than code.

Where a price comes from, first match wins:
  1. yours: `savetokens price MODEL INPUT OUTPUT [CACHE_READ]`, kept in the config
  2. the catalogue: every model OpenRouter lists, from every provider (input, output, cache read and
     write, and the long-context tier where there is one), refreshed daily into ~/.savetokens/prices.json
  3. built in: Claude's list prices, for when the catalogue hasn't been fetched (offline, first run)

For subscription users these dollars are API-equivalent, not what they are billed. A model with no
price anywhere has no cost (None), never a guessed one.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass

CATALOGUE_URL = "https://openrouter.ai/api/v1/models"
REFRESH_SECONDS = 86400
FAST_MULTIPLIER = 2.0   # Claude's fast mode: twice the price


@dataclass(frozen=True)
class Rate:
    """$ per million tokens. long: (prompt tokens above which, the Rate then), when the model has a tier."""
    input: float
    output: float
    cache_read: float
    cache_write: float
    cache_write_1h: float
    long: tuple | None = None
    source: str = "builtin"


def _claude(inp, out, read):
    return Rate(inp, out, read, inp * 1.25, inp * 2.0)   # Anthropic: 5-minute writes 1.25x, 1-hour 2x


BUILTIN = {
    "claude-fable-5-1": _claude(10.0, 50.0, 0.25),
    "claude-mythos-5-1": _claude(10.0, 50.0, 0.25),
    "claude-fable-5": _claude(10.0, 50.0, 1.0),
    "claude-mythos-5": _claude(10.0, 50.0, 1.0),
    "claude-opus-5-5": _claude(4.0, 20.0, 0.20),
    "claude-opus-5": _claude(5.0, 25.0, 0.50),
    "claude-opus-4-8": _claude(5.0, 25.0, 0.50),
    "claude-opus-4-7": _claude(5.0, 25.0, 0.50),
    "claude-opus-4-6": _claude(5.0, 25.0, 0.50),
    "claude-opus-4-5": _claude(5.0, 25.0, 0.50),
    "claude-opus-4-1": _claude(15.0, 75.0, 1.50),
    "claude-opus-4": _claude(15.0, 75.0, 1.50),
    "claude-sonnet-5-5": _claude(2.0, 10.0, 0.20),
    "claude-sonnet-5": _claude(2.0, 10.0, 0.20),
    "claude-sonnet-4": _claude(3.0, 15.0, 0.30),
    "claude-haiku-4-5": _claude(1.0, 5.0, 0.10),
    "claude-3-5-haiku": _claude(0.8, 4.0, 0.08),
}


def normalize(model: str | None) -> str:
    """Strip provider prefixes and context suffixes: 'anthropic/claude-opus-5.5[1m]' -> 'claude-opus-5-5'."""
    m = (model or "").lower().strip()
    m = m.rsplit("/", 1)[-1]
    m = re.sub(r"\[.*?\]$", "", m)
    m = m.replace(".", "-").removeprefix("anthropic-").removeprefix("us-anthropic-")
    return m


# ── sources ──────────────────────────────────────────────────────────────────

_extra = _catalogue = None


def extra() -> dict:
    """Prices you added: {model prefix: Rate}. Without a cache price, a tenth of input (the common rate);
    cache writes at the input price (no premium is assumed)."""
    global _extra
    if _extra is None:
        from .store import load_config
        _extra = {}
        for k, v in (load_config().get("prices") or {}).items():
            inp, out = float(v[0]), float(v[1])
            read = float(v[2]) if len(v) > 2 else inp / 10
            _extra[normalize(k)] = Rate(inp, out, read, inp, inp, source="yours")
    return _extra


def _path():
    from .store import home
    return home() / "prices.json"


def _per_million(v):
    try:
        return round(float(v) * 1e6, 6)
    except (TypeError, ValueError):
        return None


def parse_catalogue(models: list) -> dict:
    """OpenRouter's /models list -> {normalized name: Rate}. Variants (':free', ':batch') are left out."""
    out = {}
    for m in models:
        mid, p = m.get("id") or "", m.get("pricing") or {}
        if ":" in mid:
            continue
        inp, outp = _per_million(p.get("prompt")), _per_million(p.get("completion"))
        if inp is None or outp is None or (inp == 0 and outp == 0):
            continue
        read = _per_million(p.get("input_cache_read"))
        write = _per_million(p.get("input_cache_write"))
        write1h = _per_million(p.get("input_cache_write_1h"))
        rate = Rate(inp, outp, inp if read is None else read, inp if write is None else write,
                    (write if write is not None else inp) if write1h is None else write1h, source="catalogue")
        for o in p.get("overrides") or []:
            if o.get("min_prompt_tokens") and _per_million(o.get("prompt")) is not None:
                lr = Rate(_per_million(o["prompt"]), _per_million(o.get("completion")) or outp,
                          _per_million(o.get("input_cache_read")) or rate.cache_read,
                          _per_million(o.get("input_cache_write")) or rate.cache_write,
                          _per_million(o.get("input_cache_write_1h")) or rate.cache_write_1h, source="catalogue")
                rate = Rate(*[getattr(rate, f) for f in ("input", "output", "cache_read", "cache_write",
                                                         "cache_write_1h")], long=(int(o["min_prompt_tokens"]), lr),
                            source="catalogue")
                break
        out.setdefault(normalize(mid), rate)
    return out


def catalogue() -> dict:
    global _catalogue
    if _catalogue is None:
        try:
            d = json.loads(_path().read_text())
            _catalogue = {k: Rate(*v[:5], long=(v[5][0], Rate(*v[5][1], source="catalogue")) if v[5] else None,
                                  source="catalogue") for k, v in d["models"].items()}
        except (OSError, ValueError, KeyError, TypeError):
            _catalogue = {}
    return _catalogue


def _fetch():
    req = urllib.request.Request(CATALOGUE_URL, headers={"User-Agent": "savetokens"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r).get("data") or []


def refresh(force=False, now=None) -> int:
    """Fetch the catalogue when it's a day old. Returns models known (0: not refreshed). Never raises."""
    global _catalogue
    now = now or time.time()
    try:
        fetched = json.loads(_path().read_text()).get("fetched_at", 0)
    except (OSError, ValueError):
        fetched = 0
    if not force and now - fetched < REFRESH_SECONDS:
        return 0
    try:
        rates = parse_catalogue(_fetch())
    except Exception:
        return 0
    if not rates:
        return 0
    flat = lambda r: [r.input, r.output, r.cache_read, r.cache_write, r.cache_write_1h]
    _path().parent.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps({"fetched_at": now, "source": CATALOGUE_URL,
                                   "models": {k: flat(r) + [[r.long[0], flat(r.long[1])] if r.long else None]
                                              for k, r in rates.items()}}))
    _catalogue = None
    return len(rates)


def catalogue_at() -> float:
    """When the catalogue on disk was fetched (0: none). A long-running hub uses it to notice a newer one."""
    try:
        return json.loads(_path().read_text()).get("fetched_at", 0)
    except (OSError, ValueError):
        return 0


def reprice(store) -> int:
    """Price every request that had no price when it was read, now that one is known. Hermes usage its plan
    includes stays unpriced on purpose. Returns requests priced."""
    rows = store.conn.execute("SELECT rowid, model, input, output, cache_read, cache_write_5m, cache_write_1h"
                              " FROM usage WHERE cost_usd IS NULL AND model IS NOT NULL"
                              " AND NOT (harness = 'hermes' AND billing IS NULL)").fetchall()
    n = 0
    for r in rows:
        c = cost(r["model"], input=r["input"], output=r["output"], cache_read=r["cache_read"],
                 cache_write_5m=r["cache_write_5m"], cache_write_1h=r["cache_write_1h"])
        if c is not None:
            store.conn.execute("UPDATE usage SET cost_usd = ? WHERE rowid = ?", (c, r["rowid"]))
            n += 1
    store.conn.commit()
    return n


def reset():
    global _extra, _catalogue
    _extra = _catalogue = None


# ── lookups ──────────────────────────────────────────────────────────────────

def _match(table: dict, m: str):
    if m in table:
        return table[m]
    for prefix in sorted((k for k in table if m.startswith(k)), key=len, reverse=True):
        return table[prefix]
    return None


def rate(model: str | None) -> Rate | None:
    m = normalize(model)
    if not m:
        return None
    for table in (extra(), catalogue(), BUILTIN):
        r = _match(table, m)
        if r:
            return r
    return None


def rates(model):
    """(input, output, cache_read) $ per million, or None. Kept for callers that only need these three."""
    r = rate(model)
    return (r.input, r.output, r.cache_read) if r else None


def cost(model, *, input=0, output=0, cache_read=0, cache_write_5m=0, cache_write_1h=0, fast=False, at=None):
    """Dollar cost of one request, or None when the model has no known price. `at`: a Rate to price it at
    instead of the model's own (what-ifs)."""
    r = at or rate(model)
    if r is None:
        return None
    if r.long and input + cache_read + cache_write_5m + cache_write_1h > r.long[0]:
        r = r.long[1]
    usd = (input * r.input + output * r.output + cache_read * r.cache_read
           + cache_write_5m * r.cache_write + cache_write_1h * r.cache_write_1h) / 1e6
    return usd * (FAST_MULTIPLIER if fast else 1.0)
