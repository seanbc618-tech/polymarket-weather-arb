from __future__ import annotations

from typing import Any

from polymarket_weather_arb.domain.markets import (
    Market,
    parse_market_payload,
    parse_order_book_snapshot,
)


def translate_market(payload: dict[str, Any]) -> Market:
    return parse_market_payload(payload)


def translate_order_book(
    market_id: str, payload: dict[str, Any], *, token_id: str | None = None
):
    return parse_order_book_snapshot(market_id, payload, token_id=token_id)
