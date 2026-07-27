from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from polymarket_weather_arb.adapters.polymarket.base import PolymarketClient
from polymarket_weather_arb.domain.china_temperature_bucket import (
    ChinaTemperatureBucketRule,
    parse_china_temperature_bucket_rule,
)
from polymarket_weather_arb.storage.repositories import Repository


@dataclass(frozen=True)
class ChinaBucketDiscoveryOptions:
    max_ask: Decimal = Decimal("0.10")
    include_unsupported: bool = False
    event_dates: tuple[str, ...] = ()


CHINA_BUCKET_CITIES = ("qingdao", "chengdu", "shanghai", "wuhan")


class ChinaTemperatureBucketDiscoveryService:
    def __init__(self, client: PolymarketClient, repository: Repository) -> None:
        self.client = client
        self.repository = repository

    def discover(
        self,
        limit: int = 100,
        pages: int = 1,
        *,
        options: ChinaBucketDiscoveryOptions | None = None,
    ) -> int:
        options = options or ChinaBucketDiscoveryOptions()
        count = 0
        seen_market_ids: set[str] = set()
        for market, raw_payload in self._iter_event_slug_markets(options):
            if market.id in seen_market_ids:
                continue
            seen_market_ids.add(market.id)
            if self._store_candidate(market, raw_payload, options):
                count += 1
        if count:
            return count
        for page in range(pages):
            for market, raw_payload in self.client.list_markets(limit=limit, offset=page * limit):
                if market.id in seen_market_ids:
                    continue
                seen_market_ids.add(market.id)
                if self._store_candidate(market, raw_payload, options):
                    count += 1
        return count

    def _iter_event_slug_markets(self, options: ChinaBucketDiscoveryOptions):
        loader = getattr(self.client, "get_event_markets_by_slug", None)
        if loader is None:
            return
        for target_date in options.event_dates or _default_event_dates():
            for city in CHINA_BUCKET_CITIES:
                slug = f"highest-temperature-in-{city}-on-{_event_slug_date(target_date)}"
                yield from loader(slug)

    def _store_candidate(
        self, market: Any, raw_payload: dict[str, Any], options: ChinaBucketDiscoveryOptions
    ) -> bool:
        rule = parse_china_temperature_bucket_rule(market.title, _rule_text(market, raw_payload))
        if not _looks_like_china_bucket_market(market, raw_payload, rule):
            return False
        if not rule.tradable and not options.include_unsupported:
            return False
        snapshot = None
        try:
            snapshot, raw_snapshot = self.client.get_order_book(market)
        except Exception:
            raw_snapshot = {"error": "order book fetch failed"}
        if rule.tradable and snapshot is None and not options.include_unsupported:
            return False
        if (
            rule.tradable
            and snapshot
            and snapshot.best_ask is not None
            and snapshot.best_ask > options.max_ask
        ):
            return False
        self.repository.upsert_market(_with_module(market), raw_payload)
        if snapshot:
            self.repository.save_market_snapshot(
                snapshot,
                raw_snapshot,
                token_id=snapshot.token_id or market.yes_token_id or market.no_token_id,
            )
        if rule.tradable:
            self.repository.save_temperature_bucket_rule(market.id, rule)
        self.repository.upsert_candidate(
            market.id,
            _candidate_rule(rule),
            snapshot,
            status="dry_run_ready" if rule.tradable and snapshot is not None else "needs_review",
            notes=_candidate_notes(rule, snapshot, raw_snapshot),
            module_id="china_temp_bucket",
        )
        return True


def _rule_text(market: Any, raw_payload: dict[str, Any]) -> str:
    parts = [
        market.description or "",
        market.event_title or "",
        market.event_slug or "",
        market.slug or "",
        str(raw_payload.get("description") or ""),
        str(raw_payload.get("rules") or ""),
        str(raw_payload.get("slug") or ""),
        " ".join(str(item) for item in _jsonish_list(raw_payload.get("outcomes"))),
    ]
    return "\n".join(part for part in parts if part)


def _looks_like_china_bucket_market(
    market: Any, raw_payload: dict[str, Any], rule: ChinaTemperatureBucketRule
) -> bool:
    text = " ".join(
        part
        for part in [
            market.title,
            market.description or "",
            market.event_title or "",
            market.event_slug or "",
            market.slug or "",
            str(raw_payload.get("slug") or ""),
            " ".join(str(item) for item in _jsonish_list(raw_payload.get("outcomes"))),
        ]
        if part
    ).lower()
    if rule.city:
        return True
    return any(
        city in text
        for city in ("qingdao", "chengdu", "shanghai", "wuhan", "青岛", "成都", "上海", "武汉")
    )


def _with_module(market: Any) -> Any:
    return type(
        "ModuleMarket",
        (),
        {
            "id": market.id,
            "slug": market.slug,
            "title": market.title,
            "description": market.description,
            "event_slug": market.event_slug,
            "event_title": market.event_title,
            "category": market.category,
            "tags": market.tags,
            "yes_token_id": market.yes_token_id,
            "no_token_id": market.no_token_id,
            "close_time": market.close_time,
            "status": market.status,
            "is_weather": market.is_weather,
            "module_id": "china_temp_bucket",
        },
    )()


def _candidate_rule(rule: ChinaTemperatureBucketRule) -> Any:
    return type(
        "CandidateRule",
        (),
        {
            "tradable": rule.tradable,
            "rejection_reason": rule.rejection_reason,
        },
    )()


def _candidate_notes(
    rule: ChinaTemperatureBucketRule,
    snapshot: Any | None = None,
    raw_snapshot: dict[str, Any] | None = None,
) -> str | None:
    notes = []
    if rule.rejection_reason:
        notes.append(rule.rejection_reason)
    else:
        notes.extend(
            [
                "module=china_temp_bucket",
                f"city={rule.city}",
                f"station={rule.station_id}",
                f"bucket={rule.bucket_lower_c}-{rule.bucket_upper_c}C",
                f"target_date={rule.target_date}",
                f"source={rule.source}",
            ]
        )
    if snapshot is None and raw_snapshot and raw_snapshot.get("error"):
        notes.append(f"order_book={raw_snapshot['error']}")
    return "; ".join(notes) if notes else None


def _default_event_dates() -> tuple[str, ...]:
    today = datetime.now(timezone.utc).date()
    return tuple((today + timedelta(days=offset)).isoformat() for offset in range(-1, 8))


def _event_slug_date(value: str) -> str:
    parsed = datetime.fromisoformat(value).date()
    return f"{parsed:%B}-{parsed.day}-{parsed.year}".lower()


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
