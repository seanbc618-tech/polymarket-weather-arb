from decimal import Decimal

from polymarket_weather_arb.domain.markets import (
    classify_weather_market,
    parse_market_payload,
    parse_order_book_snapshot,
)


def test_classifies_weather_from_event_metadata():
    assert classify_weather_market(
        "Will event happen?",
        category="Climate",
        tags=("Weather",),
        event_title="Atlantic hurricane season",
    )


def test_rejects_goodweather_name_false_positive():
    assert not classify_weather_market(
        "Will Gary Goodweather win the 2026 Democratic D.C. Mayoral Primary?"
    )


def test_parse_market_payload_extracts_event_metadata():
    market = parse_market_payload(
        {
            "id": "m1",
            "question": "Will a hurricane form by May 31?",
            "slug": "hurricane-form-may-31",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["yes-token", "no-token"]',
            "events": [
                {
                    "slug": "atlantic-hurricane-season",
                    "title": "Atlantic hurricane season",
                    "category": "Climate",
                    "tags": [{"label": "Weather"}],
                }
            ],
        }
    )

    assert market.event_slug == "atlantic-hurricane-season"
    assert market.event_title == "Atlantic hurricane season"
    assert market.category == "Climate"
    assert market.tags == ("Weather",)
    assert market.is_weather is True


def test_parse_order_book_snapshot_supports_bid_only_liquidity():
    snapshot = parse_order_book_snapshot(
        "m1",
        {
            "bids": [{"price": "0.12", "size": "25"}],
            "asks": [],
        },
    )

    assert snapshot.best_bid == Decimal("0.12")
    assert snapshot.best_ask is None
    assert snapshot.midpoint is None
    assert snapshot.spread is None
    assert snapshot.liquidity == Decimal("3.00")


def test_parse_order_book_snapshot_supports_ask_only_liquidity():
    snapshot = parse_order_book_snapshot(
        "m1",
        {
            "bids": [],
            "asks": [{"price": "0.20", "size": "10"}],
        },
    )

    assert snapshot.best_bid is None
    assert snapshot.best_ask == Decimal("0.20")
    assert snapshot.liquidity == Decimal("2.00")


def test_parse_order_book_snapshot_keeps_empty_liquidity_unknown():
    snapshot = parse_order_book_snapshot("m1", {"bids": [], "asks": []})

    assert snapshot.best_bid is None
    assert snapshot.best_ask is None
    assert snapshot.liquidity is None
