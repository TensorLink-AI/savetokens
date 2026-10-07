"""Steering: what savetokens tells the agent, set by one quality knob.

Modes (`savetokens mode <mode>`, or SAVETOKENS_MODE for one session or run):

  quality   Monitoring only. Nothing is added to the agent's context except guard
            warnings, and those fire later (looser thresholds).
  balanced  Default. A short briefing at session start with this repo's lessons;
            budget figures from a 20% chance of hitting a limit, a nudge from 50%.
  lean      The briefing also asks for economical work; nudges from a 20% chance;
            tighter guard thresholds.
  auto      balanced, switching to lean from a 30% chance of hitting a limit before
            it resets, and to quality when there is plenty of headroom (unused
            subscription quota is wasted, so there is no point economising).

The chance comes from the forecast's sample paths (Ephemeris when connected), which
is where a calibrated forecast pays: `savetokens backtest` replays your usage to show
how many limit hits each forecaster would have avoided.

Everything injected is short, because it lands in the agent's context and costs
tokens on every later turn.
"""
from __future__ import annotations

import os
import time
from collections import Counter, defaultdict
from pathlib import Path

from .store import Store, load_config

MODES = ("quality", "balanced", "lean", "auto")
PROFILES = {
    # budget_from / nudge_at: chance of hitting a limit before it resets (from the forecast's sample paths)
    "quality": {"briefing": False, "budget_from": None, "nudge_at": None, "economy": False},
    "balanced": {"briefing": True, "budget_from": 0.2, "nudge_at": 0.5, "economy": False},
    "lean": {"briefing": True, "budget_from": 0.0, "nudge_at": 0.2, "economy": True},
}
AUTO_LEAN_RISK = 0.3      # auto economises from this chance of a hit (the backtest's pre-set headline rule)
AUTO_QUALITY_BELOW = 50   # ... and relaxes to quality when every limit is expected below this % with ~no risk
NUDGE_STEPS = (0.2, 0.5, 0.8)
LESSON_DAYS = 30
BIG_OUTPUT = 15_000    # characters of tool output that count as big (Claude Code caps Bash at 30k)
ECONOMY = ("Work economically without cutting corners on correctness: batch independent tool calls"
           " into one turn; read only the parts of files you need (grep -n, offset/limit) and don't"
           " re-read files already in context; run the narrowest relevant tests quietly (-q, tail long"
           " output) and the wider suite once at the end; delegate broad searches to a subagent; keep"
           " explanations and the final summary brief.")
WINDOW_NAMES = {"five_hour": "5-hour limit", "week": "weekly limit"}
SUBAGENT_HINT = ('When you start subagents for search, exploration or other routine work, pass model: "sonnet"'
                 " to the Agent tool; keep the stronger model for the hard parts.")


def economy(harness="claude-code", cfg=None) -> str:
    """The economy request, plus the Claude Code subagent hint when the user allows that lever."""
    from . import levers
    allowed_ids = levers.allowed(cfg)
    if harness == "claude-code" and ("subagents" in allowed_ids or "claude-code:subagents" in allowed_ids):
        return f"{ECONOMY} {SUBAGENT_HINT}"
    return ECONOMY


def configured_mode(cfg: dict | None = None) -> str:
    m = os.environ.get("SAVETOKENS_MODE") or (cfg or load_config()).get("mode") or "balanced"
    return m if m in MODES else "balanced"


def adjust_guard(cfg: dict, mode: str | None = None) -> dict:
    """Guard thresholds for the mode. Configured values are the balanced baseline."""
    mode = mode or cfg.get("_mode") or "balanced"
    out = dict(cfg)
    if mode == "quality":
        out.update(loop_repeats=cfg["loop_repeats"] + 1, reread_limit=cfg["reread_limit"] * 2,
                   failing_tests=cfg["failing_tests"] + 1, context_tokens=int(cfg["context_tokens"] * 1.5))
    elif mode == "lean":
        out.update(loop_repeats=max(2, cfg["loop_repeats"] - 1), reread_limit=max(2, cfg["reread_limit"] - 1),
                   failing_tests=max(2, cfg["failing_tests"] - 1), context_tokens=int(cfg["context_tokens"] * 0.6))
    return out


# ── budget pressure ──────────────────────────────────────────────────────────

def pressure(store: Store, now=None, session_id=None, cfg=None, harness="claude-code") -> list[dict]:
    """Limits and budgets that matter now for a harness: used and forecast % at reset, worst first.

    Claude Code: its 5-hour and weekly subscription limits, plus budgets that cover it.
    Hermes and other API-billed harnesses: their dollar budgets (see budgets.py).
    harness=None: everything.
    """
    from . import budgets, forecast, windows
    cfg = cfg or load_config()
    now = now or time.time()
    out = []
    if harness in ("claude-code", None):
        src = forecast.preferred_source(store)
        for r in windows.forecasts(store, now, session_id, sources=(src,)):
            p = r["pct"]
            if r["kind"] in WINDOW_NAMES and p and p["account"] is not None:
                fc = p["forecast"].get(src)
                out.append({"window": r["kind"], "name": WINDOW_NAMES[r["kind"]], "unit": "%",
                            "used": round(p["account"], 1), "forecast": [round(v, 1) for v in fc] if fc else None,
                            "p_hit": round(p["p_hit"].get(src, 0.0), 2) if fc else None, "source": src,
                            "resets": r["end"],
                            "session": round(p["session"], 2) if p["session"] is not None else None})
    try:
        out += budgets.pressure_rows(store, now, harness)
    except Exception:   # a budget problem must never break steering
        pass
    return sorted(out, key=lambda w: (-(_risk(w)), -_expected(w)))


def _risk(w) -> float:
    """Chance of hitting the limit before it resets; falls back to the forecast median when unknown."""
    if w.get("p_hit") is not None:
        return w["p_hit"]
    return 1.0 if _expected(w) >= 100 else 0.0


def _expected(w) -> float:
    return w["forecast"][1] if w["forecast"] else w["used"]


def effective_mode(store: Store, now=None, cfg=None, windows_=None) -> tuple[str, str]:
    """(mode, reason). Resolves auto from the limit forecasts and caches the result for the guard."""
    cfg = cfg or load_config()
    mode = configured_mode(cfg)
    if mode != "auto":
        return mode, "set"
    ws = windows_ if windows_ is not None else pressure(store, now, cfg=cfg)
    if not ws:
        resolved, why = "balanced", "auto: no limit readings yet"
    else:
        worst = ws[0]
        e, risk = _expected(worst), _risk(worst)
        if risk >= AUTO_LEAN_RISK:
            resolved, why = "lean", f"auto: {risk:.0%} chance of hitting the {worst['name']} before it resets"
        elif all(_expected(w) < AUTO_QUALITY_BELOW and _risk(w) < 0.02 for w in ws):
            resolved, why = "quality", f"auto: plenty of headroom ({worst['name']} forecast {e:.0f}%)"
        else:
            resolved, why = "balanced", f"auto: {worst['name']} forecast {e:.0f}%, {risk:.0%} chance of a hit"
    try:
        store.set_meta("steer_mode", {"mode": resolved, "reason": why, "ts": time.time()})
    except Exception:
        pass
    return resolved, why


def guard_mode(store: Store, cfg: dict) -> str:
    """Mode for guard thresholds, cheap enough for every tool call (auto uses the cached resolution)."""
    mode = configured_mode(cfg)
    if mode != "auto":
        return mode
    cached = store.meta("steer_mode") or {}
    return cached.get("mode") if cached.get("ts", 0) > time.time() - 3 * 3600 else "balanced"


def _when(ts, now) -> str:
    from datetime import datetime
    d = datetime.fromtimestamp(ts)
    return d.strftime("%H:%M") if ts - now < 20 * 3600 else d.strftime("%a %H:%M")


def _window_text(w, now) -> str:
    if w["forecast"]:
        chance = f", {w['p_hit']:.0%} chance of hitting it" if w.get("p_hit") else ""
        return (f"{w['name']} {w['used']:.0f}% used, ~{w['forecast'][1]:.0f}% expected by"
                f" {_when(w['resets'], now)}{chance}")
    return f"{w['name']} {w['used']:.0f}% used, resets {_when(w['resets'], now)}"


# ── lessons from this repo's history ────────────────────────────────────────

def lessons(store: Store, cwd: str | None, now=None, harness="claude-code") -> list[str]:
    """Up to three one-line lessons from past sessions in the same project. Empty when there is no evidence."""
    if not cwd:
        return []
    now = now or time.time()
    project = Path(cwd).name
    sessions = {r[0] for r in store.conn.execute(
        "SELECT DISTINCT session_id FROM usage WHERE project = ? AND harness = ? AND ts >= ?",
        (project, harness, now - LESSON_DAYS * 86400))}
    if len(sessions) < 2:
        return []
    rows = [r for r in store.conn.execute(
        "SELECT session_id, kind, target, output_chars FROM tools WHERE harness = ? AND ts >= ? ORDER BY ts, id",
        (harness, now - LESSON_DAYS * 86400)) if r[0].split("/")[0] in sessions]
    big = defaultdict(set)          # kind -> sessions with a big output
    biggest = Counter()
    reads = defaultdict(Counter)     # session -> target -> reads since last edit
    reread_sessions = defaultdict(set)
    big_files = {}                   # file -> largest read
    for sid, kind, target, chars in rows:
        root = sid.split("/")[0]
        if chars and chars >= BIG_OUTPUT and kind in ("test", "bash", "read", "search"):
            big[kind].add(root)
            biggest[kind] = max(biggest[kind], chars)
            if kind == "read" and target and _inside(target, cwd):
                big_files[target] = max(big_files.get(target, 0), chars)
        if target and kind == "edit":
            reads[sid][target] = 0
        elif target and kind == "read":
            reads[sid][target] += 1
            if reads[sid][target] >= 4:
                reread_sessions[target].add(root)
    out = []
    if len(big["test"]) >= 2:
        out.append(f"Test runs here have printed up to {biggest['test'] // 1000}k characters: run the specific"
                   " tests with -q (or -x) and tail long output.")
    if len(big["bash"]) >= 2:
        out.append(f"Shell commands here have printed up to {biggest['bash'] // 1000}k characters: filter"
                   " output with grep, head or tail.")
    if len(big["read"]) >= 2 and big_files:
        top = sorted(big_files, key=big_files.get, reverse=True)[:3]
        out.append(f"Large files ({', '.join(_rel(t, cwd) for t in top)}): grep first, then read only the"
                   " relevant range (offset/limit).")
    hot = sorted(((len(s), t) for t, s in reread_sessions.items() if len(s) >= 2 and _inside(t, cwd)),
                 reverse=True)[:3]
    if hot:
        names = ", ".join(_rel(t, cwd) for _, t in hot)
        out.append(f"{names} tend to get re-read many times per session here; read once and work from context.")
    return out[:3]


def _inside(path, cwd) -> bool:
    try:
        Path(path).relative_to(cwd)
        return True
    except ValueError:
        return False


def _rel(path, cwd) -> str:
    try:
        return str(Path(path).relative_to(cwd))
    except ValueError:
        return Path(path).name


# ── what the agent sees ──────────────────────────────────────────────────────

def briefing(store: Store, cwd=None, session_id=None, now=None, cfg=None, harness="claude-code") -> str | None:
    """Session-start context for the agent, or None when there is nothing worth its tokens."""
    cfg = cfg or load_config()
    now = now or time.time()
    ws = pressure(store, now, session_id, cfg, harness=harness)
    mode, why = effective_mode(store, now, cfg, ws)
    prof = PROFILES[mode]
    if not prof["briefing"]:
        return None
    lines = []
    shown = [w for w in ws if prof["budget_from"] is not None and _risk(w) >= prof["budget_from"]]
    if shown:
        lines.append("Budget: " + "; ".join(_window_text(w, now) for w in shown[:2]) + ".")
    lines += lessons(store, cwd, now, harness)
    if prof["economy"]:
        lines.append(economy(harness, cfg))
    if not lines:
        return None
    text = f"savetokens ({mode} mode): " + " ".join(lines)
    if session_id:   # recorded so reports and evals can see what steering was given
        store.add_alert(harness, session_id, "briefing", f"{mode}:{int(now)}", "brief", text, ts=now)
    return text


HEAVY_CONTEXT = {"quality": 800_000, "balanced": 500_000, "lean": 300_000}
SHORT_PROMPT = 200          # characters: a follow-up, not a new brief
CACHE_COLD = 3600           # seconds after which the prompt cache has expired


def heavy_followup(store: Store, session_id, prompt, now=None, cfg=None) -> str | None:
    """A short prompt on a huge context re-reads all of it, and after a break re-caches it too.

    Found by the study (tiny follow-ups on 600k+ contexts, $3–8 a turn); a fixed rule, told to the
    user once per session per 100k of context. The agent can't /clear, so this never goes to it.
    """
    from . import pricing
    cfg = cfg or load_config()
    now = now or time.time()
    main = [e for e in store.usage(session_id=session_id) if not e.subagent]
    if not main or len(prompt or "") > SHORT_PROMPT:
        return None
    last = main[-1]
    mode, _ = effective_mode(store, now, cfg)
    ctx = last.context_tokens
    if ctx < HEAVY_CONTEXT.get(mode, HEAVY_CONTEXT["balanced"]):
        return None
    rates = pricing.rates(last.model)
    if not rates:
        return None
    cold = now - last.ts > CACHE_COLD
    per_mtok = rates[0] * (2.0 if last.cache_write_1h else 1.25) if cold else rates[2]
    usd = ctx * per_mtok / 1e6
    if not store.add_alert("claude-code", session_id, "heavy_followup", f"{ctx // 100_000}:{int(cold)}", "warn",
                           f"heavy follow-up at {ctx // 1000}k", ts=now):
        return None
    why = " and rebuild the expired cache" if cold else ""
    return (f"savetokens: this short prompt will re-read {ctx // 1000}k tokens of context{why} (about ${usd:.2f}"
            f" for this turn alone, more for each tool call). If it's a new task, /clear first; if it continues the"
            f" same work, /compact keeps the thread for much less.")


def nudge(store: Store, session_id, harness="claude-code", now=None, cfg=None) -> tuple[str, str] | None:
    """(to agent, to user) once per session per 10% step, when a limit is forecast to be hit."""
    cfg = cfg or load_config()
    now = now or time.time()
    ws = pressure(store, now, session_id, cfg, harness=harness)
    mode, _ = effective_mode(store, now, cfg, ws)
    at = PROFILES[mode]["nudge_at"]
    if at is None:
        return None
    hot = [w for w in ws if w["forecast"] and _risk(w) >= at]
    if not hot:
        return None
    w = hot[0]
    key = f"{w['window']}:{sum(_risk(w) >= s for s in NUDGE_STEPS)}"
    text = f"savetokens: {_window_text(w, now)}."
    if not store.add_alert(harness, session_id or "unknown", "budget", key, "warn", text, ts=now):
        return None
    return f"{text} {economy(harness, cfg)}", text


def budget(store: Store, session_id=None, now=None, cfg=None) -> dict:
    """Machine-readable budget for agents (`savetokens budget --json`)."""
    cfg = cfg or load_config()
    now = now or time.time()
    ws = pressure(store, now, session_id, cfg, harness=None)
    mode, why = effective_mode(store, now, cfg, ws)
    from . import levers
    from .adapters import codex
    tight = PROFILES[mode]["economy"] or (ws and _risk(ws[0]) >= AUTO_LEAN_RISK)
    advice = economy("claude-code", cfg) if tight else None
    try:
        other = codex.pressure(store, now)
    except Exception:
        other = []
    return {"mode": mode, "configured_mode": configured_mode(cfg), "mode_reason": why,
            "limits": [{**w, "resets_in_min": int((w["resets"] - now) // 60)} for w in ws + other],
            "advice": advice, "levers_active": levers.active(store), "levers_allowed": levers.allowed(cfg)}
