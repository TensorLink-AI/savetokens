"""Canned fixes: each shows what it changes, applies only with consent, and reverts exactly."""
from __future__ import annotations

import json
import random
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from .adapters.claude_code import claude_home
from .store import Store

BLOCK_START = "<!-- savetokens:output-hygiene -->"
BLOCK_END = "<!-- /savetokens:output-hygiene -->"
HYGIENE = f"""{BLOCK_START}
- Cap long command output: pipe through `tail -n 50`, `head` or `grep` rather than printing everything.
- Don't re-read a file you already have in context unless it changed.
{BLOCK_END}
"""


@dataclass
class Fix:
    id: str
    harness: str
    summary: str
    kind: str            # env | setting | claude_md
    key: str | None = None
    value: str | None = None


FIXES = {f.id: f for f in (
    Fix("subagent-model", "claude-code", "Run subagents on Sonnet 5.5 (CLAUDE_CODE_SUBAGENT_MODEL)",
        "env", "CLAUDE_CODE_SUBAGENT_MODEL", "claude-sonnet-5-5"),
    Fix("bash-output-cap", "claude-code", "Cap shell output kept in context at 15k characters",
        "env", "BASH_MAX_OUTPUT_LENGTH", "15000"),
    Fix("mcp-output-cap", "claude-code", "Cap MCP tool output at 10k tokens",
        "env", "MAX_MCP_OUTPUT_TOKENS", "10000"),
    Fix("output-hygiene", "claude-code", "Two lines in ~/.claude/CLAUDE.md: cap command output, don't re-read files",
        "claude_md"),
    Fix("autocompact-window", "claude-code", "Auto-compact earlier (autoCompactWindow), at the size you compact by hand",
        "setting", "autoCompactWindow"),
)}


def _value(f: Fix, store: Store | None):
    if f.id == "autocompact-window":
        from . import compaction
        return compaction.suggested_window(store)[0] if store else compaction.DEFAULT_SUGGESTION
    return f.value


def _why(f: Fix, store: Store | None) -> str:
    if f.id != "autocompact-window" or store is None:
        return ""
    from . import compaction
    window, n, med = compaction.suggested_window(store)
    turns, usd = compaction.carry_above(store, window)
    basis = (f"you ran /compact {n} times at a median of {med / 1000:.0f}k tokens" if med
             else "default suggestion (fewer than 3 manual compactions on record)")
    current = compaction.configured_window()
    return (f"\n  why: {basis}; currently {'auto' if current is None else f'{current:,}'}."
            f"\n  in your history, {turns:,} turns ran above {window // 1000}k, re-reading ≈ ${usd:,.0f}"
            " (API-equivalent, an upper bound: each compaction costs a summary pass and a cache rebuild)."
            "\n  trade-off: the model keeps a summary instead of the full history; raise it if it loses track.")


def settings_path() -> Path:
    return claude_home() / "settings.json"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".savetokens.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def plan(fix_id: str, store: Store | None = None) -> str:
    f = FIXES[fix_id]
    if f.kind == "setting":
        cur = _read_json(settings_path()).get(f.key)
        return f"{settings_path()}: {f.key} {cur!r} -> {_value(f, store)!r}{_why(f, store)}"
    if f.kind == "env":
        cur = _read_json(settings_path()).get("env", {}).get(f.key)
        return f"{settings_path()}: env.{f.key} {cur!r} -> {f.value!r}"
    return f"{claude_home() / 'CLAUDE.md'}: append\n{HYGIENE}"


def active(store: Store):
    return {r["fix_id"]: r for r in store.conn.execute("SELECT rowid, * FROM fixes WHERE reverted_at IS NULL")}


def apply(store: Store, fix_id: str):
    f = FIXES[fix_id]
    if fix_id in active(store):
        raise ValueError(f"{fix_id} is already applied")
    if f.kind == "setting":
        path = settings_path()
        data = _read_json(path)
        backup = {"had": f.key in data, "value": data.get(f.key)}
        data[f.key] = _value(f, store)
        _write_json(path, data)
    elif f.kind == "env":
        path = settings_path()
        data = _read_json(path)
        env = data.setdefault("env", {})
        backup = {"had": f.key in env, "value": env.get(f.key)}
        env[f.key] = f.value
        _write_json(path, data)
    else:
        path = claude_home() / "CLAUDE.md"
        text = path.read_text() if path.exists() else ""
        backup = {"existed": path.exists()}
        if BLOCK_START not in text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + ("\n" if text and not text.endswith("\n") else "") + HYGIENE)
    store.conn.execute("INSERT INTO fixes (fix_id, target, applied_at, backup_json) VALUES (?,?,?,?)",
                       (fix_id, str(path), time.time(), json.dumps(backup)))
    store.conn.commit()


def revert(store: Store, fix_id: str):
    row = active(store).get(fix_id)
    if not row:
        raise ValueError(f"{fix_id} is not applied")
    f = FIXES[fix_id]
    backup = json.loads(row["backup_json"])
    path = Path(row["target"])
    if f.kind == "setting":
        data = _read_json(path)
        if backup["had"]:
            data[f.key] = backup["value"]
        else:
            data.pop(f.key, None)
        _write_json(path, data)
    elif f.kind == "env":
        data = _read_json(path)
        env = data.setdefault("env", {})
        if backup["had"]:
            env[f.key] = backup["value"]
        else:
            env.pop(f.key, None)
            if not env:
                data.pop("env")
        _write_json(path, data)
    elif path.exists():
        text = path.read_text()
        start, end = text.find(BLOCK_START), text.find(BLOCK_END)
        if start != -1 and end != -1:
            text = text[:start] + text[end + len(BLOCK_END):].lstrip("\n")
        if not text.strip() and not backup["existed"]:
            path.unlink()
        else:
            path.write_text(text)
    store.conn.execute("UPDATE fixes SET reverted_at = ? WHERE rowid = ?", (time.time(), row["rowid"]))
    store.conn.commit()


def _per_turn(store, since, until, harness="claude-code"):
    """Median-friendly metric: dollars per main-agent turn, one value per session."""
    per = {}
    for e in store.usage(since=since, until=until, harness=harness):
        if e.cost_usd is None:
            continue
        s = per.setdefault(e.session_id, [0.0, 0])
        s[0] += e.cost_usd
        s[1] += 0 if e.subagent else 1
    return [usd / n for usd, n in per.values() if n >= 5]


def impact(store: Store, fix_id: str, days=14, seed=0):
    """Observational before/after: ratio of mean $/turn per session, with a bootstrap 90% interval."""
    row = store.conn.execute("SELECT * FROM fixes WHERE fix_id = ? ORDER BY applied_at DESC LIMIT 1",
                             (fix_id,)).fetchone()
    if not row:
        return None
    t = row["applied_at"]
    before = _per_turn(store, t - days * 86400, t)
    after = _per_turn(store, t, row["reverted_at"] or time.time())
    if len(before) < 3 or len(after) < 3:
        return {"before": len(before), "after": len(after), "ratio": None}
    rng = random.Random(seed)
    ratios = sorted(statistics.mean(rng.choices(after, k=len(after)))
                    / statistics.mean(rng.choices(before, k=len(before))) for _ in range(2000))
    return {"before": len(before), "after": len(after),
            "ratio": statistics.mean(after) / statistics.mean(before), "lo": ratios[100], "hi": ratios[1899]}
