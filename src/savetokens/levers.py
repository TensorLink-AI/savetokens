"""Levers: what savetokens changes in a harness when a limit is at risk, and how it changes it back.

The decision (when) is shared: the forecast's chance of hitting a limit before it resets.
The levers (how) are per harness. Rules, so a user's task is never derailed:

  - Boundaries only. Levers are written to the settings a harness reads for new sessions
    and new subagents (from upkeep or at session start), never in the middle of a request.
    Inside a running Claude Code session, cheaper subagents are offered through the
    Agent tool's own `model` parameter instead (see steer.nudge).
  - Least risky first: subagents, then reasoning effort, then earlier compaction. The
    main model is changed only if the user allows "main".
  - Switch back on trouble: failing tests or a loop in a session while levers are on
    reverts them, with a cooldown before they can come back.
  - Temporary and exact: reverted when the risk falls (hysteresis) or the window resets,
    restoring the previous values; a value the user changed since is left alone.
  - Consent: only levers listed in config "levers" are used, only after install has
    shown them ("levers_consent"), and only in auto or lean mode.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .store import Store, load_config

APPLY_RISK = 0.3        # the same chance of a hit at which auto turns lean
REVERT_RISK = 0.15      # hysteresis: back off only once the risk is well below
COOLDOWN = 30 * 60      # no new change this soon after the last one
# compaction is opt-in: the long-session eval (5 chains of 6 tasks) found earlier compaction saved nothing at
# 100–150k (the summary costs about what it saves), while clearing between tasks saved 69%
DEFAULT_LEVERS = ("subagents", "effort", "side-tasks", "quality-score")
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
MISSING = "__missing__"


@dataclass
class Lever:
    id: str
    harness: str
    describe: str
    path: callable           # -> Path of the settings file
    key: str | None = None   # dotted key inside it (env.X for Claude Code env)
    target: callable = None  # (current value) -> new value, or None if already as cheap
    plan: callable = None    # (store) -> {key: new value}, for levers that change several keys

    def changes(self, store) -> dict:
        if self.plan:
            return self.plan(store)
        current = get(self, self.key)
        value = self.target(None if current == MISSING else current)
        return {} if value is None else {self.key: value}


def _cc_settings():
    from .adapters.claude_code import claude_home
    return claude_home() / "settings.json"


def _codex_config():
    from .adapters.codex import codex_home
    return codex_home() / "config.toml"


def _effort_down(current, floor="medium"):
    cur = current if current in EFFORT_ORDER else "high"     # unset means the harness default (high or auto)
    return floor if EFFORT_ORDER.index(cur) > EFFORT_ORDER.index(floor) else None


def _cap(limit):
    def f(current):
        try:
            return limit if current in (None, MISSING) or int(current) > limit else None
        except (TypeError, ValueError):
            return limit
    return f




# ── reading and writing settings ─────────────────────────────────────────────

def _get_json(path: Path, key):
    from .fixes import _read_json
    d = _read_json(path)
    for part in key.split(".")[:-1]:
        d = d.get(part) if isinstance(d, dict) else None
        if d is None:
            return MISSING
    return d.get(key.split(".")[-1], MISSING) if isinstance(d, dict) else MISSING


def _set_json(path: Path, key, value):
    from .fixes import _read_json, _write_json
    data = _read_json(path)
    d = data
    parts = key.split(".")
    for part in parts[:-1]:
        d = d.setdefault(part, {})
    if value is MISSING or value == MISSING:
        d.pop(parts[-1], None)
        if len(parts) > 1 and not d:
            data.pop(parts[0], None)
    else:
        d[parts[-1]] = value
    _write_json(path, data)


_TOML_KEY = r"^{key}\s*=\s*(.*)$"


def _top_level(text):
    """The part of a TOML file before its first [table]."""
    m = re.search(r"^\s*\[", text, re.M)
    return (text[:m.start()], text[m.start():]) if m else (text, "")


def _get_toml(path: Path, key):
    try:
        head, _ = _top_level(path.read_text())
    except OSError:
        return MISSING
    m = re.search(_TOML_KEY.format(key=re.escape(key)), head, re.M)
    if not m:
        return MISSING
    raw = m.group(1).split("#")[0].strip()
    try:
        return json.loads(raw)
    except ValueError:
        return raw.strip("'\"")


def _set_toml(path: Path, key, value):
    """Set, replace or remove one top-level key, line by line, so undoing restores the file exactly."""
    text = path.read_text() if path.exists() else ""
    head, rest = _top_level(text)
    lines = head.split("\n")
    pat = re.compile(rf"^{re.escape(key)}\s*=")
    idx = next((i for i, line in enumerate(lines) if pat.match(line)), None)
    if value is MISSING or value == MISSING:
        if idx is not None:
            del lines[idx]
    else:
        line = f"{key} = {json.dumps(value)}"
        if idx is not None:
            lines[idx] = line
        else:
            last = max((i for i, x in enumerate(lines) if x.strip()), default=-1)
            lines.insert(last + 1, line)
    head = "\n".join(lines)
    if rest and not head.endswith("\n"):
        head += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(head + rest)


def get(lever: Lever, key=None):
    key = key or lever.key
    if lever.harness == "hermes":
        return _hermes_get(key)
    p = lever.path()
    return _get_toml(p, key) if p.suffix == ".toml" else _get_json(p, key)


def put(lever: Lever, value, key=None):
    key = key or lever.key
    if lever.harness == "hermes":
        return _hermes_set(key, value)
    p = lever.path()
    (_set_toml if p.suffix == ".toml" else _set_json)(p, key, value)


# ── Hermes: through `hermes config get/set/unset`, so Hermes validates every write ──────

def _hermes_exe(cfg=None):
    import shutil
    return (cfg or load_config()).get("hermes_exe") or shutil.which("hermes")


def _hermes_get(key):
    import subprocess
    exe = _hermes_exe()
    if not exe:
        raise RuntimeError("hermes is not on PATH")
    r = subprocess.run([exe, "config", "get", key, "--json"], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return MISSING
    try:
        v = json.loads(r.stdout.strip() or "null")
    except ValueError:
        return MISSING
    return MISSING if v is None else v


def _hermes_set(key, value):
    import subprocess
    exe = _hermes_exe()
    if not exe:
        raise RuntimeError("hermes is not on PATH")
    cmd = ([exe, "config", "unset", key] if value is MISSING or value == MISSING else
           [exe, "config", "set", key, value if isinstance(value, str) else json.dumps(value)])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"hermes config: {(r.stderr or r.stdout)[-200:]}")


def _hermes_config_path():
    from .adapters.hermes import hermes_home
    return hermes_home() / "config.yaml"


def _model_id(v):
    if isinstance(v, dict):
        return v.get("model") or v.get("default") or v.get("name")
    return v if isinstance(v, str) and v else None


def hermes_candidates(store: Store) -> list[dict]:
    """Models the user already configured (main, fallbacks, side tasks), with prices the plugin found."""
    prices = store.meta("hermes_prices") or {}
    out = []

    def add(role, provider, model):
        if model:
            p = prices.get(f"{provider or ''}|{model}") or prices.get(f"|{model}")
            out.append({"role": role, "provider": provider, "model": model,
                        "usd_per_mtok": p and round((3 * p["in"] + p["out"]) / 4, 4)})   # ~3:1 input:output
    main = _hermes_get("model")
    add("main", main.get("provider") if isinstance(main, dict) else None, _model_id(main))
    fb = _hermes_get("fallback_providers")
    for f in fb if isinstance(fb, list) else []:
        if isinstance(f, dict):
            add("fallback", f.get("provider"), _model_id(f))
    aux = _hermes_get("auxiliary")
    for task, c in (aux.items() if isinstance(aux, dict) else []):
        if isinstance(c, dict) and c.get("model"):
            add(f"side:{task}", c.get("provider"), c["model"])
    return out


def _cheaper_side_tasks(store: Store) -> dict:
    """Point side tasks with an explicit model at the cheapest model the user already configured."""
    cands = [c for c in hermes_candidates(store) if c["usd_per_mtok"] is not None]
    if not cands:
        return {}
    cheapest = min(cands, key=lambda c: c["usd_per_mtok"])
    plan = {}
    for c in cands:
        if c["role"].startswith("side:") and c["usd_per_mtok"] > 1.5 * cheapest["usd_per_mtok"]:
            task = c["role"].split(":", 1)[1]
            plan[f"auxiliary.{task}.model"] = cheapest["model"]
            if cheapest["provider"]:
                plan[f"auxiliary.{task}.provider"] = cheapest["provider"]
    return plan


def _pareto_down(current):
    if _model_id(_hermes_get("model")) != "openrouter/pareto-code":
        return None
    cur = float(current) if current not in (None, MISSING, "") else 0.65
    return round(max(0.4, cur - 0.15), 2) if cur > 0.4 else None


LEVERS = {
    "claude-code": [
        Lever("subagents", "claude-code", "subagents on Sonnet", _cc_settings, "env.CLAUDE_CODE_SUBAGENT_MODEL",
              lambda cur: None if cur and "sonnet" in str(cur) or cur and "haiku" in str(cur) else "claude-sonnet-5-5"),
        # eval (40 Gnomon tasks, Opus 5.5): effort low saved 33% per task with no pass lost
        Lever("effort", "claude-code", "reasoning effort low", _cc_settings, "effortLevel",
              lambda cur: _effort_down(cur, floor="low")),
        Lever("compaction", "claude-code", "auto-compact at 300k tokens", _cc_settings, "autoCompactWindow",
              _cap(300_000)),
        Lever("main", "claude-code", "main model Sonnet", _cc_settings, "model",
              lambda cur: None if cur and "sonnet" in str(cur) else "sonnet"),
    ],
    "hermes": [
        Lever("side-tasks", "hermes", "side tasks on your cheapest configured model", _hermes_config_path,
              plan=_cheaper_side_tasks),
        Lever("compaction", "hermes", "compress context at 150k tokens", _hermes_config_path,
              "compression.threshold_tokens", _cap(150_000)),
        Lever("quality-score", "hermes", "OpenRouter pareto-code router 0.15 lower", _hermes_config_path,
              "openrouter.min_coding_score", _pareto_down),
    ],
    "codex": [
        Lever("effort", "codex", "reasoning effort low", _codex_config, "model_reasoning_effort",
              lambda cur: _effort_down(cur, floor="low")),
        Lever("compaction", "codex", "auto-compact at 150k tokens", _codex_config, "model_auto_compact_token_limit",
              _cap(150_000)),
    ],
}


# ── decisions ────────────────────────────────────────────────────────────────

def allowed(cfg=None) -> list[str]:
    cfg = cfg or load_config()
    if not cfg.get("levers_consent") or cfg.get("levers") == "off":
        return []
    return list(cfg.get("levers") or DEFAULT_LEVERS)


def consented(harness, cfg=None) -> bool:
    """Consent is per harness: `install claude-code` covers Claude Code and Codex (its disclosure names both),
    `install hermes` covers Hermes, `levers on` covers all three. A bare True means the first two."""
    c = (cfg or load_config()).get("levers_consent")
    if c is True:
        return harness in ("claude-code", "codex")
    return isinstance(c, list) and harness in c


def is_allowed(lever: Lever, allowed_ids) -> bool:
    """A lever id ("effort") allows it in every harness; "hermes:effort" only in that harness."""
    return lever.id in allowed_ids or f"{lever.harness}:{lever.id}" in allowed_ids


def applied(store: Store) -> dict:
    return store.meta("levers_applied") or {}


def _risk(store, harness, now):
    """Worst chance of a hit before reset for this harness's limits (and when that window resets)."""
    from . import steer
    if harness == "codex":
        from .adapters import codex
        ws = codex.pressure(store, now)
    else:
        ws = steer.pressure(store, now, harness=harness)
    ws = [w for w in ws if w.get("p_hit") is not None]
    if not ws:
        return 0.0, None, None
    w = max(ws, key=lambda w: w["p_hit"])
    return w["p_hit"], w["resets"], w["name"]


def _record(store, harness, action, lever, text, now):
    store.add_alert(harness, "levers", "lever", f"{action}:{lever}:{int(now)}", action, text, ts=now)


def apply(store: Store, harness, why, now, cfg, resets=None) -> list[str]:
    state = applied(store)
    mine = state.setdefault(harness, {})
    done = []
    for lever in LEVERS.get(harness, []):
        if not is_allowed(lever, allowed(cfg)) or lever.id in mine:
            continue
        try:
            plan = lever.changes(store)
        except Exception:      # a harness we can't read right now: skip this lever
            continue
        if not plan:
            continue
        keys = {}
        for key, value in plan.items():
            keys[key] = {"previous": get(lever, key), "value": value}
            put(lever, value, key)
        mine[lever.id] = {"keys": keys, "path": str(lever.path()), "at": now, "resets": resets, "why": why}
        done.append(lever.describe)
        _record(store, harness, "apply", lever.id, f"savetokens: {lever.describe} until the limit is safe ({why}).",
                now)
    if done:
        store.set_meta("levers_applied", state)
        store.set_meta("levers_changed_at", now)
    return done


def revert(store: Store, harness=None, why="risk passed", now=None, cooldown=False) -> list[str]:
    now = now or time.time()
    state = applied(store)
    done = []
    for h in [harness] if harness else list(state):
        for lever_id, rec in list(state.get(h, {}).items()):
            lever = next((x for x in LEVERS.get(h, []) if x.id == lever_id), None)
            keys = rec.get("keys") or {rec.get("key"): {"previous": rec.get("previous"), "value": rec.get("value")}}
            restored = False
            for key, k in keys.items():
                try:
                    if lever and get(lever, key) == k["value"]:      # untouched since: restore exactly
                        put(lever, k["previous"], key)
                        restored = True
                except Exception:
                    pass
            if restored:
                done.append(lever.describe)
                _record(store, h, "revert", lever_id, f"savetokens: undid {lever.describe} ({why}).", now)
            state[h].pop(lever_id, None)
        if h in state and not state[h]:
            state.pop(h)
    store.set_meta("levers_applied", state)
    if done:
        store.set_meta("levers_changed_at", now)
    if cooldown:
        store.set_meta("levers_cooldown_until", now + COOLDOWN)
    return done


def update(store: Store, now=None, cfg=None, harnesses=("claude-code", "codex", "hermes")) -> dict:
    """Apply or revert levers per harness from the current chance of a hit. Returns what changed."""
    from . import steer
    cfg = cfg or load_config()
    now = now or time.time()
    mode = steer.configured_mode(cfg)
    out = {}
    for h in harnesses:
        if h == "hermes" and not _hermes_exe(cfg):
            continue
        mine = applied(store).get(h, {})
        if mode not in ("auto", "lean") or not allowed(cfg) or not consented(h, cfg):
            if mine:
                out[h] = {"reverted": revert(store, h, f"mode {mode}", now)}
            continue
        risk, resets, name = _risk(store, h, now)
        cooling = now < (store.meta("levers_cooldown_until") or 0)
        recent = now - (store.meta("levers_changed_at") or 0) < COOLDOWN
        reset_passed = mine and all((r.get("resets") or now + 1) <= now for r in mine.values())
        if mine and (reset_passed or (mode == "auto" and risk < REVERT_RISK and not recent)):
            out[h] = {"reverted": revert(store, h, "the window reset" if reset_passed else
                                         f"chance of a hit down to {risk:.0%}", now)}
        elif (mode == "lean" or risk >= APPLY_RISK) and not cooling:
            why = "lean mode" if mode == "lean" else f"{risk:.0%} chance of hitting the {name} before it resets"
            done = apply(store, h, why, now, cfg, resets)
            if done:
                out[h] = {"applied": done}
    return out


def on_trouble(store: Store, harness, rule, now=None):
    """Quality guard: failing tests or a loop while levers are on means switch back, then cool down."""
    if applied(store).get(harness):
        return revert(store, harness, f"{rule.replace('_', ' ')} while economising", now, cooldown=True)
    return []


def active(store: Store, harness=None) -> list[str]:
    state = applied(store)
    out = []
    for h, levers in state.items():
        if harness and h != harness:
            continue
        out += [next((x.describe for x in LEVERS.get(h, []) if x.id == lid), lid) for lid in levers]
    return out
