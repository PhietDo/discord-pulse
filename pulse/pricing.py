"""Cost of one LLM call in USD."""
from __future__ import annotations

from pulse.config import Price


def cost_usd(
    price: Price | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    reported: float | None,
) -> float:
    if reported is not None:
        return reported
    if price is None:
        return 0.0
    return (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_read_tokens * price.cache_read
    ) / 1_000_000
