"""Forecasting through Gnomon: providers, and a ledger that keeps every forecast and the actuals.

Gnomon runs the models (Ephemeris by default, the local baseline as fallback)
and records each forecast in its temporal ledger. Hourly demand is appended as
actuals as it comes in, so every forecast is scored against what happened,
per provider: the track record behind the alerts.

Imported only by background upkeep and the server, never by hooks or the statusline.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from . import ephemeris
from .forecast import QUANTILE_GRID, baseline_paths
from .meter import HOUR
from .store import home

UNIT = "pct_of_weekly_limit"   # a subscription pool's; an API pool's is "usd"


def iso(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def series_id(account) -> str:
    return f"demand:{account or 'unknown'}"


def _baseline():
    """A Gnomon provider: hourly quantiles from replayed past days of the request's own history."""
    from gnomon import ForecastResult

    def predict(request):
        start = datetime.fromisoformat(request.future_timestamps[0]).timestamp()
        history_rows = [(datetime.fromisoformat(t).timestamp(), v)
                        for t, v in zip(request.timestamps, request.history)]
        paths = baseline_paths(history_rows, start, request.horizon, n=200)
        n = len(paths) // request.horizon
        rows, point = [], []
        for i in range(request.horizon):
            v = sorted(paths[k * request.horizon + i] for k in range(n))
            q = {p: v[min(n - 1, int(p * n))] for p in request.quantiles}
            rows.append(q)
            point.append(q.get(0.5, v[n // 2]))
        return ForecastResult(point=tuple(point), quantiles=tuple(rows), series_id=request.series_id,
                              unit=request.unit, timestamps=request.future_timestamps)
    return predict


class Engine:
    """One user's engine. ledger_path defaults to ~/.savetokens/ledger.db."""

    def __init__(self, ledger_path=None, use_ephemeris=True, key=None):
        from gnomon import EphemerisProvider, InferenceEngine, TemporalLedger
        self.ledger = TemporalLedger(ledger_path or home() / "ledger.db")
        self.gnomon = InferenceEngine(ledger=self.ledger)
        from gnomon import AdapterCapabilities
        self.gnomon.register("baseline", _baseline(), revision="savetokens-baseline-1",
                             capabilities=AdapterCapabilities(quantiles=True, min_history=1))
        self.providers = ["baseline"]
        key = key or ephemeris.api_key()
        if use_ephemeris and key:
            os.environ.setdefault("EPHEMERIS_API_KEY", key)   # Gnomon reads credentials from the environment
            name = ephemeris.model()
            how = {"mode": "ensemble"} if name == "ensemble" else {"mode": "explicit", "model": name}
            self.gnomon.register("ephemeris", EphemerisProvider(ephemeris.API, **how,
                                                                token_env="EPHEMERIS_API_KEY", timeout=120),
                                 lifecycle="pretrained")
            self.providers.insert(0, "ephemeris")

    def forecast(self, provider, account, history_rows, start_hour, horizon, unit=UNIT):
        """Hourly quantiles [{level: value}] for `horizon` hours from start_hour, recorded in the ledger."""
        from gnomon import ForecastRequest
        req = ForecastRequest(history=tuple(round(v, 4) for _, v in history_rows), horizon=horizon,
                              quantiles=QUANTILE_GRID, frequency="h",
                              timestamps=tuple(iso(h) for h, _ in history_rows),
                              future_timestamps=tuple(iso(start_hour + i * HOUR) for i in range(horizon)),
                              series_id=series_id(account), unit=unit)
        ex = self.gnomon.forecast(provider, req, use_cache=False)
        rows = ex.result.quantiles or tuple({0.5: p} for p in ex.result.point)
        return [{float(k): max(0.0, float(v)) for k, v in r.items()} for r in rows], ex

    def record_actuals(self, account, history_rows, now, unit=UNIT):
        """Hourly demand as actuals. A changed value (a later reading re-spreads a rise) becomes a revision."""
        rows = [{"series_id": series_id(account), "valid_time": iso(h), "value": round(float(v), 4),
                 "source_available_at": iso(min(now, h + HOUR)), "unit": unit} for h, v in history_rows]
        for i in range(0, len(rows), 1000):
            self.ledger.append_actual(actuals=rows[i:i + 1000])

    def track_record(self, account, limit=500):
        """Per provider, over its forecasts with at least a day of outcomes (or all of them, for shorter
        ones): how many, and the mean absolute error per hour, in the series' unit."""
        out = {}
        for provider in self.providers:
            items, cursor = [], None
            while len(items) < 5000:
                page = self.ledger.search(series_id=series_id(account), provider=provider, limit=100, cursor=cursor)
                items += page.get("items", [])
                cursor = page.get("next_cursor")
                if not cursor:
                    break
            ids = [r["execution_id"] for r in items
                   if (r.get("actuals_available") or 0) >= min(24, r.get("horizon") or 1)][-limit:]
            errs = []
            for i in range(0, len(ids), 100):
                errs += [sc["mae"] for sc in self.ledger.evaluate(execution_ids=ids[i:i + 100])
                         if sc.get("n") and sc.get("mae") is not None]
            if errs:
                out[provider] = {"forecasts": len(errs), "mae": sum(errs) / len(errs)}
        return out
