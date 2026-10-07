"""API list prices for Claude models, in USD per million tokens.

Cache writes cost 1.25x input for the 5-minute TTL and 2x for the 1-hour TTL.
Cache reads vary by model, so each row carries its own rate. For subscription
users these dollars are API-equivalent, not what they are billed.
"""
from __future__ import annotations

import re

# prefix: (input, output, cache_read)
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-mythos-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.0),
    "claude-mythos-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-opus-4-5": (5.0, 25.0, 0.50),
    "claude-opus-4-1": (15.0, 75.0, 1.50),
    "claude-opus-4": (15.0, 75.0, 1.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4": (3.0, 15.0, 0.30),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
    "claude-3-5-haiku": (0.8, 4.0, 0.08),
}
_ORDER = sorted(PRICES, key=len, reverse=True)
FAST_MULTIPLIER = 2.0


def normalize(model: str | None) -> str:
    """Strip provider prefixes and context suffixes: 'anthropic/claude-opus-5-5[1m]' -> 'claude-opus-5-5'."""
    m = (model or "").lower().strip()
    m = m.rsplit("/", 1)[-1]
    m = re.sub(r"\[.*?\]$", "", m)
    m = m.replace(".", "-").removeprefix("anthropic-").removeprefix("us-anthropic-")
    return m


def rates(model: str | None):
    m = normalize(model)
    for prefix in _ORDER:
        if m.startswith(prefix):
            return PRICES[prefix]
    return None


def cost(model, *, input=0, output=0, cache_read=0, cache_write_5m=0, cache_write_1h=0, fast=False):
    """Dollar cost of one request, or None when the model has no known price."""
    r = rates(model)
    if r is None:
        return None
    inp, out, read = r
    usd = (input * inp + output * out + cache_read * read
           + cache_write_5m * inp * 1.25 + cache_write_1h * inp * 2.0) / 1e6
    return usd * (FAST_MULTIPLIER if fast else 1.0)


def is_expensive(model) -> bool:
    """Opus and Fable tier: worth flagging when used for exploration subagents."""
    r = rates(model)
    return r is not None and r[0] >= 4.0
