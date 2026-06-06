"""Anthropic public pricing (USD per million tokens). Update when prices change.

Used by both the agent (when computing the ledger) and the dashboard
(for `Without cache` counterfactuals). Keep this file as the single source.
"""
from __future__ import annotations

# (input, output, cache_write_5m, cache_read) per million tokens
PRICING: dict[str, tuple[float, float, float, float]] = {
    "opus-4-7":   (15.00, 75.00, 18.75, 1.50),
    "sonnet-4-6": ( 3.00, 15.00,  3.75, 0.30),
    "haiku-4-5":  ( 0.80,  4.00,  1.00, 0.08),
}


def cost(model: str, input_tok: int, output_tok: int,
         cache_write_tok: int = 0, cache_read_tok: int = 0) -> float:
    code = _normalize(model)
    if code not in PRICING:
        return 0.0
    pi, po, pcw, pcr = PRICING[code]
    return (
        input_tok       * pi  / 1_000_000
        + output_tok    * po  / 1_000_000
        + cache_write_tok * pcw / 1_000_000
        + cache_read_tok  * pcr / 1_000_000
    )


def cost_without_cache(model: str, total_input_equiv: int, output_tok: int) -> float:
    """Counterfactual: what it would cost if every cache hit had been a fresh input."""
    code = _normalize(model)
    if code not in PRICING:
        return 0.0
    pi, po, _, _ = PRICING[code]
    return total_input_equiv * pi / 1_000_000 + output_tok * po / 1_000_000


def _normalize(model: str) -> str:
    m = model.lower()
    if "opus" in m:   return "opus-4-7"
    if "sonnet" in m: return "sonnet-4-6"
    if "haiku" in m:  return "haiku-4-5"
    return m
