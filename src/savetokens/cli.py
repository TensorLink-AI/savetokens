"""savetokens: know when you'll run out of Claude, before you do."""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time

from . import __version__

HOOK_BUDGET_SECONDS = 2


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


def cmd_status(args):
    from . import alerts, forecast, meter, sync
    from .store import Store, load_config
    with Store() as s:
        now = time.time()
        looks = forecast.outlook(s, now)
        if args.json:
            print(json.dumps({"outlook": looks, "models": [dict(zip(("model", "share", "subagents"), m))
                                                           for m in models(s, now - 7 * 86400)], "hits": [dict(r) for r in s.conn.execute(
                "SELECT ts, kind, model FROM hits ORDER BY ts DESC LIMIT 20")]}, indent=2))
            return 0
        cfg = load_config()
        acct = meter.active_account(s, now)
        if not looks:
            print("No limit readings yet. They arrive through the Claude Code statusline: send a message in"
                  " Claude Code, then run this again.")
        src = looks[0]["source"] if looks else None
        made = s.meta("forecast_made_at")
        how = {"ephemeris": "Ephemeris", "baseline": "local baseline", None: "none yet"}[src]
        print(f"account {acct or '?'} · forecast: {how}"
              + (f", made {(now - made) / 3600:.1f}h ago" if made else "")
              + (" · synced with " + sync.settings(cfg)["server_url"] if sync.connected(cfg) else ""))
        for o in looks:
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
    log = (lambda *_: None) if args.quiet else print
    with Store() as s:
        new = maintain.run(s, log=log)
    if not args.quiet:
        for a in new:
            print(a["message"])
        if not new:
            print("up to date; no new alerts")
    return 0


def cmd_backfill(args):
    from . import capture, install
    from .store import Store
    with Store() as s:
        n = capture.backfill(s)
        m = install.import_old_meter(s)
    print(f"read {n:,} new requests" + (f" and {m:,} earlier limit readings" if m else ""))
    return 0


def cmd_install(args):
    from . import install
    if bool(args.server) != bool(args.token):
        print("--server and --token go together")
        return 2
    return 0 if install.install(yes=args.yes, key=args.key, no_ephemeris=args.no_ephemeris,
                                cron=not args.no_schedule, server=args.server, token=args.token) else 1


def cmd_uninstall(args):
    from . import install
    install.uninstall()
    return 0


def cmd_ephemeris(args):
    from . import ephemeris
    from .store import Store, load_config, save_config
    cfg = load_config()
    if args.key:
        ephemeris.save_key(args.key, cfg)
        cfg["forecaster"] = "ephemeris"
    elif args.off:
        cfg["forecaster"] = "baseline"
    elif args.on:
        cfg["forecaster"] = "ephemeris"
    save_config(cfg)
    key = ephemeris.api_key(cfg)
    print(f"forecaster: {cfg['forecaster']}; key: {'found' if key else 'none'} ({ephemeris.SITE})")
    if key and cfg["forecaster"] == "ephemeris":
        try:
            print(f"credits available: {ephemeris.balance(key):,.0f}")
        except Exception as e:
            print(f"key not accepted: {e}")
    with Store() as s:
        last = s.meta("ephemeris_last")
    if last:
        print(f"last forecast: {time.strftime('%a %H:%M', time.localtime(last['made_at']))},"
              f" {last['horizon']}h ahead from {last['history_hours']}h of history, {last['credits']:g} credits")
    return 0


def cmd_connect(args):
    from . import sync
    from .store import Store, load_config, save_config
    cfg = load_config()
    if args.off:
        cfg.pop("server_url", None)
        cfg.pop("server_token", None)
        save_config(cfg)
        print("disconnected; forecasts are made on this machine again")
        return 0
    cfg.update(server_url=args.url, server_token=args.token)
    with Store() as s:
        n = sync.push(s, cfg)
        got = sync.pull(s, cfg)
    save_config(cfg)
    print(f"connected to {args.url}: sent {n:,} rows, received {got['meter']:,} readings from other machines")
    return 0


def cmd_server(args):
    from pathlib import Path

    from . import server
    from .store import home
    data = Path(args.data) if args.data else home() / "server"
    if args.add_user:
        token = server.Users(data).add(args.add_user)
        print(f"user {args.add_user}: token {token}\n(shown once; on each machine run:"
              f" savetokens connect URL --token {token})")
        return 0
    server.serve(data, args.host, args.port)
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["hook"]:
        return _hook()
    if argv[:1] == ["statusline"]:
        return _statusline()
    p = argparse.ArgumentParser(prog="savetokens", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("install", help="add the statusline and hooks to Claude Code, read history")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--key", help="Ephemeris API key")
    s.add_argument("--no-ephemeris", action="store_true", help="forecast locally only")
    s.add_argument("--no-schedule", action="store_true", help="no crontab line")
    s.add_argument("--server", help="URL of your savetokens server (it makes the forecasts)")
    s.add_argument("--token", help="your token on that server")
    s.set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="remove the statusline, hooks and crontab line").set_defaults(fn=cmd_uninstall)
    s = sub.add_parser("status", help="each limit: used now, at reset, and when you'd run out")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)
    sub.add_parser("backfill", help="read transcripts again").set_defaults(fn=cmd_backfill)
    s = sub.add_parser("maintain", help="forecast if due, raise alerts, sync (runs in the background)")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=cmd_maintain)
    s = sub.add_parser("ephemeris", help="the Ephemeris forecaster: key, on/off, credits")
    s.add_argument("--key")
    s.add_argument("--on", action="store_true")
    s.add_argument("--off", action="store_true")
    s.set_defaults(fn=cmd_ephemeris)
    s = sub.add_parser("connect", help="sync with a savetokens server, so all machines and accounts add up")
    s.add_argument("url", nargs="?")
    s.add_argument("--token")
    s.add_argument("--off", action="store_true")
    s.set_defaults(fn=cmd_connect)
    s = sub.add_parser("server", help="run the sync server, or add a user to it")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--data", help="data directory (default ~/.savetokens/server)")
    s.add_argument("--add-user", metavar="NAME")
    s.set_defaults(fn=cmd_server)
    args = p.parse_args(argv)
    if args.cmd == "connect" and not args.off and not (args.url and args.token):
        p.error("connect needs URL and --token (or --off)")
    return args.fn(args)
