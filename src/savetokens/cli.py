"""savetokens: set a token budget and stick to it, for Claude Code, Codex and Hermes. It forecasts and advises; it never changes how your tools run."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
from pathlib import Path

from . import __version__

HOOK_BUDGET_SECONDS = 2

# exit codes, the same for every command (for agents and scripts)
OK, ERROR, USAGE, AT_RISK, WONT_FIT = 0, 1, 2, 3, 4
EXIT_CODES = """exit codes:
  0  done
  1  failed: with --json, {"ok": false, "error": ..., "fix": the command that fixes it} on stdout
  2  bad arguments"""
CHECK_CODES = """
  3  a limit is at risk, or the job has to wait for a 5-hour reset (--json gives the reasons)
  4  the job doesn't fit before the limit resets"""
JSON_NOTE = '\n\n--json prints one JSON object on stdout with "ok": true or false, and asks nothing.'

# `savetokens --help`: the commands by what you'd want, then the jobs people do most (one call answers most tasks)
GROUPS = [
    ("start", ["install", "setup", "suggest", "uninstall"]),
    ("see where you stand", ["status", "recap", "efficiency", "advise", "spend", "watch", "web", "dashboard"]),
    ("before a big job", ["estimate", "check"]),
    ("budgets, projects and tasks", ["budget", "projects", "project", "tasks", "task", "api", "plan", "price",
                                     "ephemeris"]),
    ("several machines and teams", ["server", "pair", "join", "connect", "team", "otel"]),
    ("run by install, in the background", ["maintain", "backfill", "mcp"]),
]
COMMON = """common jobs:
  savetokens install                            set up this machine (shows every change first)
  savetokens status --json                      every limit: used, likely at reset, when you'd run out
  savetokens check --like big || echo "wait"    gate a big job on the exit code (0 fits, 3 at risk, 4 won't fit)
  savetokens advise                             what to change, biggest effect first
  savetokens api hermes --budget 100 --per month   a $ budget for a tool on an API key
  savetokens server --setup                     make this machine the hub for all your machines
  curl -fsSL http://HUB:8787/join | sh -s CODE  add another machine (server --setup prints this line)

`savetokens COMMAND --help` shows each command's options and examples.
Docs: https://github.com/TensorLink-AI/savetokens"""

# examples shown under each command's --help
EXAMPLES = {
    "install": ["savetokens install                           # shows every change, asks before making it",
                "savetokens install --yes --json              # nothing asked (agents, scripts)",
                "savetokens install --server URL --code CODE  # and join your hub",
                "savetokens install --no-ephemeris            # forecast on this machine only; nothing leaves it"],
    "uninstall": ["savetokens uninstall                         # your data stays in ~/.savetokens"],
    "status": ["savetokens status", "savetokens status --json"],
    "check": ["savetokens check                             # is every limit on track?",
              "savetokens check --hours 3 --parallel 4 || echo 'not now'",
              "savetokens check --like big --tool codex"],
    "watch": ["savetokens watch --every 30"],
    "web": ["savetokens web", "savetokens web --no-open --port 9000"],
    "dashboard": ["savetokens dashboard --json"],
    "backfill": ["savetokens backfill"],
    "maintain": ["savetokens maintain --quiet                  # what the crontab line runs"],
    "ephemeris": ["savetokens ephemeris                         # key, model and credits left",
                  "echo \"$KEY\" | savetokens ephemeris --key -   # a key, kept out of shell history",
                  "savetokens ephemeris --model ensemble          # every model (about 13x the credits)",
                  "savetokens ephemeris --off                     # the local baseline only"],
    "api": ["savetokens api claude-code --budget 200 --per month",
            "savetokens api hermes --budget 100 --per week",
            "savetokens api codex --off                     # back to the plan"],
    "spend": ["savetokens spend", "savetokens spend --models --json"],
    "plan": ["savetokens plan anthropic --usd 200 --per month    # Claude Max",
             "savetokens plan openai --usd 200 --per month       # ChatGPT Pro",
             "savetokens plan synthetic.new --off"],
    "price": ["savetokens price gpt-6-astra 2 8              # $2 in, $8 out per million tokens",
              "savetokens price my-model 3 15 0.3            # with a cache-read price"],
    "advise": ["savetokens advise", "savetokens advise --json"],
    "team": ["savetokens team                               # totals, projects, budgets (and people, for admins)",
             "savetokens team --people on                   # owner: members see the per-person view too",
             "savetokens budget --team --project webapp --usd 2000 --per month"],
    "otel": ["savetokens otel                               # paste the lines where the agent runs",
             "savetokens otel --json"],
    "budget": ["savetokens budget                              # every budget, with its forecast",
               "savetokens budget --usd 500 --per month         # overall: every tool, provider and machine",
               "savetokens budget --project webapp --usd 150 --per month   # real API spend, any provider",
               "savetokens budget --project webapp --usd 500 --count all   # plan usage too, at API prices",
               "savetokens budget --project webapp --provider openai --usd 100   # one provider's API spend",
               "savetokens budget --project . --tokens 30M --by 2026-12-15   # this folder's project, by a date",
               "savetokens budget --task \"Add OAuth\" --tokens 3M    # a task in this folder's project",
               "savetokens budget --off webapp"],
    "task": ["savetokens task start \"Add OAuth\"          # what follows here counts to it",
             "savetokens task done                          # finished: its cost joins the estimates",
             "savetokens task                               # the task this folder is on, and its cost so far",
             "savetokens task pattern '[A-Z]+-[0-9]+'       # issue keys in branch names are tasks",
             "savetokens task branches off"],
    "tasks": ["savetokens tasks --project .", "savetokens tasks --days 30 --json"],
    "projects": ["savetokens projects                              # tokens, API value and API spend per project",
                 "savetokens projects --days 7 --json",
                 "savetokens projects --share-names on              # your hub's dashboard shows names, not ids"],
    "project": ["savetokens project link                        # this folder is one project, on any machine",
                "savetokens project link webapp",
                "savetokens project share                       # your team sees its name, not just an id"],
    "efficiency": ["savetokens efficiency                        # the last 7 days: facts, then what-ifs",
                   "savetokens efficiency --project webapp --days 30",
                   "savetokens efficiency --tool codex --as gpt-6-luna   # the same tokens at another model's prices"],
    "recap": ["savetokens recap                              # the last 7 days against your plan",
              "savetokens recap --days 30 --json"],
    "estimate": ["savetokens estimate --like big",
                 "savetokens estimate --hours 3 --parallel 4",
                 "savetokens estimate --usd 20 --pool hermes:api"],
    "setup": ["savetokens setup                             # one question at a time; Enter takes the default",
              "savetokens setup --yes                       # take the detected plan prices",
              "savetokens setup --json                      # what's left, each with its command (changes nothing)"],
    "suggest": ["savetokens suggest --json"],
    "mcp": ["claude mcp add savetokens -- savetokens mcp   # install does this for you"],
    "join": ["savetokens join http://HUB:8787 KXQT-7MBD"],
    "pair": ["savetokens pair                               # one machine, 15 minutes",
             "savetokens pair --machines 5 --hours 24"],
    "connect": ["savetokens connect https://hub.example.com --token TOKEN",
                "savetokens connect --off"],
    "server": ["savetokens server --setup                     # this machine is the hub, from now on",
               "savetokens server --public-url https://hub.example.com   # every join command uses it",
               "savetokens server --stop",
               "savetokens server --host 0.0.0.0 --port 8787  # run it yourself, in the foreground",
               "savetokens server --add-user sam              # a teammate's own view and token",
               "echo \"$ANTHROPIC_ADMIN_KEY\" | savetokens server --provider-key anthropic   # API usage no reader saw",
               "savetokens server --team acme --member sam --role admin   # a team on this hub"],
}


def _epilog(name, json):
    ex = "examples:\n" + "\n".join("  " + e for e in EXAMPLES[name])
    return f"{ex}\n\n{EXIT_CODES}{CHECK_CODES if name == 'check' else ''}{JSON_NOTE if json else ''}"


def _overview_help(helps):
    return "\n\n".join(f"{title}:\n" + "\n".join(f"  {c:<11}{helps[c]}" for c in cmds) for title, cmds in GROUPS)


class Fail(Exception):
    """A failure the caller can act on: what went wrong, and the command that fixes it."""

    def __init__(self, error, fix=None, code=ERROR):
        super().__init__(error)
        self.error, self.fix, self.code = error, fix, code


def _out(args, data, text):
    """One result: JSON when --json was given, else the text."""
    if getattr(args, "json", False):
        print(json.dumps({"ok": True, **data}, default=str))
    elif text:
        print(text)


def _fail(args, e: Fail):
    if getattr(args, "json", False):
        print(json.dumps({"ok": False, "error": e.error, "fix": e.fix, "exit": e.code}))
    else:
        print(f"savetokens: {e.error}" + (f"\n  fix: {e.fix}" if e.fix else ""), file=sys.stderr)
    return e.code


def _hook():
    """Hook entry point. Fails open: any error or overrun prints nothing and exits 0."""
    def _timeout(*_):
        raise TimeoutError
    try:
        signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(HOOK_BUDGET_SECONDS)
    except (AttributeError, ValueError):
        pass
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        from . import hooks
        from .store import Store
        with Store() as s:
            out = hooks.handle(payload.get("hook_event_name", ""), payload, s)
        if out:
            sys.stdout.write(json.dumps(out))
    except BaseException:  # noqa: BLE001 - a hook must never break a session
        pass
    finally:
        try:
            signal.alarm(0)
        except (AttributeError, ValueError):
            pass
    return 0


def _statusline():
    try:
        raw = sys.stdin.read()
        from . import hooks
        from .store import Store
        with Store() as s:
            sys.stdout.write(hooks.statusline(json.loads(raw or "{}"), raw, s))
    except Exception:
        pass
    return 0


def models(store, since):
    """[(model, share of API-equivalent $, share of that via subagents)], biggest first."""
    rows = store.conn.execute("SELECT model, SUM(cost_usd), SUM(CASE WHEN subagent THEN cost_usd ELSE 0 END)"
                              " FROM usage WHERE ts >= ? AND cost_usd > 0 GROUP BY model ORDER BY 2 DESC",
                              (since,)).fetchall()
    total = sum(r[1] for r in rows)
    return [(r[0], r[1] / total, r[2] / r[1]) for r in rows] if total else []


def _found(s, now) -> list[str]:
    """What the history holds, per tool, over the last 30 days: for a first run, before any limit reading."""
    from . import pools
    rows = s.conn.execute("SELECT harness, COUNT(*), SUM(input + output + cache_read + cache_write_5m + cache_write_1h),"
                          " MIN(ts)"
                          " FROM usage WHERE ts >= ? GROUP BY harness ORDER BY 3 DESC", (now - 30 * 86400,))
    return [f"{pools.TOOLS.get(h, h)}: {_tok(t or 0)} tokens in {n:,} requests since"
            f" {time.strftime('%d %b', time.localtime(first))}" for h, n, t, first in rows]


def overview() -> int:
    """`savetokens` on its own: what's here, and the next step."""
    from . import install, setup
    from .store import load_config
    found = setup.detected()
    done = install._our_statusline(install._read_json(install.settings_path()).get("statusLine"))
    print(f"savetokens {__version__}: set a token budget and stick to it, for Claude Code, Codex and Hermes.")
    if found:
        from .pools import TOOLS
        how = {k: v["plan"] or ("pay as you go" if k == "hermes" else v["billing"]) or "found" for k, v in found.items()}
        print("Found here: " + "; ".join(f"{TOOLS[k]} ({h})" for k, h in how.items()))
    if not done:
        print("\nStart with:  savetokens install    (shows every change first, then sets up the rest)")
    else:
        from .store import Store
        with Store() as st:
            left = [x for x in setup.todo(setup.steps(st, cfg=load_config(), found=found)) if not x["optional"]]
        if left:
            print(f"\n{len(left)} thing{'s' if len(left) > 1 else ''} left to set up:  savetokens setup")
    print("\n  savetokens status    each limit, and when you'd run out\n  savetokens recap     the last 7 days against your plan\n  savetokens spend     tokens and $ by"
          " provider, and what's coming\n  savetokens web       all of it in your browser\n  savetokens --help    every"
          " command")
    return 0


def cmd_status(args):
    from . import alerts, forecast, meter, sync
    from .store import Store, load_config
    with Store() as s:
        now = time.time()
        looks = forecast.outlook(s, now)
        if args.json:
            print(json.dumps({"ok": True, "outlook": looks, "models": [dict(zip(("model", "share", "subagents"), m))
                                                           for m in models(s, now - 7 * 86400)], "hits": [dict(r) for r in s.conn.execute(
                "SELECT ts, kind, model FROM hits ORDER BY ts DESC LIMIT 20")]}, indent=2))
            return 0
        cfg = load_config()
        acct = meter.active_account(s, now)
        if not looks and not s.conn.execute("SELECT 1 FROM usage LIMIT 1").fetchone():
            from . import capture, maintain
            print("Reading your Claude Code, Codex and Hermes history (first run; counts only)...", flush=True)
            capture.backfill(s)
            maintain.update(s, now, use_ephemeris=False)   # a first forecast from what was read, at once
            looks = forecast.outlook(s, now)
        if not looks:
            found = _found(s, now)
            if found:
                print("Found in your history (last 30 days):\n  " + "\n  ".join(found))
            print("No limit readings yet: they arrive with your next message in Claude Code (through its statusline)"
                  " or Codex. Then this shows each limit and when you'd run out."
                  + ("" if found else " On an API key: `savetokens api`."))
            print("Meanwhile: `savetokens spend` shows tokens and $ by provider.")
        from . import ephemeris
        trouble = ephemeris.problem(s) if not sync.connected(cfg) else None
        if trouble:
            print("⚠ " + ephemeris.problem_text(trouble))
        src = looks[0]["source"] if looks else None
        made = s.meta("forecast_made_at")
        how = {"ephemeris": "Ephemeris", "baseline": "local baseline", None: "none yet"}[src]
        print((f"Claude account {acct} · " if acct else "") + f"forecast: {how}"
              + (f", made {(now - made) / 3600:.1f}h ago" if made else "")
              + (" · synced with " + sync.settings(cfg)["server_url"] if sync.connected(cfg) else ""))
        for o in looks:
            if o.get("kind") == "api":
                print(f"\n{o['label']}: ${o['spent_usd']:,.2f} of ${o['budget_usd']:,.0f} {o['per']} ({o['used']:.0f}%),"
                      f" the {o['period']} ends {alerts.when(o['resets'], now)}")
            else:
                print(f"\n{o['label']}: {o['used']:.0f}% used, resets {alerts.when(o['resets'], now)}"
                      f" (reading {alerts.span(now - o['read_at'])} old)")
            if o["p50"] is not None:
                print(f"  at reset: {o['p50']:.0f}% likely, {o['p10']:.0f}–{o['p90']:.0f}% range;"
                      f" {o['p_hit']:.0%} chance of running out first")
            if o.get("eta"):
                print(f"  at this pace you run out around {alerts.when(o['eta'], now)},"
                      f" {alerts.span(o['resets'] - o['eta'])} before the reset")
            elif o.get("eta_early"):
                print(f"  if it runs hot (1 in 10): out around {alerts.when(o['eta_early'], now)}")
            st = alerts.stage(o, now)
            if st:
                print(f"  ⚠ {st.replace('_', ' ')}: {alerts.message(o, st, now)}")
        hits = []
        for h in s.conn.execute("SELECT ts, kind, model FROM hits ORDER BY ts DESC LIMIT 200"):
            if not hits or (h["kind"], h["model"]) != (hits[-1]["kind"], hits[-1]["model"]) or hits[-1]["ts"] - h["ts"] > 3600:
                hits.append(h)   # one line per run-in: retries of the same limit within the hour are one hit
        hits = hits[:8]
        if hits:
            print("\nlimits you've hit (from transcripts):")
            for h in hits:
                what = {"session": "5-hour limit", "weekly": "weekly limit"}.get(h["kind"]) or (
                    f"{h['model'].title()} limit" if h["model"] else "a limit")
                print(f"  {time.strftime('%a %d %b %H:%M', time.localtime(h['ts']))}  {what}")
        mix = models(s, now - 7 * 86400)
        if mix:
            print("\nthis week by model (share of usage, of which subagents):")
            for m, share, sub in mix[:5]:
                print(f"  {m:28} {share:4.0%}" + (f"  ({sub:.0%} via subagents)" if sub else ""))
        tr = {k: v for k, v in (s.meta("track_record") or {}).items() if k != "at"}
        if tr:
            print("\ntrack record (forecasts whose hours have all passed; error per hour in % of the weekly limit):")
            for provider, r in sorted(tr.items(), key=lambda kv: kv[1]["mae"]):
                print(f"  {provider:10} off by {r['mae']:.2f} points an hour on average, over {r['forecasts']} forecasts")
        rate, ratio = meter.rate(s, account=meter.active_account(s)), meter.five_hour_ratio(s)
        if rate:
            print(f"\n1% of the weekly limit ≈ ${1 / rate:,.2f} of usage at API prices; the 5-hour meter moves"
                  f" {ratio:.1f}x as fast")
    return 0


def cmd_maintain(args):
    from . import maintain
    from .store import Store
    log = (lambda *_: None) if args.quiet or args.json else print
    with Store() as s:
        new = maintain.run(s, log=log)
    if not args.quiet:
        _out(args, {"alerts": [a["message"] for a in new]},
             "\n".join(a["message"] for a in new) or "up to date; no new alerts")
    return 0


def cmd_backfill(args):
    from . import capture, install
    from .store import Store
    with Store() as s:
        n = capture.backfill(s)
        m = install.import_old_meter(s)
    _out(args, {"requests": n, "readings": m},
         f"read {n:,} new requests" + (f" and {m:,} earlier limit readings" if m else ""))
    return 0


def cmd_install(args):
    from . import install
    if bool(args.server) != bool(args.token or args.code):
        raise Fail("--server goes with --code (from `savetokens pair`) or --token",
                   "savetokens install --server URL --code XXXX-XXXX", USAGE)
    if args.json and not args.yes:
        raise Fail("install shows its changes and asks first; with --json, agree up front with --yes",
                   "savetokens install --yes --json", USAGE)
    log = []
    done = install.install(yes=args.yes, key=args.key, no_ephemeris=args.no_ephemeris, cron=not args.no_schedule,
                           server=args.server, token=args.token, code=args.code,
                           tell_agent=True if args.tell_agent else (False if args.yes else None),
                           mcp=not args.no_mcp, out=log.append if args.json else print)
    if not done:
        raise Fail(log[-1] if log else "nothing changed")
    _out(args, {"installed": True, "log": log}, None)
    return 0


def cmd_uninstall(args):
    from . import install
    log = []
    install.uninstall(out=log.append if args.json else print)
    _out(args, {"removed": True, "log": log}, None)
    return 0


def cmd_ephemeris(args):
    from . import ephemeris
    from .store import Store, load_config, save_config
    cfg = load_config()
    if args.key:
        ephemeris.save_key(_key_arg(args.key), cfg)
        cfg["forecaster"] = "ephemeris"
    elif args.off:
        cfg["forecaster"] = "baseline"
    elif args.on:
        cfg["forecaster"] = "ephemeris"
    if args.model:
        cfg["ephemeris_model"] = None if args.model == ephemeris.DEFAULT_MODEL else args.model
    save_config(cfg)
    key = ephemeris.api_key(cfg)
    forecaster = cfg.get("forecaster", "ephemeris")
    data, lines = {"forecaster": forecaster, "model": ephemeris.model(cfg), "key": bool(key), "site": ephemeris.SITE}, [
        f"forecaster: {forecaster}, model {ephemeris.model(cfg)}; key: {'found' if key else 'none'} ({ephemeris.SITE})"]
    if key and forecaster == "ephemeris":
        try:
            data["credits"] = ephemeris.balance(key)
            lines.append(f"credits available: {data['credits']:,.0f}")
        except Exception as e:
            data["key_error"] = str(e)[:200]
            lines.append(f"key not accepted: {e}")
    with Store() as s:
        last = s.meta("ephemeris_last")
    if last:
        data["last_forecast"] = last
        lines.append(f"last forecast: {time.strftime('%a %H:%M', time.localtime(last['made_at']))},"
                     f" {last['horizon']}h ahead from {last['history_hours']}h of history, {last['credits']:g} credits")
    _out(args, data, "\n".join(lines))
    if "key_error" in data:
        raise Fail(f"Ephemeris didn't accept the key: {data['key_error']}", "savetokens ephemeris --key KEY")
    return 0


def cmd_api(args):
    """This machine's Claude Code or Codex runs on an API key, or Hermes (always pay as you go): count its usage
    against a dollar budget."""
    from . import pools, sync
    from .store import Store, load_config, save_config
    cfg = load_config()
    billing = cfg.setdefault("billing", {})
    tool = pools.TOOLS[args.tool]
    lines = []
    with Store() as s:
        if args.off:
            billing.pop(args.tool, None)
            pools.set_budget(s, args.tool, None, None)
            lines.append(f"{tool}'s API budget is removed" if args.tool == "hermes" else
                         f"{tool} on this machine counts as a subscription again; its API budget is removed")
        else:
            if args.tool != "hermes":   # Hermes is always billed by its API provider
                billing[args.tool] = "api"
            if args.budget is not None:
                pools.set_budget(s, args.tool, args.budget, args.per)
            b = pools.budgets(s).get(args.tool)
            lines.append(f"{tool} on this machine is on an API key: its usage counts against "
                         + (f"a budget of ${b['usd']:,.0f} a {b['period']}" if b else
                            "no budget yet (add one with --budget USD --per day|week|month)"))
            if args.tool == "codex":
                lines.append("Codex models have no built-in price: add each with `savetokens price MODEL INPUT"
                             " OUTPUT` ($ per million tokens), or its usage can't count against the budget.")
        save_config(cfg)
        synced = None
        if sync.connected(cfg):
            try:
                sync.push(s, cfg)
                synced = True
            except Exception as e:
                synced = False
                lines.append(f"couldn't reach the server ({e}); it's sent on the next sync")
        b = pools.budgets(s).get(args.tool)
        unpriced = pools.unpriced(s, args.tool, time.time() - 7 * 86400)
    _out(args, {"tool": args.tool, "billing": "subscription" if args.off else "api", "budget": b,
                "unpriced_models": unpriced, "synced": synced,
                "next": [f"savetokens price {m} INPUT OUTPUT" for m in unpriced]
                + ([] if b or args.off else [f"savetokens api {args.tool} --budget USD --per month"])},
         "\n".join(lines))
    return 0


def cmd_price(args):
    """A price for a model savetokens doesn't know, and every captured request on it priced again."""
    from . import pricing
    from .store import Store, load_config, save_config
    cfg = load_config()
    prices = cfg.setdefault("prices", {})
    prices[args.model] = [args.input, args.output] + ([args.cache_read] if args.cache_read is not None else [])
    save_config(cfg)
    pricing.reset()
    with Store() as s:
        n = pricing.reprice(s)
    _out(args, {"model": args.model, "input": args.input, "output": args.output, "cache_read": args.cache_read,
                "repriced": n},
         f"{args.model}: ${args.input:g} in, ${args.output:g} out per million tokens; {n:,} requests priced")
    return 0


def cmd_advise(args):
    from . import advise, capture, mcp
    from .store import Store
    with Store() as s:
        capture.backfill(s)
        b = advise.brief(s, session_id=args.session)
    _out(args, mcp._clean(b), advise.brief_text(b))
    return 0


def _money(v):
    return "–" if v is None else f"${v:,.0f}" if v >= 100 else f"${v:,.2f}"


def _tok(v):
    if v is None:
        return "–"
    for unit, d in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if v >= d:
            return f"{v / d:.1f}{unit}"
    return f"{v:.0f}"


def cmd_spend(args):
    """Tokens and dollars by provider: today, this week, this month, projected, and the next day/week/month."""
    from . import capture, spend
    from .store import Store
    with Store() as s:
        capture.backfill(s)
        got = spend.summary(s)
    if args.json:
        _out(args, got, None)
        return 0
    names = {"day": ("today", "next 24 h"), "week": ("this week", "next 7 days"), "month": ("this month", "next 30 days")}
    lines = [f"{'':22}" + "".join(f"{names[k][0]:>26}" for k in spend.PERIODS)]

    def row(name, cells):
        """cells: {period: (cost so far, tokens so far, likely cost by its end or None)}"""
        out = f"{name[:22]:22}"
        for k in spend.PERIODS:
            cost, tokens, proj = cells.get(k, (None, None, None))
            out += (f"{_money(cost):>9} {_tok(tokens):>6}" + (f" → {_money(proj):>7}" if proj is not None else " " * 10)
                    if cost is not None else " " * 26)
        return out

    def p50(x):
        return (x.get("projected") or {}).get("cost", [None] * 3)[1] if (x.get("projected") or {}).get("cost") else None
    lines.append(row("all providers", {k: (x["cost"], x["tokens"], p50(x)) for k, x in got["periods"].items()}))
    for e in got["providers"]:
        lines.append(row(e["label"] + (" (plan)" if e["plan"] else ""),
                         {k: (x["cost"], x["tokens"], p50(x)) for k, x in e["periods"].items()}))
        if args.models:
            for m in e["models"][:6]:
                lines.append(row("  " + m["model"], {k: (v["usd"], v["tokens"], v.get("projected_usd"))
                                                     for k, v in m["periods"].items()}))
    lines.append("")
    lines.append("cost so far, tokens so far → likely total by the period's end. Cost is pay-as-you-go spend plus"
                 " subscriptions spread over time.")
    nxt = got["periods"]
    lines.append("coming: " + "; ".join(f"{names[k][1]} {_money(nxt[k]['next']['cost'][1])}"
                                        f" ({_money(nxt[k]['next']['cost'][0])}–{_money(nxt[k]['next']['cost'][2])})"
                                        for k in spend.PERIODS if nxt[k]["next"]["cost"]))
    from . import setup
    with Store() as st:
        unset = [x for x in setup.todo(setup.steps(st)) if x["id"].startswith("plan:")]
    for x in unset:   # until a plan has its price, its cost shows as $0
        lines.append(f"{x['title'].split(':')[0]} isn't counted yet: " + (
            f"`savetokens setup --yes` counts it at ${x['default']:,.0f} a month" if x["default"] is not None
            else f"`{x['command']}`"))
    print("\n".join(lines))
    return 0


def cmd_plan(args):
    """A subscription's fixed price, so spend counts it (and not the API value of what it covers)."""
    from . import pools, sync
    from .store import Store, load_config
    with Store() as s:
        pools.set_plan(s, args.provider, None if args.off else args.usd, args.per)
        got = pools.plans(s).get(args.provider)
        cfg = load_config()
        if sync.connected(cfg):
            try:
                sync.push(s, cfg)
            except Exception:
                pass
    _out(args, {"provider": args.provider, "plan": got},
         f"{args.provider}: " + (f"${got['usd']:,.0f} a {got['period']}, counted as a fixed cost" if got
                                 else "no subscription; its usage counts as pay as you go"))
    return 0


def _key_arg(value):
    """--key -: read the key from stdin, so it stays out of shell history."""
    return sys.stdin.readline().strip() if value == "-" else value


def cmd_setup(args):
    """One pass through what's left: plans (their price detected), budgets, the Ephemeris key."""
    from . import setup
    from .store import Store, load_config
    key = _key_arg(args.key)
    interactive = not (args.yes or args.json) and sys.stdin.isatty()
    log = []
    with Store() as s:
        got = setup.run(s, load_config(), interactive=interactive, accept=args.yes, key=key,
                        out=log.append if args.json else print)
    left = setup.todo(got["steps"])
    _out(args, {"changed": got["changed"], "steps": got["steps"], "left": [x["id"] for x in left], "log": log},
         None)
    if key and "ephemeris key" not in got["changed"]:
        raise Fail("Ephemeris didn't accept that key", f"create one at {setup.SIGNUP}")
    return 0


def cmd_suggest(args):
    from . import advise
    from .store import Store
    with Store() as s:
        got = advise.setup(s)
    _out(args, {"suggestions": got}, "\n".join(f"{x['why']}:\n  {x['command']}" for x in got) or "nothing to set up")
    return 0


def _estimate(s, args, pool=None):
    from . import advise
    e = advise.estimate(s, points=args.points, usd=args.usd, like=args.like, hours=args.hours,
                        parallel=max(1, args.parallel), session_id=args.session, pool=pool)
    if "error" in e:
        raise Fail(e["error"], "savetokens estimate --hours H (or --points, --usd, --like small|typical|big)"
                   if "size" in e["error"] else "savetokens status (it needs a few hours of usage first)")
    return e


def cmd_estimate(args):
    from . import capture, mcp
    from .store import Store
    with Store() as s:
        capture.backfill(s)
        e = _estimate(s, args, args.pool)
    _out(args, mcp._clean(e), e["summary"])
    return 0


def cmd_check(args):
    """One verdict for a script or agent to act on, as the exit code: on track (0), at risk (3), won't fit (4)."""
    from . import alerts, capture, forecast, mcp, pools
    from .store import Store
    sized = any(v is not None for v in (args.points, args.usd, args.like, args.hours))
    with Store() as s:
        capture.backfill(s)
        now = time.time()
        every = pools.pools(s, now)
        if args.tool and not any(p.harness == args.tool for p in every):
            raise Fail(f"no limits known for {args.tool} yet",
                       f"savetokens api {args.tool} --budget USD --per month" if args.tool == "hermes"
                       else "savetokens status")
        looks = [o for o in forecast.outlook(s, now) if not args.tool or o["harness"] == args.tool]
        pool = next((p.id for p in every if p.harness == args.tool), None) if args.tool else None
        job = _estimate(s, args, pool) if sized else None
    limits = [{k: o.get(k) for k in ("pool", "label", "kind", "used", "p50", "p10", "p90", "p_hit", "eta",
                                       "resets", "spent_usd", "budget_usd")} | {"stage": alerts.stage(o, now)}
              for o in looks]
    risky = [x for x in limits if x["stage"]]
    reasons = [f"{x['label']}: {x['stage'].replace('_', ' ')}" for x in risky]
    code = AT_RISK if risky else OK
    if job:
        w = job.get("likely_at_reset_with_job")
        if job["finish"] is None or (w is not None and w > 100):
            code = WONT_FIT
            reasons.insert(0, "the job doesn't fit before the limit resets")
        elif job["waits"]:
            code = max(code, AT_RISK)
            reasons.insert(0, "the job has to wait for a 5-hour reset")
    verdict = {OK: "on_track", AT_RISK: "at_risk", WONT_FIT: "wont_fit"}[code]
    if not limits:
        verdict = "no_data"
    text = {"on_track": "on track", "at_risk": "at risk", "wont_fit": "won't fit",
            "no_data": "no limits known yet (savetokens status)"}[verdict]
    _out(args, {"verdict": verdict, "exit": code, "reasons": reasons, "limits": mcp._clean(limits),
                "job": mcp._clean(job) if job else None},
         "\n".join([text + (": " + "; ".join(reasons) if reasons else "")] + ([job["summary"]] if job else [])))
    return code


def _amount(s):
    """'30M' -> 30_000_000; '1.5k' -> 1500."""
    m = {"k": 1e3, "m": 1e6, "b": 1e9}.get(str(s)[-1:].lower())
    try:
        return float(s[:-1]) * m if m else float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an amount: {s} (e.g. 500, 30M, 1.5B)")


BILLING = {"plan": "plan", "api": "API"}


def _split_line(t):
    """ "Claude Code plan 95% · Codex API (openai) 5%": where a project's or task's tokens went."""
    from . import spend
    from .pools import TOOLS
    tot = t["tokens"] or 1
    return " · ".join(f"{TOOLS.get(b['tool'], b['tool'])} {BILLING[b['billing']]}"
                      + (f" ({spend.label(b['provider'])})" if b["tool"] == "hermes" or b["billing"] == "api" else "")
                      + f" {100 * b['tokens'] / tot:.0f}%" for b in t["by"])


def cmd_projects(args):
    from . import projects, sync, tasks
    from .store import Store, load_config, save_config
    cfg = load_config()
    if args.share_names:
        cfg["share_names"] = args.share_names == "on"
        save_config(cfg)
    with Store() as s:
        if args.share_names:
            s.set_meta("pushed:names", None)   # sent (or cleared) on the next sync
        projects.fill(s)
        tasks.fill(s)
        got = projects.report(s, time.time() - args.days * 86400)
        hub = s.meta("hub_projects") if sync.connected(cfg) and args.days == 30 else None
    mine = {p["id"] for p in got}
    every = {r["id"]: r for r in (hub or {}).get("totals", []) if "by" in r}
    if every:   # the hub's totals: every machine's usage, not only this one's
        names = {p["id"]: p for p in got}
        got = sorted(({**names.get(i, {"name": None, "tasks": [], "more_tasks": 0}), **r} for i, r in every.items()),
                     key=lambda p: -p["tokens"])
    out = [{**p, "this_machine": p["id"] in mine} for p in got]
    lines = []
    if args.share_names:
        lines.append("Project and task names: sent to your hub on the next sync, so its dashboard shows them."
                     if cfg["share_names"] else "Project and task names: no longer sent to your hub (cleared on the"
                                                " next sync).")
    if out:
        lines.append(f"Projects, last {args.days} days ({'every machine' if every else 'this machine'}):"
                     "  tokens · at API prices · paid as API")
        for p in out:
            lines.append(f"  {(p['name'] or 'another machine ' + p['id'][:8]):<28} {p['tokens'] / 1e6:>9,.1f}M"
                         f"  ${p['value_usd']:>9,.2f}  ${p['api_usd']:>9,.2f}")
            lines.append(f"  {'':<28} {_split_line(p)}")
            for t in p["tasks"][:3]:
                lines.append(f"    {(t['name'] or t['id'][:8])[:26]:<26} {t['tokens'] / 1e6:>9,.1f}M"
                             f"  ${t['value_usd']:>9,.2f}  ${t['api_usd']:>9,.2f}")
        lines.append("At API prices: what it would cost pay-as-you-go. Paid as API: what was billed that way (real"
                     " money); the rest is on plans. `savetokens tasks --project NAME` for every task.")
    else:
        lines.append(f"No usage with a project in the last {args.days} days.")
    _out(args, {"days": args.days, "every_machine": bool(every), "projects": out,
                "share_names": bool(cfg.get("share_names"))}, "\n".join(lines))
    return OK


def cmd_project(args):
    from . import projects
    from .store import Store
    folder = Path(os.getcwd())
    if args.action == "share":
        with Store() as s:
            pid, name = projects.resolve(s, str(folder))
        name = args.name or name
        _team_call("/v1/team", {"share": {pid: name}})
        _out(args, {"id": pid, "name": name}, f"Your team now sees this project as {name!r}.")
        return OK
    projects.link(folder, args.name)
    with Store() as s:   # folders under this one belong to the linked project from now on (old requests too)
        s.conn.execute("DELETE FROM projects WHERE cwd = ? OR cwd LIKE ?", (str(folder), str(folder) + os.sep + "%"))
        s.conn.execute("UPDATE usage SET project_id = NULL WHERE cwd = ? OR cwd LIKE ?",
                       (str(folder), str(folder) + os.sep + "%"))
        n = projects.fill(s)
        pid, name = projects.resolve(s, str(folder))
    _out(args, {"id": pid, "name": name, "file": str(folder / projects.MARKER), "requests": n},
         f"{folder} is the project {name!r} ({n:,} requests so far). Commit {projects.MARKER} to make it the same"
         " project on every machine.")
    return OK


def _task_line(tk):
    state = "done" if tk.get("done") else "open"
    return (f"  {tk['name']:<32} {state:<5} ${tk.get('usd') or 0:>9,.2f}  {(tk.get('tokens') or 0) / 1e6:>8,.1f}M tokens"
            f"  {tk.get('sessions') or 0} session{'s' if tk.get('sessions') != 1 else ''}  ({tk['source']})")


def cmd_task(args):
    import re
    from . import tasks
    from .store import Store
    cwd = os.getcwd()
    with Store() as s:
        if args.action == "start":
            if not args.name:
                raise Fail("name the task: savetokens task start NAME")
            got = tasks.start(s, cwd, args.name)
            _out(args, got, f"Started {args.name!r} in {got['project']}: what follows here counts to it until"
                            f" `savetokens task done`.\nEstimate: {got['estimate']['text']}.")
        elif args.action == "done":
            got = tasks.done(s, cwd, args.name)
            if not got:
                raise Fail("no task here to finish: start one, or work on a branch", "savetokens task start NAME")
            _out(args, got, f"Done: {got['name']}, ${got['usd']:,.2f}, {got['tokens'] / 1e6:,.1f}M tokens over"
                            f" {got['sessions']} session{'s' if got['sessions'] != 1 else ''}.")
        elif args.action == "pattern":
            st = tasks._settings(s)
            st["pattern"] = None if args.name in (None, "off") else args.name
            if st["pattern"]:
                try:
                    re.compile(st["pattern"])
                except re.error as e:
                    raise Fail(f"not a pattern: {e}")
            s.set_meta("task_settings", st)
            s.conn.execute("UPDATE usage SET task_id = NULL WHERE branch IS NOT NULL")
            n = tasks.fill(s)
            _out(args, {"pattern": st["pattern"], "requests": n},
                 f"Tracker keys: {st['pattern'] or 'off'} ({n:,} requests on a task).")
        elif args.action == "branches":
            st = tasks._settings(s)
            st["branches"] = args.name != "off"
            s.set_meta("task_settings", st)
            s.conn.execute("UPDATE usage SET task_id = NULL WHERE branch IS NOT NULL")
            n = tasks.fill(s)
            _out(args, {"branches": st["branches"], "requests": n},
                 f"Branches as tasks: {'on' if st['branches'] else 'off'} ({n:,} requests on a task).")
        else:   # current
            got = tasks.current(s, cwd)
            if not got:
                _out(args, {"task": None}, "No task here: `savetokens task start NAME`, or work on a branch.")
            else:
                tot = tasks.totals(s, got["id"])
                _out(args, {"task": tot, "estimate": tasks.estimate(s, got["project_id"])},
                     f"{tot['name']} ({tot['source']}): ${tot['usd']:,.2f}, {tot['tokens'] / 1e6:,.1f}M tokens so far."
                     f"\nEstimate: {tasks.estimate(s, got['project_id'])['text']}.")
    return OK


def cmd_tasks(args):
    from . import projects, tasks
    from .store import Store
    with Store() as s:
        projects.fill(s)
        tasks.fill(s)
        ids = None
        if args.project:
            ids = [i for i in projects.by_name(s, args.project) if i]
            if not ids:
                raise Fail(f"no project called {args.project!r} here", "savetokens projects")
        got = tasks.listing(s, ids, time.time() - args.days * 86400)
        sp = projects.split(s, "task_id", 0)
        got = [{**tk, **{k: v for k, v in sp.get(tk["id"], {}).items() if k in ("by", "value_usd", "api_usd")}}
               for tk in got]
        names = projects.names(s)
        est = tasks.estimate(s, ids[0]) if ids and len(ids) == 1 else None
    if not got:
        _out(args, {"tasks": []}, "No tasks yet: `savetokens task start NAME` in a project, or work on a branch.")
        return OK
    lines, last = [], None
    for tk in got:
        if tk.get("project_id") != last:
            last = tk.get("project_id")
            lines.append(f"{names.get(last) or 'project ' + str(last)[:8]}:")
        lines.append(_task_line(tk))
        if tk.get("by"):
            lines.append(f"  {'':<32} {_split_line(tk)}" + (f"; ${tk['api_usd']:,.2f} paid as API" if tk.get("api_usd")
                                                             else ""))
    if est:
        lines.append(f"Estimate for the next one: {est['text']}.")
    _out(args, {"tasks": got, "estimate": est}, "\n".join(lines))
    return OK


def _team_call(path, body=None):
    from . import sync
    from .store import load_config
    cfg = load_config()
    if not sync.connected(cfg):
        raise Fail("not connected to a hub", "savetokens server --setup")
    try:
        return sync._call(cfg, path, body)
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except Exception:
            msg = str(e)
        raise Fail(msg or str(e))


def cmd_team(args):
    from . import projects
    from .store import Store
    if args.people:
        _team_call("/v1/team", {"people_visible": args.people == "on"})
    got = _team_call(f"/v1/team?days={args.days}")
    with Store() as s:
        names = projects.names(s)
    plural = lambda n, one, many: f"{n} {one if n == 1 else many}"
    lines = [f"{got['team']} ({plural(got['members'], 'member', 'members')}; you're {got['role']}): ${got['usd']:,.2f},"
             f" {got['tokens'] / 1e6:,.1f}M tokens in {got['days']} days."]
    for pr in got["projects"][:10]:
        name = pr["name"] or names.get(pr["id"]) or f"project {pr['id'][:8]}"
        lines.append(f"  {name:<28} ${pr['usd']:>10,.2f}  {pr['tokens'] / 1e6:>8,.1f}M tokens"
                     f"  {plural(pr['people'], 'person', 'people')}")
    for o in got["budgets"]:
        lines.append(f"  {o['label']}: {o['used']:.0f}% used" + (f", likely {o['p50']:.0f}% by the end" if o.get("p50")
                                                                 is not None else ""))
    for pe in got.get("people") or []:
        lines.append(f"  {pe['person']:<20} ${pe['usd']:>10,.2f}  {pe['tokens'] / 1e6:>8,.1f}M tokens")
    _out(args, got, "\n".join(lines))
    return OK


def cmd_budget(args):
    from . import forecast, maintain, pools, projects
    from .store import Store
    import re
    from datetime import datetime
    with Store() as s:
        if args.off and args.team:
            goals = _team_call("/v1/team").get("goals")
            if goals is None:
                raise Fail("only the team's owner or admins set its budgets")
            gid = re.sub(r"[^a-z0-9]+", "-", args.off.lower()).strip("-")
            if goals.pop(gid, None) is None:
                raise Fail(f"no team budget {args.off!r}", "savetokens team")
            _team_call("/v1/team", {"goals": goals})
            _out(args, {"removed": gid, "team": True}, f"Removed the team's {gid} budget.")
            return OK
        if args.off:
            gid = args.off if args.off in pools.goals_(s) else re.sub(r"[^a-z0-9]+", "-", args.off.lower()).strip("-")
            if not pools.remove_goal(s, gid):
                raise Fail(f"no budget {args.off!r}", "savetokens budget")
            _out(args, {"removed": gid}, f"Removed the {gid} budget.")
            return OK
        if args.usd is None and args.tokens is None:
            looks = [o for o in forecast.outlook(s, goals=True) if o["kind"] == "goal"]
            if not looks:
                _out(args, {"budgets": []}, "No budgets yet. e.g. savetokens budget --usd 500 --per month,"
                                            " or --project NAME --usd 150 --per month")
                return OK
            fmt = lambda o, v: f"${v:,.2f}" if o["unit"] == "usd" else f"{v / 1e6:,.1f}M tokens"
            lines = []
            for o in looks:
                line = (f"{o['label']} ({o['counts']}): {fmt(o, o['spent'])} of {fmt(o, o['amount'])} {o['per']}"
                        f" ({o['used']:.0f}%)")
                if o["used"] >= 100:
                    line += f"; over by {fmt(o, o['spent'] - o['amount'])}"
                elif o["p50"] is not None:
                    line += f"; likely {o['p50']:.0f}% by the end, {o['p_hit'] or 0:.0%} chance of going over"
                else:
                    line += "; forecast on the next upkeep"
                lines.append(line)
            _out(args, {"budgets": looks}, "\n".join(lines))
            return OK
        if args.task:
            from . import tasks
            pid, _ = projects.resolve(s, os.getcwd())
            tid = tasks.task_id(s, pid, args.task)
            if not s.conn.execute("SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone():
                raise Fail(f"no task called {args.task!r} in this folder's project", "savetokens tasks --project .")
            label, scope = args.task, {"tasks": [tid]}
        elif args.project:
            ids = projects.by_name(s, args.project)
            if not ids or ids == [None]:
                raise Fail(f"no project called {args.project!r} here", "savetokens projects")
            label = args.project if args.project != "." else projects.resolve(s, os.getcwd())[1]
            scope = {"projects": ids}
        else:
            label, scope = "Overall", {}
        unit, amount = ("usd", args.usd) if args.usd is not None else ("tokens", args.tokens)
        if args.provider:
            scope["providers"] = sorted({p.lower() for p in args.provider})
        if args.tool:
            scope["tools"] = sorted(set(args.tool))
        count = args.count or ("api" if unit == "usd" else "all")
        goal = {"label": label, "scope": scope, "count": count, "unit": unit, "amount": float(amount),
                "start": time.time(), "tz": time.localtime().tm_gmtoff}
        if args.by:
            try:
                end = datetime.strptime(args.by, "%Y-%m-%d").replace(hour=23, minute=59, second=59).timestamp()
            except ValueError:
                raise Fail(f"--by takes a date like 2026-12-15, not {args.by!r}")
            if end <= time.time():
                raise Fail("--by is in the past")
            goal["by"] = end
        else:
            goal["period"] = args.per
        gid = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "budget"
        if args.team:   # the team's, on the hub: every member counts
            goals = _team_call("/v1/team").get("goals")
            if goals is None:
                raise Fail("only the team's owner or admins set its budgets")
            goals[gid] = {**goal, "label": f"Team {label}" if label != "Overall" else "Team"}
            _team_call("/v1/team", {"goals": goals})
            _out(args, {"id": gid, "team": True, **goal}, f"Team budget set: {label},"
                                                         " every member's usage. `savetokens team` shows it.")
            return OK
        pools.set_goal(s, gid, goal)
        s.set_meta("kicked_at", 0)
        maintain.kick(s)   # its forecast, in the background
        # would it have counted anything lately? A pay-as-you-go budget on work that's all on plans stays at 0
        where, wargs = pools._scope(goal, s)
        lately = s.conn.execute(f"SELECT COUNT(*) FROM usage WHERE {where} AND ts >= ?",
                                wargs + [time.time() - 30 * 86400]).fetchone()[0]
    when = f"by {args.by}" if args.by else f"a {args.per}"
    shown = f"${amount:,.0f}" if unit == "usd" else f"{amount / 1e6:,.1f}M tokens"
    text = (f"{label} budget: {shown} {when}, counting {pools.describe(goal)}, from every machine. Its forecast"
            " comes with the next upkeep: savetokens budget shows it.")
    if not lately and count == "api":
        text += ("\nNote: none of the usage it covers in the last 30 days was pay-as-you-go (it was on plans),"
                 " so it stays at $0 until an API key is used. To budget plan usage too, at API prices: add --count all.")
    elif not lately:
        text += "\nNote: nothing it covers was used in the last 30 days."
    _out(args, {"id": gid, "counted_last_30_days": lately, **goal}, text)
    return OK


def cmd_otel(args):
    """The lines that send an agent's OpenTelemetry to your hub (a token of its own, from the hub)."""
    from . import sync
    from .store import load_config
    cfg = load_config()
    if not sync.connected(cfg):
        raise Fail("not connected to a hub: telemetry goes to a hub", "savetokens server --setup")
    try:
        got = sync._call(cfg, "/v1/token", {})
    except Exception as e:
        raise Fail(f"the hub didn't give a token: {e}")
    url = (got.get("url") or cfg["server_url"]).rstrip("/")
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": url, "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
           "OTEL_EXPORTER_OTLP_HEADERS": f"Authorization=Bearer {got['token']}", "OTEL_LOGS_EXPORTER": "otlp",
           "OTEL_TRACES_EXPORTER": "otlp"}
    claude = {"CLAUDE_CODE_ENABLE_TELEMETRY": "1"}
    lines = ["# any agent or app that exports OpenTelemetry (GenAI conventions):"]
    lines += [f"export {k}='{v}'" for k, v in env.items()]
    lines += ["# Claude Code also needs (only where savetokens isn't installed: there it reads Claude Code already):"]
    lines += [f"export {k}={v}" for k, v in claude.items()]
    _out(args, {"env": env, "claude_code": claude}, "\n".join(lines))
    return OK


def cmd_efficiency(args):
    from . import efficiency
    from .store import Store
    from . import projects
    where = {k: v for k, v in (("harness", args.tool), ("provider", args.provider), ("model", args.model)) if v}
    with Store() as s:
        if args.project:
            ids = [i for i in projects.by_name(s, args.project) if i]
            if not ids:
                raise Fail(f"no project called {args.project!r} here", "savetokens projects")
            where["project_id"] = ids
        r = efficiency.report(s, days=args.days, where=where or None, model=args.as_model)
    _out(args, r, efficiency.text(r))
    return OK


def cmd_recap(args):
    from . import recap
    from .store import Store
    with Store() as s:
        r = recap.recap(s, days=args.days)
    _out(args, r, recap.text(r) or f"No usage in the last {args.days} days.")
    return OK


def cmd_mcp(args):
    from . import mcp
    mcp.serve()
    return 0


def cmd_join(args):
    """Join a server with a short code: no token to copy."""
    import urllib.request

    from . import sync
    from .store import Store, load_config, save_config
    req = urllib.request.Request(args.url.rstrip("/") + "/v1/join", data=json.dumps({"code": args.code}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            token = json.load(r)["token"]
    except Exception as e:
        raise Fail(f"couldn't join {args.url}: {e}",
                   "get a fresh code: `savetokens pair` on a joined machine, or 'Add a machine' in the hub's browser view")
    cfg = load_config()
    cfg.update(server_url=args.url, server_token=token)
    save_config(cfg)
    with Store() as s:
        n = sync.push(s, cfg)
        got = sync.pull(s, cfg)
    _out(args, {"server": args.url, "sent": n, "received": got["meter"]},
         f"joined {args.url}: sent {n:,} rows, received {got['meter']:,} readings from other machines")
    return 0


def cmd_pair(args):
    """A join code for another machine, from this one (already connected)."""
    from . import sync
    from .store import load_config
    cfg = load_config()
    if not sync.connected(cfg):
        raise Fail("this machine isn't connected to a hub",
                   "savetokens server --setup (make this machine the hub), or paste the join line from your hub")
    import urllib.parse

    from .server import join_command
    got = sync._call(cfg, "/v1/pair", {"machines": args.machines, "hours": args.hours})
    url = sync.settings(cfg)["server_url"].rstrip("/")
    if url.startswith(("http://127.0.0.1", "http://localhost")):   # this is the hub: give the address others use
        from . import hub
        url = hub.address(urllib.parse.urlparse(url).port or 8787)[0]
    cmd = join_command(url, got["code"])
    n = got.get("machines", 1)
    _out(args, {"code": got["code"], "expires_in": got.get("expires_in"), "machines": n, "command": cmd,
                "join": f"savetokens join {url} {got['code']}", "browser": f"{url}/#code={got['code']}"},
         f"On {'each' if n > 1 else 'the'} other machine (works for {_span(got.get('expires_in') or 600)}"
         f"{f', up to {n} machines' if n > 1 else ', once'}):\n  {cmd}\n"
         f"(savetokens already installed there? savetokens join {url} {got['code']})\n"
         f"Or open the browser view: {url}/#code={got['code']}")
    return 0


def _span(seconds):
    return f"{seconds // 3600:g} hours" if seconds >= 7200 else f"{max(1, round(seconds / 60))} minutes"


def cmd_connect(args):
    from . import sync
    from .store import Store, load_config, save_config
    cfg = load_config()
    if args.off:
        cfg.pop("server_url", None)
        cfg.pop("server_token", None)
        save_config(cfg)
        _out(args, {"connected": False}, "disconnected; forecasts are made on this machine again")
        return 0
    if not (args.url and args.token):
        raise Fail("connect needs URL and --token (or --off)", "savetokens join URL CODE (no token needed)", USAGE)
    cfg.update(server_url=args.url, server_token=args.token)
    with Store() as s:
        n = sync.push(s, cfg)
        got = sync.pull(s, cfg)
    save_config(cfg)
    _out(args, {"connected": True, "server": args.url, "sent": n, "received": got["meter"]},
         f"connected to {args.url}: sent {n:,} rows, received {got['meter']:,} readings from other machines")
    return 0


def _snapshot(s, push=True):
    """The server's snapshot when connected (every machine), else this machine's. Falls back to local."""
    from . import dashboard, sync
    from .store import load_config
    cfg = load_config()
    if sync.connected(cfg):
        try:
            if push:
                sync.push(s, cfg)
            snap = sync._call(cfg, "/v1/dashboard", timeout=10)
            snap["synced_at"] = time.time()
            return snap
        except Exception as e:
            snap = dashboard.snapshot(s)
            snap["sync_error"] = str(e)[:120]
            return snap
    return dashboard.snapshot(s)


def cmd_dashboard(args):
    import shutil

    from . import capture, dashboard
    from .store import Store
    with Store() as s:
        capture.backfill(s)
        snap = _snapshot(s)
    if args.json:
        print(json.dumps({"ok": True, **snap}))
    else:
        print("\n".join(dashboard.render(snap, shutil.get_terminal_size().columns, color=sys.stdout.isatty())))
    return 0


def cmd_watch(args):
    """Full-screen and live: redraws every few seconds until Ctrl-C."""
    import shutil

    from . import capture, dashboard, maintain
    from .store import Store
    out = sys.stdout
    out.write("\033[?1049h\033[?25l")   # alternate screen, hide the cursor
    try:
        while True:
            with Store() as s:
                capture.backfill(s)
                maintain.kick(s)
                snap = _snapshot(s)
            size = shutil.get_terminal_size()
            lines = dashboard.render(snap, size.columns)
            if snap.get("sync_error"):
                lines.append(f"(server unreachable, showing this machine: {snap['sync_error']})")
            lines.append("")
            lines.append(f"\033[2mrefreshes every {args.every}s · Ctrl-C to quit\033[0m")
            out.write("\033[H\033[2J" + "\n".join(lines[:size.lines - 1]))
            out.flush()
            time.sleep(args.every)
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\033[?25h\033[?1049l")
        out.flush()
    return 0


def cmd_web(args):
    """The dashboard in the browser, served on this machine only."""
    from . import web
    web.serve_local(web.local_snapshot, port=args.port, open_browser=not args.no_open)
    return 0


def cmd_server(args):
    from pathlib import Path

    from . import server
    from .store import home
    data = Path(args.data) if args.data else home() / "server"
    if args.add_user:
        users = server.Users(data)
        token = users.add(args.add_user)
        print(f"user {args.add_user}: token {token} (shown once; for SAVETOKENS_TOKEN in containers)\n"
              f"to add a machine: savetokens join URL {users.pair(args.add_user, seconds=3600)} (valid an hour)")
        return 0
    if args.pair:
        print(f"savetokens join URL {server.Users(data).pair(args.pair)}  (valid 10 minutes, once)")
        return 0
    if args.team or args.member:
        from .teams import Teams, copy_history
        if not (args.team and args.member):
            raise Fail("both: savetokens server --team TEAM --member NAME [--role owner|admin|member]")
        users, teams = server.Users(data), Teams(data)
        new = args.member not in users.names()
        if new:
            users.add(args.member)
        try:
            t = teams.add(args.team, args.member, args.role or "member")
        except ValueError as e:
            raise Fail(str(e))
        with users.store(args.member) as us, teams.store(args.team) as ts:
            copy_history(us, ts, args.member)
        print(f"{args.member} is {t['members'][args.member]} of {args.team} ({len(t['members'])} member"
              f"{'s' if len(t['members']) != 1 else ''}).")
        if new:
            code = users.pair(args.member, server.INVITE_SECONDS, server.INVITE_MACHINES)
            base = server.public_url(data)
            print(f"Their machines join with: {server.join_command(base, code) if base else 'savetokens join URL ' + code}")
        return 0
    if args.provider_key:
        from . import providers
        if args.provider_key not in providers.NAMES:
            raise Fail(f"providers: {', '.join(providers.NAMES)}")
        key = "" if sys.stdin.isatty() else sys.stdin.read().strip()
        path = providers.keys_path(data, args.user or "me")
        providers.save_key(path, args.provider_key, key or None)
        print(f"{args.provider_key}: " + (f"admin key kept in {path} (only you can read it); the hub reads its"
                                          " usage report hourly" if key else "removed (pipe a key in to set one)"))
        return 0
    if args.public_url:
        try:
            url = server.set_public_url(data, None if args.public_url == "off" else args.public_url)
        except ValueError as e:
            raise Fail(str(e))
        print(f"Join commands use {url} from now on." if url else
              "Forgot the saved address: join commands use the address each browser reached the hub at.")
        if not args.setup:
            return 0
    if args.setup:
        from . import hub
        return 0 if hub.setup(args.port, data, interactive=sys.stdin.isatty(), public=args.public_url
                              if args.public_url not in (None, "off") else None) else 1
    if args.stop:
        from . import hub
        print("Stopped the hub; it won't start at boot. Your data stays in " + str(data) if hub.stop()
              else "No hub set up by `savetokens server --setup` here.")
        return 0
    server.serve(data, args.host, args.port)
    return 0


def _job_args(s, required):
    g = s.add_mutually_exclusive_group(required=required)
    g.add_argument("--points", type=float, help="points of the limit")
    g.add_argument("--usd", type=float, help="API-equivalent dollars")
    g.add_argument("--like", help="small, typical, big (past sessions here) or a session id")
    g.add_argument("--hours", type=float, help="hours the agent will work (sized at your usual usage per hour)")
    s.add_argument("--parallel", type=int, default=1, help="sessions or subagents at once")
    s.add_argument("--session", help="a session id (default: the latest one in this folder)")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return overview()
    if argv[:1] == ["hook"]:
        return _hook()
    if argv[:1] == ["statusline"]:
        return _statusline()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="one JSON object on stdout, for agents and scripts")
    p = argparse.ArgumentParser(prog="savetokens", usage="savetokens COMMAND [options]", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND", help=argparse.SUPPRESS)
    helps = {}

    def cmd(name, fn, help, json=True, **kw):
        helps[name] = help
        s = sub.add_parser(name, prog=f"savetokens {name}", description=help, parents=[common] if json else [], epilog=_epilog(name, json),
                           formatter_class=argparse.RawDescriptionHelpFormatter, **kw)
        s.set_defaults(fn=fn)
        return s
    s = cmd("install", cmd_install, "add the statusline, hooks, skill and MCP server; read history")
    s.add_argument("--yes", action="store_true", help="don't ask (needed with --json)")
    s.add_argument("--key", help="Ephemeris API key")
    s.add_argument("--no-ephemeris", action="store_true", help="forecast locally only")
    s.add_argument("--no-schedule", action="store_true", help="no crontab line")
    s.add_argument("--server", help="URL of your savetokens server (it makes the forecasts)")
    s.add_argument("--token", help="your token on that server")
    s.add_argument("--code", help="a join code from that server (instead of a token)")
    s.add_argument("--tell-agent", action="store_true", help="give the agent a pacing note when a limit is at risk")
    s.add_argument("--no-mcp", action="store_true", help="don't register the MCP server")
    cmd("uninstall", cmd_uninstall, "remove the statusline, hooks, skill, MCP server and crontab line")
    cmd("status", cmd_status, "each limit: used now, at reset, and when you'd run out")
    s = cmd("check", cmd_check, "one verdict in the exit code: 0 on track, 3 at risk, 4 the job won't fit")
    _job_args(s, required=False)
    s.add_argument("--tool", choices=["claude-code", "codex", "hermes"], help="only this tool's limits")
    s = cmd("watch", cmd_watch, "live dashboard in the terminal (put it in a split pane next to your agent)", json=False)
    s.add_argument("--every", type=int, default=15, help="seconds between refreshes")
    s = cmd("web", cmd_web, "the dashboard in your browser (every machine, when connected to a server)", json=False)
    s.add_argument("--port", type=int, default=8788, help="port on 127.0.0.1 (default 8788)")
    s.add_argument("--no-open", action="store_true", help="print the address instead of opening a browser")
    cmd("dashboard", cmd_dashboard, "the dashboard once (--json for other tools, e.g. the Claude Code pane)")
    cmd("backfill", cmd_backfill, "read Claude Code, Codex and Hermes sessions again")
    s = cmd("maintain", cmd_maintain, "forecast if due, raise alerts, sync (runs in the background)")
    s.add_argument("--quiet", action="store_true", help="print nothing unless something failed")
    s = cmd("ephemeris", cmd_ephemeris, "the Ephemeris forecaster: key, on/off, credits")
    s.add_argument("--key", help="your API key (- reads it from stdin)")
    s.add_argument("--model", help="the model every forecast uses from now on (default toto2-313m), or"
                                   " ensemble: every model, about 13x the credits. Forecasts run in the background")
    s.add_argument("--on", action="store_true", help="forecast with Ephemeris again")
    s.add_argument("--off", action="store_true", help="the local baseline only; nothing sent to Ephemeris")
    s = cmd("api", cmd_api, "Claude Code or Codex on an API key, or Hermes: set a $ budget")
    s.add_argument("tool", choices=["claude-code", "codex", "hermes"], help="claude-code, codex or hermes")
    s.add_argument("--budget", type=float, metavar="USD", help="dollars per period")
    s.add_argument("--per", choices=["day", "week", "month"], default="month", help="the period (default month)")
    s.add_argument("--off", action="store_true", help="back to a subscription")
    s = cmd("spend", cmd_spend, "tokens and $ by provider: today, this week, this month, and what's coming")
    s.add_argument("--models", action="store_true", help="each provider's models too")
    s = cmd("plan", cmd_plan, "a subscription's fixed price (Claude Max, ChatGPT Pro, a flat-rate provider)")
    s.add_argument("provider", help="anthropic, openai, or a provider Hermes uses (as `savetokens spend` names it)")
    s.add_argument("--usd", type=float, help="what it costs per period")
    s.add_argument("--per", choices=["day", "week", "month"], default="month", help="the period (default month)")
    s.add_argument("--off", action="store_true", help="no subscription any more")
    s = cmd("price", cmd_price, "the API price of a model savetokens doesn't know ($ per million tokens)")
    s.add_argument("model", help="the model's name, as the tool reports it")
    s.add_argument("input", type=float, help="$ per million input tokens")
    s.add_argument("output", type=float, help="$ per million output tokens")
    s.add_argument("cache_read", type=float, nargs="?", help="$ per million cached input tokens (default: a tenth of input)")
    s = cmd("budget", cmd_budget, "set a budget, overall or for a project, in $ or tokens: per period or a total by"
            " a date; or list them with their forecasts")
    sc = s.add_mutually_exclusive_group()
    sc.add_argument("--project", metavar="NAME", help="a project's name (savetokens projects), or . for this folder's")
    sc.add_argument("--task", metavar="NAME", help="a task in this folder's project (savetokens tasks)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--usd", type=float, help="dollars: what --count says counts")
    g.add_argument("--tokens", type=_amount, help="tokens, e.g. 30M")
    g2 = s.add_mutually_exclusive_group()
    g2.add_argument("--per", choices=["day", "week", "month"], default="month", help="the period (default month)")
    g2.add_argument("--by", metavar="YYYY-MM-DD", help="a total to spend by this date, instead of per period")
    s.add_argument("--count", choices=["api", "all"], help="api: only pay-as-you-go spend, real money (default for"
                   " --usd); all: plan usage too, at API prices (default for --tokens)")
    s.add_argument("--provider", action="append", metavar="NAME", help="only this provider (anthropic, openai, ...);"
                   " repeat for more")
    s.add_argument("--tool", action="append", choices=["claude-code", "codex", "hermes"], help="only this tool;"
                   " repeat for more")
    s.add_argument("--off", metavar="NAME", help="remove a budget")
    s.add_argument("--team", action="store_true", help="the team's budget, on the hub (owner or admins)")
    s = cmd("projects", cmd_projects, "every project's tokens and $, split by tool, provider and plan or API, with"
            " its biggest tasks: across machines when connected to a hub")
    s.add_argument("--days", type=int, default=30, help="how many days back (default 30)")
    s.add_argument("--share-names", choices=["on", "off"], help="send project and task names to your own hub, so its"
                   " dashboard shows them (off: they stay on this machine; the hub sees ids)")
    s = cmd("task", cmd_task, "a unit of work in this folder's project: start, done, or what it's on now")
    s.add_argument("action", nargs="?", default="current", choices=["current", "start", "done", "pattern", "branches"],
                   help="start NAME, done [NAME], pattern REGEX|off (issue keys in branch names), branches on|off")
    s.add_argument("name", nargs="?", help="the task's name (or the pattern, or on/off)")
    s = cmd("tasks", cmd_tasks, "every task: tokens, $, sessions, open or done; and what finished ones cost")
    s.add_argument("--project", metavar="NAME", help="one project (or . for this folder's)")
    s.add_argument("--days", type=int, default=90, help="tasks active in this many days (default 90)")
    s = cmd("project", cmd_project, "make this folder one project on every machine (writes a .savetokens file)")
    s.add_argument("action", choices=["link", "share"], help="link: this folder (and all under it) is one project;"
                                                              " share: your team sees its name")
    s.add_argument("name", nargs="?", help="what to call it (default: the repo's or folder's name)")
    s = cmd("team", cmd_team, "your team on the hub: totals, projects, budgets, and people (if you may see them)")
    s.add_argument("--days", type=int, default=30, help="how many days back (default 30)")
    s.add_argument("--people", choices=["on", "off"], help="owner: whether members see the per-person view")
    cmd("otel", cmd_otel, "the lines that send any agent's OpenTelemetry to your hub (a token of its own)")
    s = cmd("efficiency", cmd_efficiency, "where tokens go and what the same work would cost done differently:"
            " facts and what-ifs, any tool or provider")
    s.add_argument("--days", type=int, default=7, help="how many days back (default 7)")
    s.add_argument("--tool", choices=["claude-code", "codex", "hermes"], help="only this tool")
    s.add_argument("--provider", help="only this provider (anthropic, openai, openrouter, ...)")
    s.add_argument("--project", help="only this project (a name from savetokens projects, or . for this folder's)")
    s.add_argument("--model", help="only this model")
    s.add_argument("--as", dest="as_model", metavar="MODEL", help="price the same tokens at this model's prices")
    s = cmd("recap", cmd_recap, "how the last week went against your plan: under or over budget and forecast, limit hits")
    s.add_argument("--days", type=int, default=7, help="how many days back (default 7)")
    s = cmd("advise", cmd_advise, "where the limits stand and what to change, biggest effect first")
    s.add_argument("--session", help="a session id (default: the latest one in this folder)")
    s = cmd("estimate", cmd_estimate, "a job's size and how long it would take, waits for resets included")
    _job_args(s, required=True)
    s.add_argument("--pool", help="the limit to size it against, e.g. hermes:api (default: this session's)")
    s = cmd("setup", cmd_setup, "set up subscriptions, API budgets and Ephemeris: detects what it can, asks the rest")
    s.add_argument("--yes", action="store_true", help="take what was detected (plan prices) and ask nothing")
    s.add_argument("--key", help="an Ephemeris API key to check and keep (- reads it from stdin)")
    cmd("suggest", cmd_suggest, "what to set up here (budgets, prices, ...), each with its command; changes nothing")
    cmd("mcp", cmd_mcp, "run as an MCP server (stdio) for your agent", json=False)
    s = cmd("join", cmd_join, "join a savetokens server with a short code (XXXX-XXXX)")
    s.add_argument("url", help="the hub's address, e.g. http://box.tail1234.ts.net:8787")
    s.add_argument("code", help="the join code, XXXX-XXXX")
    s = cmd("pair", cmd_pair, "a join code (and the one line to paste) for more machines; run it on a machine joined to your hub")
    s.add_argument("--machines", type=int, default=1, help="how many more machines may join with it (up to 50; the hub is already in)")
    s.add_argument("--hours", type=float, default=0.25, help="how long it works (up to 168)")
    s = cmd("connect", cmd_connect, "sync with a savetokens server, so all machines and accounts add up")
    s.add_argument("url", nargs="?", help="the server's address")
    s.add_argument("--token", help="your token on it (`savetokens join` with a code is easier)")
    s.add_argument("--off", action="store_true", help="stop syncing; this machine works on its own")
    s = cmd("server", cmd_server, "run the sync server (with the browser view at /), or add a user to it", json=False)
    s.add_argument("--host", default="127.0.0.1", help="address to listen on (0.0.0.0: every network; default 127.0.0.1)")
    s.add_argument("--port", type=int, default=8787, help="port (default 8787)")
    s.add_argument("--data", help="data directory (default ~/.savetokens/server)")
    s.add_argument("--add-user", metavar="NAME", help="a teammate: their own view, and a token for them")
    s.add_argument("--pair", metavar="NAME", help="a join code for one of NAME's machines")
    s.add_argument("--setup", action="store_true",
                   help="make this machine the hub: starts on boot, prints the line that adds other machines")
    s.add_argument("--public-url", metavar="URL", help="the address other machines reach this hub at (e.g. its HTTPS"
                   " domain): every join command uses it; --setup finds one itself; off forgets it")
    s.add_argument("--stop", action="store_true", help="stop the hub set up with --setup (the data stays)")
    s.add_argument("--provider-key", metavar="PROVIDER", help="anthropic or openai: an admin key on stdin, so the hub"
                                                              " counts API usage no reader saw (nothing piped: remove)")
    s.add_argument("--user", metavar="NAME", help="with --provider-key: whose (default: me, the first user)")
    s.add_argument("--team", metavar="TEAM", help="with --member: add someone to a team (made if new)")
    s.add_argument("--member", metavar="NAME", help="with --team: the person (a new user gets a join code)")
    s.add_argument("--role", choices=["owner", "admin", "member"], help="with --team: their role (default member;"
                                                                         " a team's first member owns it)")
    p.epilog = _overview_help(helps) + "\n\n" + COMMON + "\n\n" + EXIT_CODES
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except Fail as e:
        return _fail(args, e)
    except KeyboardInterrupt:
        return 130
    except Exception as e:   # anything else still answers in the shape the caller asked for
        return _fail(args, Fail(f"{type(e).__name__}: {str(e)[:300]}"))
