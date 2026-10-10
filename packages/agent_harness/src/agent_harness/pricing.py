"""Run cost: prefer what the driver reports, else estimate from tokens.

Drivers that report a cost (Claude Code's `total_cost_usd`) are used as-is.
For the rest, `estimate_cost` multiplies token counts by a per-model price
table (USD per 1M tokens). The default table is a dated snapshot; callers
can pass their own. Unknown models return None, never a guessed number.
"""
from __future__ import annotations

from typing import Any, Optional

PRICES_AS_OF = "2026-09-25"

# cache_write is the 5-minute cache write rate (1.25x input); cache_read is
# 0.1x input unless a model publishes its own (Opus 5.5, Fable 5.1). A missing rate
# falls back to the input rate (an overestimate, never an invented number).
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "claude-fable-5-1":  {"input": 10.00, "output": 50.00, "cache_write": 12.50, "cache_read": 0.25},
    "claude-fable-5":    {"input": 10.00, "output": 50.00, "cache_write": 12.50},   # cache_read unpublished here: billed at input rate
    "claude-opus-5-5":   {"input": 4.00, "output": 20.00, "cache_write": 5.00, "cache_read": 0.20},
    "claude-opus-5":     {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.20},
    "claude-sonnet-5":   {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.20},
    "claude-opus-4-8":   {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-7":   {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-6":   {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-5":   {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5":  {"input": 1.00, "output": 5.00,  "cache_write": 1.25, "cache_read": 0.10},
}

_TOKEN_KEYS = {
    "input": ("input_tokens", "prompt_tokens"),
    "output": ("output_tokens", "completion_tokens"),
    "cache_write": ("cache_creation_input_tokens", "cache_write_tokens"),
    "cache_read": ("cache_read_input_tokens", "cache_read_tokens"),
}


def _price_for(model: str, prices: dict[str, dict[str, float]]) -> Optional[dict[str, float]]:
    if model in prices:
        return prices[model]
    # tolerate dated suffixes, e.g. "claude-haiku-4-5-20251001"
    for known in sorted(prices, key=len, reverse=True):
        if model.startswith(known):
            return prices[known]
    return None


def estimate_cost(model: Optional[str], usage: Optional[dict[str, Any]],
                  prices: Optional[dict[str, dict[str, float]]] = None) -> Optional[float]:
    """USD cost of one run from its usage, or None if it can't be priced."""
    usage = usage or {}
    reported = usage.get("total_cost_usd")
    if isinstance(reported, (int, float)):
        return round(float(reported), 6)
    if not model:
        return None
    rate = _price_for(model, prices or DEFAULT_PRICES)
    if rate is None:
        return None
    total, counted = 0.0, False
    for kind, keys in _TOKEN_KEYS.items():
        tokens = next((usage[k] for k in keys if isinstance(usage.get(k), (int, float))), None)
        if not tokens:
            continue
        counted = True
        total += tokens * rate.get(kind, rate["input"]) / 1_000_000
    return round(total, 6) if counted else None     # no tokens counted: unknown, not $0
