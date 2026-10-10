"""savetokens: know when you'll run out of Claude Code, Codex or your Hermes budget, before you do."""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time

from . import __version__

HOOK_BUDGET_SECONDS = 2

# exit codes, the same for every command (for agents and scripts)
OK, ERROR, USAGE, AT_RISK, WONT_FIT = 0, 1, 2, 3, 4
EXIT_CODES = """exit codes:
  0  done; or (check) every limit on track and the job fits
  1  failed: with --json, {"ok": false, "error": ..., "fix": a command to run} on stdout
  2  bad arguments
  3  (check) a limit is at risk, or the job has to wait for a reset
  4  (check) the job doesn't fit before the limit resets

Every command takes --json: one JSON object on stdout, with "ok": true or false."""


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
    print(f"savetokens {__version__}: pacing alerts and spend forecasts for Claude Code, Codex and Hermes.")
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
    print("\n  savetokens status    each limit, and when you'd run out\n  savetokens spend     tokens and $ by"
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
        rate, ratio = meter.rate(s), meter.five_hour_ratio(s)
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
    n = 0
    with Store() as s:
        rows = s.conn.execute("SELECT rowid, model, input, output, cache_read, cache_write_5m, cache_write_1h"
                              " FROM usage WHERE cost_usd IS NULL AND model IS NOT NULL").fetchall()
        for r in rows:
            c = pricing.cost(r["model"], input=r["input"], output=r["output"], cache_read=r["cache_read"],
                             cache_write_5m=r["cache_write_5m"], cache_write_1h=r["cache_write_1h"])
            if c is not None:
                s.conn.execute("UPDATE usage SET cost_usd = ? WHERE rowid = ?", (c, r["rowid"]))
                n += 1
        s.conn.commit()
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
                   "get a fresh code (codes last 10 minutes, once): `savetokens pair` on a joined machine")
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
        raise Fail("this machine isn't connected to a server", "savetokens join URL CODE")
    got = sync._call(cfg, "/v1/pair", {})
    url = sync.settings(cfg)["server_url"]
    cmd = f"savetokens join {url} {got['code']}"
    _out(args, {"code": got["code"], "expires_in": got.get("expires_in"), "command": cmd,
                "browser": f"{url.rstrip('/')}/#code={got['code']}"},
         f"On the other machine, within 10 minutes:\n  {cmd}\nOr sign in to the browser view with the code:"
         f" {url.rstrip('/')}/")
    return 0


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
    server.serve(data, args.host, args.port)
    return 0


def _job_args(s, required):
    g = s.add_mutually_exclusive_group(required=required)
    g.add_argument("--points", type=float, help="points of the limit")
    g.add_argument("--usd", type=float, help="API-equivalent dollars")
    g.add_argument("--like", help="small, typical, big (past sessions here) or a session id")
    g.add_argument("--hours", type=float, help="hours of work at your pace")
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
    p = argparse.ArgumentParser(prog="savetokens", description=__doc__, epilog=EXIT_CODES,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def cmd(name, fn, help, json=True, **kw):
        s = sub.add_parser(name, help=help, description=help, parents=[common] if json else [], epilog=EXIT_CODES,
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
    s.add_argument("--port", type=int, default=8788)
    s.add_argument("--no-open", action="store_true", help="print the address instead of opening a browser")
    cmd("dashboard", cmd_dashboard, "the dashboard once (--json for other tools, e.g. the Claude Code pane)")
    cmd("backfill", cmd_backfill, "read Claude Code, Codex and Hermes sessions again")
    s = cmd("maintain", cmd_maintain, "forecast if due, raise alerts, sync (runs in the background)")
    s.add_argument("--quiet", action="store_true")
    s = cmd("ephemeris", cmd_ephemeris, "the Ephemeris forecaster: key, on/off, credits")
    s.add_argument("--key", help="your API key (- reads it from stdin)")
    s.add_argument("--model", help="the model to forecast with (default toto2-313m), or ensemble: every"
                                   " model, about 13x the credits")
    s.add_argument("--on", action="store_true")
    s.add_argument("--off", action="store_true")
    s = cmd("api", cmd_api, "Claude Code or Codex on an API key, or Hermes: set a $ budget")
    s.add_argument("tool", choices=["claude-code", "codex", "hermes"])
    s.add_argument("--budget", type=float, metavar="USD")
    s.add_argument("--per", choices=["day", "week", "month"], default="month")
    s.add_argument("--off", action="store_true", help="back to a subscription")
    s = cmd("spend", cmd_spend, "tokens and $ by provider: today, this week, this month, and what's coming")
    s.add_argument("--models", action="store_true", help="each provider's models too")
    s = cmd("plan", cmd_plan, "a subscription's fixed price (Claude Max, ChatGPT Pro, a flat-rate provider)")
    s.add_argument("provider", help="anthropic, openai, or a provider Hermes uses (as `savetokens spend` names it)")
    s.add_argument("--usd", type=float)
    s.add_argument("--per", choices=["day", "week", "month"], default="month")
    s.add_argument("--off", action="store_true", help="no subscription any more")
    s = cmd("price", cmd_price, "the API price of a model savetokens doesn't know ($ per million tokens)")
    s.add_argument("model")
    s.add_argument("input", type=float)
    s.add_argument("output", type=float)
    s.add_argument("cache_read", type=float, nargs="?")
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
    s.add_argument("url")
    s.add_argument("code")
    cmd("pair", cmd_pair, "a join code for another machine, or for the browser view")
    s = cmd("connect", cmd_connect, "sync with a savetokens server, so all machines and accounts add up")
    s.add_argument("url", nargs="?")
    s.add_argument("--token")
    s.add_argument("--off", action="store_true")
    s = cmd("server", cmd_server, "run the sync server (with the browser view at /), or add a user to it", json=False)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--data", help="data directory (default ~/.savetokens/server)")
    s.add_argument("--add-user", metavar="NAME")
    s.add_argument("--pair", metavar="NAME", help="a join code for one of NAME's machines")
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except Fail as e:
        return _fail(args, e)
    except KeyboardInterrupt:
        return 130
    except Exception as e:   # anything else still answers in the shape the caller asked for
        return _fail(args, Fail(f"{type(e).__name__}: {str(e)[:300]}"))
