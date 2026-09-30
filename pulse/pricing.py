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


def request_cost(price: Price | None, reported: float | None) -> float:
    """Cost of one request to a request-priced model (Jev)."""
    if reported is not None:
        return reported
    return price.per_request if price is not None else 0.0
