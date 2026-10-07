"""savetokens command line."""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from datetime import datetime

from . import __version__

HOOK_BUDGET_SECONDS = 2


def _hook(argv_harness):
    """Claude Code hook entry point. Fails open: any error or overrun prints nothing and exits 0."""
    def _timeout(*_):
        raise TimeoutError
    try:
        signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(HOOK_BUDGET_SECONDS)
    except (AttributeError, ValueError):
        pass
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        from .adapters import claude_code
        from .store import Store
        store = Store()
        try:
            out = claude_code.handle_hook(payload.get("hook_event_name", ""), payload, store)
        finally:
            store.close()
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
        payload = json.loads(raw or "{}")
        from .adapters import claude_code
        from .store import Store
        with Store() as s:
            sys.stdout.write(claude_code.statusline(payload, raw, s))
    except Exception:
        pass
    return 0


def _session_default(s):
    row = s.conn.execute("SELECT session_id FROM usage WHERE harness = 'claude-code' ORDER BY ts DESC LIMIT 1").fetchone()
    return row[0] if row else None


def _fmt_when(ts, kind):
    d = datetime.fromtimestamp(ts)
    return d.strftime("%H:%M") if kind in ("hour", "five_hour") else d.strftime("%a %H:%M")


def cmd_status(args):
    """Actuals and forecasts per window, at session, machine and account level."""
    from . import forecast, maintain, windows
    from .store import Store
    with Store() as s:
        if s.meta("account_plan") is None or not s.conn.execute("SELECT 1 FROM forecast_paths").fetchone():
            maintain.run(s)
        now = time.time()
        sid = args.session or _session_default(s)
        src = forecast.preferred_source(s)
        rows = windows.forecasts(s, now, sid)
        plan = s.meta("account_plan") or {}
        if plan.get("plan"):
            print(f"plan: {plan['plan']} ({plan.get('tier')}), billing: {plan.get('billing')}"
                  + ("; extra usage on: past a limit, usage is billed at API rates" if plan.get("extra_usage") else ""))
        from . import ephemeris
        from .store import load_config
        cfg = load_config()
        if src == "ephemeris":
            made = s.meta("ephemeris_hourly") or {}
            print(f"forecasts: Ephemeris ensemble ({', '.join(made.get('models', [])[:4])}…), updated"
                  f" {(now - made.get('made_at', now)) / 3600:.1f}h ago")
        elif cfg.get("forecaster", "ephemeris") == "baseline":
            print("forecasts: local baseline (Ephemeris turned off; `savetokens ephemeris connect` to turn it on)")
        elif not ephemeris.api_key(cfg):
            print(f"forecasts: local baseline until you add an Ephemeris key ({ephemeris.SITE}):"
                  " savetokens ephemeris connect --key <key>")
        else:
            print("forecasts: local baseline until the first Ephemeris forecast arrives (within minutes)")
        names = {"hour": "this hour", "five_hour": "5-hour window", "day": "today", "week": "this week"}
        sub_any = any(r["pct"] for r in rows)
        if sub_any:
            print(f"\nsubscription: % of the limit shown on each row (forecast: {src}; p10–p90)")
            print(f"  {'window':14} {'session':>8} {'machine':>8} {'account':>8}   {'forecast at end':22} ends")
            for r in rows:
                p = r["pct"]
                if not p:
                    continue
                f = lambda v: f"{v:7.1f}%" if v is not None else "      –"
                fc = p["forecast"].get(src)
                fcs = f"{fc[1]:5.1f}% ({fc[0]:.1f}–{fc[2]:.1f}%)" if fc else "–"
                warn = "  ⚠ could hit the limit" if fc and fc[2] >= 100 and p["limit_window"] else ""
                print(f"  {names[r['kind']]:14} {f(p['session'])} {f(p['machine'])} {f(p['account'])}   {fcs:22}"
                      f" {_fmt_when(r['end'], r['kind'])}  [{p['unit_of']}]{warn}")
            print("  session = this session; machine = all sessions here; account = Claude Code's own reading"
                  " (every device and claude.ai)")
            from . import limits
            ls = limits.learning_state(s)
            if not ls["settled"]:
                print(f"  learning your limits: {ls['readings']} readings over {ls['span_hours']:.1f}h so far."
                      f" Percentages and ranges are rough until {limits.SETTLE_READINGS} readings over"
                      f" {limits.SETTLE_SPAN // 3600}h+ (usually the first day of use).")
        api_rows = [r for r in rows if r["kind"] != "five_hour" and (r["units"]["api_usd"]["machine"]
                                                                      or r["units"]["api_usd"]["forecast"])]
        if api_rows and (any(r["units"]["api_usd"]["machine"] for r in api_rows) or not sub_any):
            print(f"\nAPI-billed dollars (forecast: {src}; p10–p90)")
            print(f"  {'window':14} {'session':>9} {'machine':>9}   {'forecast at end':24} ends")
            for r in api_rows:
                u = r["units"]["api_usd"]
                fc = u["forecast"].get(src)
                fcs = f"${fc[1]:,.2f} (${fc[0]:,.0f}–{fc[2]:,.0f})" if fc else "–"
                sess = f"${u['session']:8,.2f}" if u["session"] is not None else "        –"
                print(f"  {names[r['kind']]:14} {sess} ${u['machine']:8,.2f}   {fcs:24} {_fmt_when(r['end'], r['kind'])}")
        from . import levers
        from .adapters import codex
        cx = codex.pressure(s, now)
        if cx:
            print(f"\nCodex (from its own limit readings; forecast: {cx[0]['source']})")
            for w in cx:
                fc = w["forecast"]
                print(f"  {w['name']:20} {w['used']:5.1f}% used" + (f", {fc[1]:.0f}% expected ({fc[0]:.0f}–{fc[2]:.0f}%),"
                                                                     f" {w['p_hit']:.0%} chance of a hit" if fc else "")
                      + f", resets {_fmt_when(w['resets'], 'week')}")
        from . import budgets
        rows = [budgets.pace(s, b, now) for b in budgets.configured()]
        if rows:
            print("\nbudgets (pay-as-you-go)")
            for p in rows:
                print(_budget_line(p))
        on = levers.active(s)
        if on:
            print(f"\neconomising until the risk passes: {', '.join(on)}  (savetokens levers)")
        if not s.latest_limits("claude-code"):
            print("no limit readings yet: they arrive through the Claude Code statusline once installed")
    return 0


def cmd_learn(args):
    """Show what savetokens has learned about how usage moves your limits, per model."""
    from . import limits
    from .store import Store
    with Store() as s:
        cal = limits.calibrate(s, force=args.refit)
        if not cal:
            print("Nothing learned yet: needs statusline readings with limit percentages.")
            return 0
        names = {"five_hour": "5-hour limit", "seven_day": "weekly limit"}
        for window, f in cal.items():
            r2 = f"R² {f['r2']:.2f}" if f["r2"] is not None else "R² n/a (readings don't vary yet)"
            print(f"{names[window]}: {f['n']} readings, {r2}")
            print(f"  pooled: {f['pooled']:.4f}% per API-equivalent $  (≈ ${100 / f['pooled']:,.0f} fills it)"
                  if f["pooled"] else "  pooled: no signal yet")
            for m, w in sorted(f["models"].items(), key=lambda kv: -f["usd"].get(kv[0], 0)):
                fills = f"≈ ${100 / w:,.0f} fills it" if w else "no signal"
                print(f"  {m:22} {w:.4f}% per $  ({fills})")
        hist = limits.history(s, "seven_day")
        if len(hist) > 1:
            print("weekly pooled rate over time:")
            for h in hist[-8:]:
                print(f"  {datetime.fromtimestamp(h['ts']).strftime('%m-%d %H:%M')}  {h['pct_per_usd']:.4f}%/$"
                      f"  n={h['n']}  tier={h['tier']}")
        print("Usage on other devices or claude.ai also counts towards limits but isn't visible here,"
              " so these rates can read high.")
    return 0


def cmd_maintain(args):
    from . import maintain
    from .store import Store
    with Store() as s:
        maintain.run(s, log=(lambda *_: None) if args.quiet else print)
    return 0


def cmd_report(args):
    from . import report
    from .store import Store
    with Store() as s:
        if args.refresh:
            _refresh(s)
        r = report.build(s, days=args.days, harness=args.harness)
        if args.json:
            r["findings"] = [f.__dict__ for f in r["findings"]]
            print(json.dumps(r, indent=2, default=str))
        else:
            print(report.render(r))


def _refresh(s):
    from .adapters import claude_code, hermes
    claude_code.backfill(s)
    if hermes.hermes_home().exists():
        hermes.backfill(s)


def cmd_backfill(args):
    from .store import Store
    with Store() as s:
        from .adapters import claude_code, hermes
        if args.harness in (None, "claude-code"):
            print(f"claude-code: {claude_code.backfill(s):,} new requests")
        if args.harness in (None, "hermes") and hermes.hermes_home().exists():
            print(f"hermes: {hermes.backfill(s):,} new rows")
        from .adapters import codex
        if args.harness in (None, "codex") and codex.transcripts():
            print(f"codex: {codex.backfill(s):,} new turns (and limit readings)")


def cmd_install(args):
    from . import install
    if args.harness == "hermes":
        ok = install.install_hermes(yes=args.yes, block=args.block, notify=args.notify, restart=not args.no_restart,
                                    ephemeris_key=args.ephemeris_key, no_ephemeris=args.no_ephemeris,
                                    levers=not args.no_levers)
    else:
        ok = install.install_claude_code(yes=args.yes, block=args.block, schedule_upkeep=not args.no_schedule,
                                         ephemeris_key=args.ephemeris_key, no_ephemeris=args.no_ephemeris,
                                         levers=not args.no_levers)
    return 0 if ok else 1


def cmd_uninstall(args):
    from . import install
    {"claude-code": install.uninstall_claude_code, "hermes": install.uninstall_hermes}[args.harness]()


def cmd_fixes(args):
    from . import fixes
    from .store import Store
    with Store() as s:
        active = fixes.active(s)
        if args.action == "list" or not args.fix:
            for f in fixes.FIXES.values():
                mark = "applied" if f.id in active else "       "
                print(f"  {mark}  {f.id:16} {f.summary}")
            return 0
        if args.fix not in fixes.FIXES:
            print(f"unknown fix {args.fix}; see `savetokens fixes list`")
            return 1
        if args.action == "plan":
            print(fixes.plan(args.fix, s))
        elif args.action == "apply":
            print(fixes.plan(args.fix, s))
            if not args.yes and input("Apply? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Nothing changed.")
                return 1
            fixes.apply(s, args.fix)
            print(f"Applied. Undo with `savetokens fixes revert {args.fix}`. Restart Claude Code to pick it up.")
        elif args.action == "revert":
            fixes.revert(s, args.fix)
            print("Reverted.")
        elif args.action == "impact":
            r = fixes.impact(s, args.fix)
            if not r:
                print("never applied")
            elif r["ratio"] is None:
                print(f"not enough sessions yet ({r['before']} before, {r['after']} after; need 3 each)")
            else:
                print(f"$/turn after vs before: {r['ratio']:.2f}x (90% interval {r['lo']:.2f}–{r['hi']:.2f}),"
                      f" {r['before']} sessions before, {r['after']} after."
                      " Observational: other changes in how you worked also show up here.")
    return 0


def cmd_guard(args):
    from .store import load_config, save_config
    cfg = load_config()
    if args.block:
        cfg["block"] = args.block == "on"
        save_config(cfg)
        print(f"blocking {'on' if cfg['block'] else 'off'}."
              " For Claude Code, re-run `savetokens install claude-code` so the PreToolUse hook matches;"
              " for Hermes, restart it.")
    print(json.dumps({k: v for k, v in cfg.items() if not k.startswith("statusline")}, indent=2))


def cmd_mode(args):
    """The quality knob: how much savetokens steers the agent."""
    from . import steer
    from .store import Store, load_config, save_config
    cfg = load_config()
    if args.mode:
        cfg["mode"] = args.mode
        save_config(cfg)
    with Store() as s:
        mode, why = steer.effective_mode(s, cfg=cfg)
    configured = steer.configured_mode(cfg)
    print(f"mode: {configured}" + (f" (now {mode}; {why})" if configured == "auto" else ""))
    for name, text in (("quality", "monitoring only; nothing added to the agent's context but late guard warnings"),
                       ("balanced", "repo lessons at session start; budget and a nudge only when a limit is at risk"),
                       ("lean", "always asks for economical work; nudges from 80% of a limit; tighter guard"),
                       ("auto", "lean when a limit is forecast to be hit, quality with plenty of headroom")):
        print(f"  {'*' if name == configured else ' '} {name:9} {text}")
    if args.mode is None:
        print("change with: savetokens mode <name>  (SAVETOKENS_MODE overrides it for one session)")


def cmd_budget(args):
    from . import steer
    from .store import Store, load_config, save_config
    if args.daily_usd is not None:
        cfg = load_config()
        if args.daily_usd > 0:
            cfg["daily_budget_usd"] = args.daily_usd
        else:
            cfg.pop("daily_budget_usd", None)
        save_config(cfg)
    with Store() as s:
        b = steer.budget(s, session_id=args.session)
    if args.json:
        print(json.dumps(b, indent=2))
        return
    print(f"mode: {b['mode']}" + (f" ({b['mode_reason']})" if b["configured_mode"] == "auto" else ""))
    for w in b["limits"]:
        fc = w["forecast"]
        print(f"  {w['name']:18} {w['used']:5.1f}% used" + (f", forecast {fc[1]:.0f}% ({fc[0]:.0f}–{fc[2]:.0f}%)"
                                                             if fc else "") + f" at reset in {w['resets_in_min']} min")
    if not b["limits"]:
        print("  no limit readings yet (API billing: set a daily budget with `savetokens budget --daily-usd N`)")
    if b["advice"]:
        print(f"advice: {b['advice']}")


def cmd_briefing(args):
    """Show exactly what the agent receives at session start and on the next prompt."""
    import os
    from . import steer
    from .store import Store
    with Store() as s:
        text = steer.briefing(s, cwd=args.cwd or os.getcwd())
    print(text or "(nothing: the agent's context is left alone in this mode and state)")


def cmd_backtest(args):
    """Replay real usage: would forecasts have kept you under your limits?"""
    from . import backtest, limits
    from .store import Store
    with Store() as s:
        if args.show and s.meta("backtest"):
            r = s.meta("backtest")
        else:
            r = backtest.run(s, hit_rates=tuple(float(x) / 100 for x in args.hit_rates.split(",")),
                             lean=args.lean, use_ephemeris=not args.no_ephemeris,
                             log=(lambda *_: None) if args.json else (lambda m: print(m, file=sys.stderr)))
        rate = limits.effective_rate(s, "five_hour", time.time())
    if args.json:
        print(json.dumps(r, indent=2))
    else:
        print(backtest.render(r, 100 / rate if rate else None))


def cmd_levers(args):
    """What savetokens may change when a limit is at risk, and what it has changed now."""
    from . import levers
    from .store import Store, load_config, save_config
    cfg = load_config()
    with Store() as s:
        if args.action == "on":
            cfg["levers_consent"] = ["claude-code", "codex", "hermes"]
            cfg["levers"] = args.allow.split(",") if args.allow else list(levers.DEFAULT_LEVERS)
            save_config(cfg)
        elif args.action == "off":
            cfg["levers"] = "off"
            save_config(cfg)
            done = levers.revert(s, why="turned off")
            print("levers off" + (f"; undid {', '.join(done)}" if done else ""))
            return 0
        elif args.action == "revert":
            done = levers.revert(s, why="reverted by hand", cooldown=True)
            print(f"undid {', '.join(done)}" if done else "nothing to undo")
            return 0
        allowed = levers.allowed(cfg)
        print(f"allowed: {', '.join(allowed) if allowed else 'none (savetokens levers on)'}"
              f" for {', '.join(h for h in levers.LEVERS if levers.consented(h, cfg)) or 'no harness'}"
              f"   applied when the chance of a hit reaches {levers.APPLY_RISK:.0%} (auto) or always (lean);"
              f" undone below {levers.REVERT_RISK:.0%}, at reset, or when tests fail or a loop starts")
        for h, items in levers.LEVERS.items():
            risk, resets, name = levers._risk(s, h, time.time())
            on = levers.applied(s).get(h, {})
            print(f"\n{h}: {risk:.0%} chance of hitting the {name}" if name else f"\n{h}: no limit forecast yet")
            for lv in items:
                state = "ON" if lv.id in on else ("allowed" if levers.is_allowed(lv, allowed) else "off")
                print(f"  {state:8} {lv.id:13} {lv.describe:46} {lv.key or 'auxiliary.<task>.model'} in {lv.path()}")
    return 0


def cmd_study(args):
    """Experimental: a cheap model reads session skeletons (no content) and proposes changes."""
    from . import study
    from .store import Store
    with Store() as s:
        r = study.run(s, days=args.days, n=args.sessions, model=args.model, dry_run=args.dry_run)
    if args.dry_run:
        print(r["prompt"])
        print(f"\n({r['sessions']} sessions, about {len(r['prompt']) // 4:,} tokens; nothing was sent)")
        return 0
    if args.json:
        print(json.dumps(r, indent=2))
        return 0
    print(f"study of {r['sessions']} sessions with {r['model']} (${r['cost_usd'] or 0:.3f}); proposals only,"
          " nothing is applied:")
    for f in r["findings"]:
        tag = "NEW" if f.get("category") == "new" else f.get("category", "?")
        print(f"\n  [{tag}] {f.get('pattern')}\n    change: {f.get('change')}\n    measure: {f.get('measure')}")
    if not r["new"]:
        print("\nNothing beyond the report's own categories: `savetokens report` already covers these.")
    return 0


def _budget_line(p):
    when = lambda ts: datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M")
    fc = p["forecast"]
    line = f"  {p['name']:18} {p['describe']:34} ${p['spent']:,.2f} spent"
    if fc:
        line += f", ${fc[1]:,.0f} expected (${fc[0]:,.0f}–{fc[2]:,.0f}), {p['p_over']:.0%} chance of going over"
    if p["runs_out"]:
        line += f"; at this pace it runs out {when(p['runs_out'])}"
    return line + f" (resets {when(p['resets'])}; {p['source']})"


def cmd_budgets(args):
    """Pay-as-you-go budgets per provider, harness or all API spend, paced with the forecast."""
    from . import budgets
    from .store import Store, load_config, save_config
    cfg = load_config()
    items = list(cfg.get("budgets") or [])
    if args.action == "add":
        if not args.usd or args.period not in budgets.PERIODS:
            print("usage: savetokens budgets add NAME --usd 200 --period day|week|month"
                  " [--provider openrouter] [--harness hermes]")
            return 1
        b = {"name": args.name, "usd": args.usd, "period": args.period}
        b.update({k: v for k, v in (("provider", args.provider), ("harness", args.harness)) if v})
        items = [x for x in items if x.get("name") != args.name] + [b]
        cfg["budgets"] = items
        save_config(cfg)
        print(f"added {args.name}: {budgets.describe(b)}")
    elif args.action == "remove":
        cfg["budgets"] = [x for x in items if x.get("name") != args.name]
        if args.name == "daily":
            cfg.pop("daily_budget_usd", None)
        save_config(cfg)
        print(f"removed {args.name}")
    elif args.action == "connect":
        if args.name != "openrouter":
            print("only openrouter reports its own spend so far: savetokens budgets connect openrouter")
            return 1
        if args.env_file:
            cfg["openrouter_env_file"] = str(__import__("pathlib").Path(args.env_file).expanduser().resolve())
            save_config(cfg)
        if not budgets.openrouter_key(cfg):
            print("No OPENROUTER_API_KEY found: set it, or pass --env-file (e.g. ~/.hermes/.env); the key is not copied")
            return 1
        with Store() as s:
            k = budgets.fetch_openrouter(s)
        print(f"connected: today ${k.get('usage_daily') or 0:,.2f}, this week ${k.get('usage_weekly') or 0:,.2f},"
              f" this month ${k.get('usage_monthly') or 0:,.2f}"
              + (f", ${k['limit_remaining']:,.2f} left on the key's limit" if k.get("limit_remaining") is not None
                 else ""))
        return 0
    with Store() as s:
        rows = [budgets.pace(s, b) for b in budgets.configured()]
    if not rows:
        print("no budgets yet: savetokens budgets add openrouter --usd 200 --period month --provider openrouter")
        return 0
    print("budgets (subscription usage never counts; dollars per call come from the harness):")
    for p in rows:
        print(_budget_line(p))
    return 0


def cmd_ephemeris(args):
    from . import ephemeris
    from .store import load_config, save_config
    cfg = load_config()
    if args.action == "connect":
        if args.env_file:
            cfg["ephemeris_env_file"] = str(__import__("pathlib").Path(args.env_file).expanduser().resolve())
        if args.key:
            ephemeris.save_key(args.key, cfg)
        key = ephemeris.api_key(cfg)
        if not key:
            print(f"No key found. Get one at {ephemeris.SITE}, then run `savetokens ephemeris connect --key <key>`"
                  " (or set EPHEMERIS_API_KEY, or pass --env-file).")
            return 1
        try:
            credits = ephemeris.balance(key)
        except Exception as e:
            print(f"Ephemeris didn't accept the key: {e}")
            return 1
        cfg["forecaster"] = "ephemeris"
        save_config(cfg)
        print(f"Connected: {credits:,.0f} credits available. Forecasts now use the Ephemeris ensemble"
              " (refreshed every 6 hours; only hourly dollar totals are sent).")
        from . import maintain
        from .store import Store
        with Store() as s:
            s.set_meta("ephemeris_last_try", 0)
            maintain.run(s)
    elif args.action == "disconnect":
        cfg["forecaster"] = "baseline"
        save_config(cfg)
        print("Forecasts are local only (the baseline). Reconnect with `savetokens ephemeris connect`.")
    return 0


def cmd_forecast(args):
    """Ephemeris against the local baseline: windows now, per-day outlook, and the scored track record."""
    from . import maintain, windows
    from .store import Store, load_config
    with Store() as s:
        if args.refresh:
            s.set_meta("ephemeris_last_try", 0)
            s.set_meta("ephemeris_hourly", None)
            s.set_meta("maintain_last_try", 0)
            maintain.run(s, log=(lambda *_: None) if args.quiet else print)
        if args.quiet:
            return 0
        now = time.time()
        eph = s.meta("ephemeris_hourly")
        if load_config().get("forecaster") == "ephemeris" and eph:
            print(f"Ephemeris: {eph['horizon']}h hourly forecast made "
                  f"{datetime.fromtimestamp(eph['made_at']).strftime('%a %H:%M')} ({eph['credits']:g} credits)")
        else:
            print("Ephemeris not connected or not run yet: showing the local baseline only.")
        rate = __import__("savetokens.limits", fromlist=["x"]).effective_rate(s, "seven_day", now)
        for unit, label in (("sub_usd", "subscription"), ("api_usd", "API-billed")):
            days = {src: dict(windows.per_day(s, src, unit, now)) for src in ("ephemeris", "baseline")}
            if not any(days.values()):
                continue
            pct = unit == "sub_usd" and rate
            print(f"\n{label} usage per day" + (" (% of weekly limit)" if pct else " ($)") + ", p50 (p10–p90):")
            print(f"  {'day':12} {'Ephemeris':>24} {'baseline':>24}")
            for d in sorted(set(days["ephemeris"]) | set(days["baseline"])):
                cells = []
                for src in ("ephemeris", "baseline"):
                    b = days[src].get(d)
                    if not b:
                        cells.append(f"{'–':>24}")
                    elif pct:
                        cells.append(f"{b[1] * rate:7.2f}% ({b[0] * rate:.1f}–{b[2] * rate:.1f}%)".rjust(24))
                    else:
                        cells.append(f"${b[1]:,.2f} (${b[0]:,.0f}–{b[2]:,.0f})".rjust(24))
                print(f"  {d:12} {cells[0]} {cells[1]}")
        sc = windows.score(s, now)
        print("\ntrack record (matured window forecasts, scored on this machine's usage):")
        if not sc:
            print("  none yet: each window is scored when it ends (first hourly scores within the hour)")
        for (kind, unit, src), t in sorted(sc.items()):
            print(f"  {kind:10} {unit:8} {src:10} n={t['n']:4}  {t['hit_rate']:4.0%} inside p10–p90"
                  f"  pinball {t['pinball']:.3f} (lower is better)")
    return 0


def cmd_notify(args):
    """For cron delivery: run upkeep, then print new alerts (and the daily summary with --daily). Silent otherwise."""
    from . import maintain, notify
    from .store import Store
    with Store() as s:
        try:
            if args.harness in (None, "hermes"):
                from .adapters import hermes
                if hermes.hermes_home().exists():
                    hermes.backfill(s)
            maintain.run(s)
        except Exception:
            pass
        lines = notify.alerts(s)
        if args.daily:
            lines.append(notify.daily(s))
        if lines:
            print("\n".join(lines))
    return 0


def cmd_feedback(args):
    from .store import home
    text = " ".join(args.text) or input("What worked, what didn't? ")
    home().mkdir(parents=True, exist_ok=True)
    with open(home() / "feedback.txt", "a") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{text}\n")
    print(f"Saved locally to {home() / 'feedback.txt'}. Nothing is sent anywhere.")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["hook"]:
        return _hook(argv[1:2])
    if argv[:1] == ["statusline"]:
        return _statusline()
    p = argparse.ArgumentParser(prog="savetokens", description="Stop runaway agent sessions and cut token waste.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    harnesses = ["claude-code", "hermes"]

    s = sub.add_parser("install", help="add hooks or plugin and backfill history")
    s.add_argument("harness", choices=harnesses)
    s.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    s.add_argument("--block", action="store_true", help="also block repeated runaway calls (off by default)")
    s.add_argument("--no-schedule", action="store_true", help="Claude Code: don't add the hourly crontab line")
    s.add_argument("--notify", default="local",
                   help="Hermes: where cron alerts and the daily summary go (local, telegram, ...)")
    s.add_argument("--no-restart", action="store_true", help="Hermes: don't restart the gateway")
    s.add_argument("--ephemeris-key", help="Ephemeris API key for forecasts (the default forecaster)")
    s.add_argument("--no-ephemeris", action="store_true", help="keep forecasts local (no network)")
    s.add_argument("--no-levers", action="store_true",
                   help="never change models, effort or compaction settings when a limit is at risk")
    s.set_defaults(fn=cmd_install)
    s = sub.add_parser("uninstall")
    s.add_argument("harness", choices=harnesses)
    s.set_defaults(fn=cmd_uninstall)
    s = sub.add_parser("backfill", help="import history from harness logs")
    s.add_argument("harness", nargs="?", choices=harnesses + ["codex"])
    s.set_defaults(fn=cmd_backfill)
    s = sub.add_parser("status", help="actuals and forecasts: this hour, 5-hour window, today, this week")
    s.add_argument("--session", help="session id for the session column (default: the latest)")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("report", help="where tokens went and the biggest waste")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--harness", choices=harnesses)
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-refresh", dest="refresh", action="store_false")
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser("fixes", help="list, plan, apply, revert or measure fixes")
    s.add_argument("action", choices=["list", "plan", "apply", "revert", "impact"], nargs="?", default="list")
    s.add_argument("fix", nargs="?")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_fixes)
    s = sub.add_parser("guard", help="show or change guard settings")
    s.add_argument("--block", choices=["on", "off"])
    s.set_defaults(fn=cmd_guard)
    s = sub.add_parser("capabilities", help="machine-readable summary for agents")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=lambda a: print(__import__("savetokens.install", fromlist=["x"]).capabilities_json()))
    s = sub.add_parser("mode", help="the quality knob: quality, balanced, lean or auto")
    s.add_argument("mode", nargs="?", choices=["quality", "balanced", "lean", "auto"])
    s.set_defaults(fn=cmd_mode)
    s = sub.add_parser("budget", help="limits used and forecast at reset, with advice (--json for agents)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--session")
    s.add_argument("--daily-usd", type=float, help="set a daily budget for API-billed usage (0 removes it)")
    s.set_defaults(fn=cmd_budget)
    s = sub.add_parser("briefing", help="what the agent is told at session start, for a project directory")
    s.add_argument("--cwd")
    s.set_defaults(fn=cmd_briefing)
    s = sub.add_parser("levers", help="what savetokens may change when a limit is at risk (models, effort, compaction)")
    s.add_argument("action", nargs="?", choices=["status", "on", "off", "revert"], default="status")
    s.add_argument("--allow", help="comma list of levers, e.g. subagents,effort; prefix a harness to limit one"
                                   " to it, e.g. hermes:compaction (default: subagents,effort,side-tasks,quality-score)")
    s.set_defaults(fn=cmd_levers)
    s = sub.add_parser("study", help="experimental: a cheap model proposes changes from session skeletons")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--sessions", type=int, default=6)
    s.add_argument("--model", default="claude-haiku-4-5")
    s.add_argument("--dry-run", action="store_true", help="print exactly what would be sent, send nothing")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_study)
    s = sub.add_parser("budgets", help="pay-as-you-go budgets per provider or harness, paced with the forecast")
    s.add_argument("action", nargs="?", choices=["list", "add", "remove", "connect"], default="list")
    s.add_argument("name", nargs="?")
    s.add_argument("--usd", type=float)
    s.add_argument("--period", choices=["day", "week", "month"], default="month")
    s.add_argument("--provider", help="e.g. openrouter, api.engy.ai (the API host for custom endpoints)")
    s.add_argument("--harness", help="e.g. hermes")
    s.add_argument("--env-file", help="connect openrouter: a file with OPENROUTER_API_KEY= (not copied)")
    s.set_defaults(fn=cmd_budgets)
    s = sub.add_parser("backtest", help="replay your usage: would forecasts have kept you under your limits?")
    s.add_argument("--lean", type=float, default=0.2, help="share of usage lean mode saves while on (default 0.2)")
    s.add_argument("--hit-rates", default="10,25,40", help="limits that this %% of past windows would hit")
    s.add_argument("--no-ephemeris", action="store_true", help="baseline only (no API calls)")
    s.add_argument("--show", action="store_true", help="show the last result without rerunning")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_backtest)
    s = sub.add_parser("ephemeris", help="connect or disconnect the Ephemeris forecaster")
    s.add_argument("action", choices=["connect", "disconnect"])
    s.add_argument("--key", help="your Ephemeris API key (stored owner-only in the savetokens home)")
    s.add_argument("--env-file", help="file holding EPHEMERIS_API_KEY or EPHEMERIS_API_TOKEN (not copied)")
    s.set_defaults(fn=cmd_ephemeris)
    s = sub.add_parser("forecast", help="Ephemeris vs local baseline: per-day outlook and track record")
    s.add_argument("--refresh", action="store_true", help="fetch now instead of using the cached forecast")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=cmd_forecast)
    s = sub.add_parser("learn", help="what savetokens has learned about your limits, per model")
    s.add_argument("--refit", action="store_true")
    s.set_defaults(fn=cmd_learn)
    s = sub.add_parser("maintain", help="refit limit rates and refresh forecasts (run in the background)")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=cmd_maintain)
    s = sub.add_parser("notify", help="new alerts for cron delivery (silent when nothing is new)")
    s.add_argument("--daily", action="store_true", help="also print the daily summary")
    s.add_argument("--harness", choices=harnesses)
    s.set_defaults(fn=cmd_notify)
    s = sub.add_parser("feedback", help="leave feedback (stored locally)")
    s.add_argument("text", nargs="*")
    s.set_defaults(fn=cmd_feedback)
    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
