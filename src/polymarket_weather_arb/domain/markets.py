from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
import re

WEATHER_KEYWORDS = (
    "weather",
    "temperature",
    "temp",
    "rain",
    "rainfall",
    "precipitation",
    "snow",
    "snowfall",
    "noaa",
    "nws",
    "degrees",
    "°f",
    "°c",
)
NON_WEATHER_KEYWORDS = (
    "hurricanes win",
    "carolina hurricanes",
    "miami hurricanes",
    "mayweather",
    "goodweather",
    "government shutdown",
    "blockade",
)


@dataclass(frozen=True)
class Market:
    id: str
    title: str
    slug: str | None = None
    description: str | None = None
    event_slug: str | None = None
    event_title: str | None = None
    category: str | None = None
    tags: tuple[str, ...] = ()
    yes_token_id: str | None = None
    no_token_id: str | None = None
    close_time: str | None = None
    status: str | None = None
    is_weather: bool = False


@dataclass(frozen=True)
class MarketSnapshot:
    market_id: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    midpoint: Decimal | None
    spread: Decimal | None
    liquidity: Decimal | None
    fetched_at: datetime
    # Outcome token (asset) ID when known. Required for multi-outcome quote history.
    token_id: str | None = None


def classify_weather_market(
    title: str,
    description: str | None = None,
    *,
    category: str | None = None,
    tags: tuple[str, ...] = (),
    event_title: str | None = None,
    event_slug: str | None = None,
) -> bool:
    text = " ".join(
        part
        for part in [
            title,
            description or "",
            category or "",
            " ".join(tags),
            event_title or "",
            event_slug or "",
        ]
        if part
    ).lower()
    if any(keyword in text for keyword in NON_WEATHER_KEYWORDS):
        return False
    metadata_text = " ".join(
        part
        for part in [category or "", " ".join(tags), event_title or "", event_slug or ""]
        if part
    ).lower()
    metadata_weather = bool(
        re.search(
            r"\b(weather|climate|hurricane|storm|temperature|rain|snow|arctic|sea ice)\b",
            metadata_text,
        )
    )
    text_weather = bool(
        re.search(
            r"\b(weather|temperature|temp|rain(?:fall)?|precipitation|snow(?:fall)?|noaa|nws|hurricane|storm)\b|°[fc]",
            text,
        )
    )
    return metadata_weather or text_weather


def parse_market_payload(payload: dict[str, Any]) -> Market:
    title = str(
        payload.get("question") or payload.get("title") or payload.get("name") or ""
    ).strip()
    market_id = str(
        payload.get("id") or payload.get("conditionId") or payload.get("slug") or title
    ).strip()
    outcomes = _jsonish_list(payload.get("outcomes"))
    token_ids = _jsonish_list(payload.get("clobTokenIds")) or _jsonish_list(payload.get("tokenIds"))
    yes_token_id, no_token_id = _extract_yes_no_token_ids(outcomes, token_ids)
    description = (
        payload.get("description") or payload.get("rules") or payload.get("resolutionSource")
    )
    events = _jsonish_list(payload.get("events"))
    event = events[0] if events and isinstance(events[0], dict) else {}
    category = _string_or_none(payload.get("category") or event.get("category"))
    tags = tuple(_extract_tags(payload.get("tags") or event.get("tags")))
    event_slug = payload.get("eventSlug") or payload.get("event_slug") or event.get("slug")
    event_title = event.get("title")
    return Market(
        id=market_id,
        slug=payload.get("slug"),
        title=title or market_id,
        description=str(description) if description is not None else None,
        event_slug=_string_or_none(event_slug),
        event_title=_string_or_none(event_title),
        category=category,
        tags=tags,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        close_time=payload.get("endDate") or payload.get("end_date") or payload.get("close_time"),
        status=payload.get("active")
        if isinstance(payload.get("active"), str)
        else payload.get("status"),
        is_weather=classify_weather_market(
            title,
            str(description) if description else None,
            category=category,
            tags=tags,
            event_title=_string_or_none(event_title),
            event_slug=_string_or_none(event_slug),
        ),
    )


def parse_order_book_snapshot(
    market_id: str,
    payload: dict[str, Any],
    *,
    token_id: str | None = None,
) -> MarketSnapshot:
    bids = payload.get("bids") or []
    asks = payload.get("asks") or []
    best_bid = _best_price(bids, highest=True)
    best_ask = _best_price(asks, highest=False)
    midpoint = (
        (best_bid + best_ask) / Decimal("2")
        if best_bid is not None and best_ask is not None
        else None
    )
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
    bid_liquidity = _liquidity(bids)
    ask_liquidity = _liquidity(asks)
    liquidity = (
        None
        if bid_liquidity is None and ask_liquidity is None
        else (bid_liquidity or Decimal("0")) + (ask_liquidity or Decimal("0"))
    )
    resolved_token = token_id
    if resolved_token is None:
        raw_token = payload.get("token_id") or payload.get("asset_id") or payload.get("assetId")
        if raw_token is not None:
            resolved_token = str(raw_token)
    return MarketSnapshot(
        market_id=market_id,
        best_bid=best_bid,
        best_ask=best_ask,
        midpoint=midpoint,
        spread=spread,
        liquidity=liquidity,
        fetched_at=datetime.now(timezone.utc),
        token_id=resolved_token,
    )


def _jsonish_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            import json

            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return []
            return parsed if isinstance(parsed, list) else []
    return []


def _extract_yes_no_token_ids(
    outcomes: list[Any], token_ids: list[Any]
) -> tuple[str | None, str | None]:
    if len(token_ids) < 2:
        return None, None
    normalized = [str(outcome).lower() for outcome in outcomes]
    if "yes" in normalized and "no" in normalized:
        return str(token_ids[normalized.index("yes")]), str(token_ids[normalized.index("no")])
    return str(token_ids[0]), str(token_ids[1])


def _extract_tags(value: Any) -> list[str]:
    tags = _jsonish_list(value)
    result = []
    for tag in tags:
        if isinstance(tag, dict):
            raw = tag.get("label") or tag.get("name") or tag.get("slug")
        else:
            raw = tag
        if raw is not None:
            result.append(str(raw))
    return result


def _string_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _best_price(levels: list[Any], highest: bool) -> Decimal | None:
    prices = []
    for level in levels:
        if isinstance(level, dict):
            raw = level.get("price")
        elif isinstance(level, (list, tuple)) and level:
            raw = level[0]
        else:
            raw = None
        if raw is not None:
            prices.append(Decimal(str(raw)))
    if not prices:
        return None
    return max(prices) if highest else min(prices)


def _liquidity(levels: list[Any]) -> Decimal | None:
    total = Decimal("0")
    found = False
    for level in levels:
        if isinstance(level, dict):
            raw_price = level.get("price")
            raw_size = level.get("size") or level.get("quantity")
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            raw_price, raw_size = level[0], level[1]
        else:
            continue
        if raw_price is None or raw_size is None:
            continue
        total += Decimal(str(raw_price)) * Decimal(str(raw_size))
        found = True
    return total if found else None
