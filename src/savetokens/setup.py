"""Setup: what savetokens found here, what's left, and one guided pass through it.

One checklist feeds everything: `savetokens setup` (one question at a time, the detected answer as the
default), `setup --yes` and install (take the detected answers, ask nothing), `suggest` and the MCP
setup tool (what's left, each with its command), and the browser view.

Plans are read from what Claude Code and Codex keep locally: the plan's name only (Claude Code's
`organizationType` and rate-limit tier, Codex's `chatgpt_plan_type` claim). Never tokens, names or emails.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from . import pools

SIGNUP = "https://ephemeris.cascade.industries/sign-up?redirect_url=%2Fdashboard%2Fapi-keys"

# list prices a month; None: depends on seats or a contract, so ask
CLAUDE_TIERS = {"default_claude_max_20x": ("Claude Max 20x", 200), "default_claude_max_5x": ("Claude Max 5x", 100)}
CLAUDE_PLANS = {"max": ("Claude Max", None), "pro": ("Claude Pro", 20), "team": ("Claude Team", None),
                "enterprise": ("Claude Enterprise", None)}
CHATGPT_PLANS = {"free": ("ChatGPT Free", 0), "plus": ("ChatGPT Plus", 20), "pro": ("ChatGPT Pro", 200), "go": ("ChatGPT Go", None),
                 "team": ("ChatGPT Team", None), "business": ("ChatGPT Business", None),
                 "enterprise": ("ChatGPT Enterprise", None), "edu": ("ChatGPT Edu", None)}


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def claude_plan() -> dict | None:
    """Claude Code's way of paying here: {"billing", "plan", "usd"}, or None when it isn't installed."""
    from .capture import account, api_key_signs, claude_home
    if not claude_home().is_dir():
        return None
    a = account()
    kind, tier = a["plan"], a["tier"]
    if not kind:   # no ~/.claude.json account: the login may still be in the credentials file
        oauth = _read(claude_home() / ".credentials.json").get("claudeAiOauth") or {}
        kind, tier = oauth.get("subscriptionType"), oauth.get("rateLimitTier")
    if kind:
        name, usd = CLAUDE_TIERS.get(tier) or CLAUDE_PLANS.get(str(kind).removeprefix("claude_"),
                                                                (f"Claude ({kind})", None))
        return {"billing": "subscription", "plan": name, "usd": usd}
    return {"billing": "api" if api_key_signs() else None, "plan": None, "usd": None}


def codex_plan() -> dict | None:
    from .codex import codex_home
    if not codex_home().is_dir():
        return None
    auth = _read(codex_home() / "auth.json")
    if auth.get("OPENAI_API_KEY") or str(auth.get("auth_mode", "")).lower() in ("apikey", "api_key", "api"):
        return {"billing": "api", "plan": None, "usd": None}
    kind = None
    try:
        body = (auth.get("tokens") or {}).get("id_token", "").split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        kind = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type")
    except (IndexError, ValueError, AttributeError):
        pass
    if not kind:
        return {"billing": None, "plan": None, "usd": None}
    name, usd = CHATGPT_PLANS.get(kind, (f"ChatGPT ({kind})", None))
    return {"billing": "subscription", "plan": name, "usd": usd}


def detected() -> dict:
    """{tool: {"billing", "plan", "usd"}} for the tools installed here."""
    from .hermes import hermes_home
    got = {"claude-code": claude_plan(), "codex": codex_plan()}
    if hermes_home().is_dir():
        got["hermes"] = {"billing": "api", "plan": None, "usd": None}
    return {k: v for k, v in got.items() if v}


def _merged(by_machine) -> dict:
    """What every machine detected, one entry per tool: a named plan first."""
    out = {}
    for found in by_machine.values():
        for h, d in found.items():
            if h not in out or (d.get("plan") and not out[h].get("plan")):
                out[h] = d
    return out


def _step(id, done, title, detail, command=None, ask=None, default=None, optional=False):
    return {"id": id, "done": done, "title": title, "detail": detail, "command": command, "ask": ask,
            "default": default, "optional": optional}


def steps(store, now=None, cfg=None, found=None, on_server=False) -> list[dict]:
    """The checklist, in the order setup walks it. found: detected() (None: detect here). on_server: a sync
    server's view of one user (nothing to detect; it makes the forecasts)."""
    from . import ephemeris, spend, sync
    from .store import DEFAULT_PROVIDER, load_config
    now = now or time.time()
    cfg = load_config() if cfg is None else cfg
    if found is None:
        found = detected() if not on_server else _merged(store.meta("detected") or {})
    week, budgets, plans, out = now - 7 * 86400, pools.budgets(store), pools.plans(store), []

    def used(h, billing):
        cond = "billing = 'api'" if billing == "api" else "billing IS NULL"
        return store.conn.execute(f"SELECT 1 FROM usage WHERE harness = ? AND {cond} AND ts >= ? LIMIT 1",
                                  (h, week)).fetchone() is not None

    for h, d in ({} if on_server else found).items():
        how = d["plan"] or ("pay as you go" if h == "hermes" else
                            {"api": "API key", "subscription": "subscription"}.get(d["billing"], "found"))
        out.append(_step(f"tool:{h}", True, f"{pools.TOOLS[h]}: {how}", f"{pools.TOOLS[h]} is installed here"
                         + (f" on {d['plan']}" if d["plan"] else "")))
    # subscriptions: a fixed price, so spend counts them
    subs = {}
    for h, prov in DEFAULT_PROVIDER.items():
        d = found.get(h) or {}
        if d.get("billing") == "subscription" or used(h, None):
            subs.setdefault(prov, (h, d))
    for prov, in store.conn.execute("SELECT DISTINCT provider FROM usage WHERE harness = 'hermes' AND billing IS NULL"
                                    " AND provider IS NOT NULL AND ts >= ?", (week,)):
        subs.setdefault(prov, ("hermes", {}))
    for prov, (h, d) in subs.items():
        name = d.get("plan") or f"{spend.label(prov)}'s subscription"
        have = plans.get(prov)
        usd = d.get("usd")
        out.append(_step(f"plan:{prov}", bool(have), f"{name}: " + (f"${have['usd']:,.0f} a {have['period']}" if have
                                                                       else "price not set"),
                         f"{pools.TOOLS.get(h, h)} runs on {name}; with its price, spend counts it as a fixed cost"
                         + (f" (list price ${usd:,.0f} a month)" if usd is not None else ""),
                         f"savetokens plan {prov} --usd {usd:g} --per month" if usd is not None else
                         f"savetokens plan {prov} --usd USD --per month",
                         None if usd is not None else f"what {name} costs a month", usd))
    # pay as you go: a budget, and a price for each model savetokens can't price
    for h in pools.TOOLS:
        paid = used(h, "api")
        if (found.get(h) or {}).get("billing") == "api" or paid:
            b = budgets.get(h)
            out.append(_step(f"budget:{h}", bool(b), f"{pools.TOOLS[h]} API budget: "
                             + (f"${b['usd']:,.0f} a {b['period']}" if b else "not set"),
                             f"{pools.TOOLS[h]} pays by API; a budget gives it a forecast and alerts like a plan's",
                             f"savetokens api {h} --budget USD --per month", "the budget and period",
                             optional=not paid))   # nothing spent yet: worth setting, not needed
        for m in pools.unpriced(store, h, week):
            out.append(_step(f"price:{m}", False, f"{m}: no price",
                             f"{m} has no known price, so its usage can't count against the {pools.TOOLS[h]} budget",
                             f"savetokens price {m} INPUT OUTPUT",
                             "the model's $ per million input and output tokens (from the provider's price page)"))
    # forecasts
    if sync.connected(cfg) and not on_server:
        out.append(_step("forecaster", True, "Forecasts: by your server", "the server makes the forecasts for"
                         " every machine; no key needed here"))
    elif cfg.get("forecaster", "ephemeris") != "ephemeris":
        out.append(_step("forecaster", True, "Forecasts: local only", "you turned Ephemeris off",
                         "savetokens ephemeris --on", optional=True))
    else:
        key, trouble = ephemeris.api_key(cfg), ephemeris.problem(store)
        if key and trouble:
            out.append(_step("forecaster", False, "Ephemeris: " + {"credits": "out of credits", "key": "key refused"}
                             .get(trouble["kind"], "failing"), ephemeris.problem_text(trouble),
                             "savetokens ephemeris" if trouble["kind"] == "credits" else "savetokens setup",
                             "a top-up" if trouble["kind"] == "credits" else "a new key"))
        else:
            out.append(_step("forecaster", bool(key), "Forecasts: Ephemeris" if key else "Ephemeris key: not set",
                             "Ephemeris forecasts your usage; without a key savetokens uses its local baseline"
                             + ("" if key else f". Sign up and create a key: {SIGNUP}"),
                             "savetokens setup", None if key else "an Ephemeris key (setup signs you in)"))
    # every machine in one view
    if not sync.connected(cfg) and not on_server:
        out.append(_step("server", False, "Other machines: not connected",
                         "to see every machine together, run a server (savetokens server) or join one",
                         "savetokens join URL CODE", "a join code from `savetokens pair` on a connected machine",
                         optional=True))
    return out


def todo(checklist) -> list[dict]:
    return [s for s in checklist if not s["done"]]


def _money(text):
    try:
        v = float(str(text).strip().lstrip("$").replace(",", ""))
    except ValueError:
        return None
    return v if v >= 0 else None


def run(store, cfg, interactive=True, accept=False, key=None, out=print, ask=input, secret=None, now=None,
        open_url=None) -> dict:
    """Walk what's left. interactive: ask, the detected answer as the default. accept: take the detected
    answers and ask nothing. key: an Ephemeris key to check and keep. Returns {"changed", "steps"}."""
    from . import ephemeris, sync
    from .store import save_config
    secret = secret or __import__("getpass").getpass
    open_url = open_url or __import__("webbrowser").open
    changed = []

    def try_key(k):
        try:
            credits = ephemeris.balance(k)
        except Exception as e:
            out(f"  Ephemeris didn't accept that key ({str(e)[:120]}).")
            return False
        path = ephemeris.save_key(k, cfg)
        cfg["forecaster"] = "ephemeris"
        changed.append("ephemeris key")
        out(f"  Ephemeris key saved (owner-only, in {path}): {credits:,.0f} credits available.")
        return True

    if key:   # a key given up front replaces any key already set
        try_key(key.strip())
    checklist = steps(store, now, cfg)
    found = [s for s in checklist if s["id"].startswith("tool:")]
    if found:
        out("Found: " + "; ".join(s["title"] for s in found) + ".")
    for s in todo(checklist):
        kind, _, name = s["id"].partition(":")
        if kind == "plan":
            usd, label = s["default"], s["title"].split(":")[0]
            if interactive:
                a = ask(f"{label}: what does it cost a month? "
                        + (f"[${usd:,.0f}; n to skip] " if usd is not None else "$ (Enter to skip) ")).strip()
                usd = usd if not a and usd is not None else _money(a)
            elif not accept:
                usd = None
            if usd is not None:
                pools.set_plan(store, name, usd, "month")
                changed.append(f"plan {name}")
                out(f"  {label}: ${usd:,.0f} a month, counted as a fixed cost.")
        elif kind == "budget" and interactive:
            v = _money(ask(f"{pools.TOOLS[name]} pays by API. A budget, $ a month (Enter to skip): "))
            if v:
                pools.set_budget(store, name, v, "month")
                changed.append(f"budget {name}")
        elif kind == "forecaster" and interactive and not key:
            if "credits" in s["title"]:
                out(f"- {s['detail']}")
                continue
            out("Forecasts: Ephemeris predicts your usage hour by hour, so alerts come before you run out.")
            choice = secret("  Press Enter to sign in with your browser, or paste a key (hidden); s to skip: ").strip()
            if choice.lower() in ("s", "skip"):
                continue
            if choice and try_key(choice):
                continue
            if not choice:
                try:
                    if try_key(ephemeris.device_login(out=out, open_url=open_url)):
                        continue
                except ephemeris.NoDeviceLogin:
                    out("  Signing in from the terminal isn't available yet.")
                except Exception as e:
                    out(f"  Sign-in didn't finish ({str(e)[:120]}).")
            out(f"  Get a key (sign up, then 'Create key'): {SIGNUP}")
            for _ in range(2):
                k = secret("  Paste your Ephemeris key (hidden; Enter to skip): ").strip()
                if not k or try_key(k):
                    break
        elif interactive and not s["optional"]:
            out(f"- {s['title']}: {s['command']}")
    save_config(cfg)
    if changed and sync.connected(cfg):
        try:
            sync.push(store, cfg)
        except Exception:
            pass
    checklist = steps(store, now, cfg)
    left = [s for s in todo(checklist) if not s["optional"]]
    out(f"Setup: {len(checklist) - len(todo(checklist))} of {len(checklist)} done."
        + (" Left: " + "; ".join(f"{s['title']} ({s['command']})" for s in left) if left else "")
        + " Run `savetokens setup` again any time.")
    return {"changed": changed, "steps": checklist}
