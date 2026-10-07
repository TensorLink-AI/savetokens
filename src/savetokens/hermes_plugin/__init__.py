"""Hermes plugin: savetokens capture, steering and runaway guard.

Every callback swallows its own errors: tracking must never slow or break
Hermes. pre_tool_call is registered only when blocking is enabled, because
Hermes treats a failing pre_tool_call as a block.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)
_local = threading.local()


def _savetokens():
    try:
        import savetokens  # noqa: F401
    except ImportError:
        path_file = Path(__file__).with_name("package_path.txt")
        if path_file.exists():
            sys.path.append(path_file.read_text().strip())   # appended: never shadows Hermes's own modules
    from savetokens.adapters import hermes
    from savetokens.store import Store, load_config
    return hermes, Store, load_config


def _store():
    hermes, Store, _ = _savetokens()
    if getattr(_local, "store", None) is None:
        _local.store = Store()   # one connection per thread
    return _local.store


def _hermes_cost(model, usage, provider, base_url):
    """Price the call with Hermes's own estimator, which knows every provider Hermes supports."""
    try:
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
        keys = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
                "request_count")
        cu = CanonicalUsage(**{k: int(usage.get(k) or 0) for k in keys if k in usage})
        r = estimate_usage_cost(model or "", cu, provider=provider, base_url=base_url)
        amount = float(r.amount_usd) if r.amount_usd is not None else None
        return {"cost_usd": amount, "cost_status": str(r.status), "cost_source": str(r.source)}
    except Exception:
        logger.debug("savetokens: Hermes pricing unavailable", exc_info=True)
        return {}


_priced = set()


def _price_entry(model, provider=None, base_url=None):
    from agent.usage_pricing import get_pricing_entry
    e = get_pricing_entry(model, provider=provider, base_url=base_url)
    if not e or e.input_cost_per_million is None:
        return None
    return {"in": float(e.input_cost_per_million), "out": float(e.output_cost_per_million or 0), "at": time.time()}


def _remember_prices(pairs):
    """Per-million prices for (provider, model, base_url) pairs, from Hermes's own catalogues."""
    prices = {}
    for provider, model, base_url in pairs:
        key = f"{provider or ''}|{model}"
        if not model or key in _priced:
            continue
        _priced.add(key)
        try:
            p = _price_entry(model, provider, base_url)
        except Exception:
            p = None
        if p:
            prices[key] = p
    if prices:
        store = _store()
        store.set_meta("hermes_prices", {**(store.meta("hermes_prices") or {}), **prices})


def _configured_models():
    from hermes_cli.config import load_config
    cfg = load_config() or {}
    out = []

    def add(c):
        if isinstance(c, dict):
            out.append((c.get("provider"), c.get("model") or c.get("default"), c.get("base_url")))
        elif isinstance(c, str) and c:
            out.append((None, c, None))
    add(cfg.get("model"))
    for f in cfg.get("fallback_providers") or []:
        add(f)
    for c in (cfg.get("auxiliary") or {}).values():
        if isinstance(c, dict) and c.get("model"):
            add(c)
    return out


def snapshot_prices():
    """Price every model the user configured, so levers can rank them without calling them first."""
    try:
        _remember_prices(_configured_models())
    except Exception:
        logger.debug("savetokens: price snapshot skipped", exc_info=True)


def record_api(**kwargs):
    try:
        hermes, _, _ = _savetokens()
        usage = kwargs.get("usage")
        try:
            _remember_prices([(kwargs.get("provider"), kwargs.get("response_model") or kwargs.get("model"),
                               kwargs.get("base_url"))])
        except Exception:
            pass
        if isinstance(usage, dict) and "cost_usd" not in kwargs:
            kwargs.update(_hermes_cost(kwargs.get("response_model") or kwargs.get("model"), usage,
                                       kwargs.get("provider"), kwargs.get("base_url")))
        hermes.on_api_request(_store(), **kwargs)
    except Exception:
        logger.debug("savetokens: api request not recorded", exc_info=True)


def warn_in_result(tool_name=None, args=None, result=None, **kwargs):
    """transform_tool_result: append a one-line guard warning to the tool result."""
    try:
        hermes, _, _ = _savetokens()
        msg = hermes.on_tool_result(_store(), tool_name=tool_name, args=args, result=result, **kwargs)
        if msg and isinstance(result, str):
            return f"{result}\n\n[{msg}]"
    except Exception:
        logger.debug("savetokens: guard skipped", exc_info=True)
    return None


def guard_pre_tool(tool_name=None, args=None, **kwargs):
    try:
        hermes, _, _ = _savetokens()
        return hermes.on_pre_tool(_store(), tool_name=tool_name, args=args, **kwargs)
    except Exception:
        logger.debug("savetokens: pre-tool guard skipped", exc_info=True)
        return None


def steer_turn(**kwargs):
    """pre_llm_call: short budget briefing or nudge, injected into the user message (mode permitting)."""
    try:
        hermes, _, _ = _savetokens()
        return hermes.on_pre_llm(_store(), **kwargs)
    except Exception:
        logger.debug("savetokens: steering skipped", exc_info=True)
        return None


def register(ctx):
    threading.Thread(target=snapshot_prices, name="savetokens-prices", daemon=True).start()
    ctx.register_hook("post_api_request", record_api)
    ctx.register_hook("pre_llm_call", steer_turn)
    ctx.register_hook("post_auxiliary_call", record_api)
    ctx.register_hook("transform_tool_result", warn_in_result)
    try:
        _, _, load_config = _savetokens()
        if load_config().get("block"):
            ctx.register_hook("pre_tool_call", guard_pre_tool)
    except Exception:
        logger.debug("savetokens: blocking not enabled", exc_info=True)
