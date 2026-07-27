from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

from polymarket_weather_arb.adapters.http_client import build_httpx_client
from polymarket_weather_arb.adapters.http_reader import open_meteo_cooldown_remaining
from polymarket_weather_arb.adapters.polymarket.base import PolymarketClient
from polymarket_weather_arb.adapters.weather.awc_metar import fetch_awc_station_location
from polymarket_weather_arb.adapters.weather.open_meteo import geocode_location
from polymarket_weather_arb.domain.global_temperature_bucket import (
    parse_global_temperature_bucket_rule,
    with_settlement_timezone,
)
from polymarket_weather_arb.domain.hurricane_storm import classify_hurricane_storm_market
from polymarket_weather_arb.domain.market_eligibility import evaluate_market_orderability
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.domain.rules import parse_resolution_rule
from polymarket_weather_arb.storage.repositories import Repository

# Pattern to match weather event slugs like "highest-temperature-in-shanghai-on-may-29-2026"
WEATHER_EVENT_SLUG_PATTERN = re.compile(
    r"highest-temperature-in-([a-z0-9-]+)-on-([a-z]+-\d{1,2}-\d{4})$"
)
# This list bootstraps an empty database. It is not an allowlist: the live
# Polymarket weather page and persisted market/rule rows expand the catalog.
WEATHER_EVENT_CITIES = (
    "shanghai",
    "wuhan",
    "chengdu",
    "qingdao",
    "nyc",
    "chicago",
    "miami",
    "london",
    "seoul",
    "dallas",
    "atlanta",
)
_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)

# Strict upper bound on per-tick discovery CLOB book fallbacks when Gamma quotes
# are missing/invalid. Must not scale with total discovered buckets.
DISCOVERY_CLOB_FALLBACK_LIMIT = 5

logger = logging.getLogger(__name__)


def extract_weather_event_slugs(html: str) -> list[str]:
    """Extract event slugs without depending on one Next.js JSON field encoding."""
    pattern = re.compile(
        r"highest-temperature-in-[a-z0-9-]+-on-[a-z]+-\d{1,2}-\d{4}(?!\d)",
        re.I,
    )
    return list(dict.fromkeys(match.group(0).lower() for match in pattern.finditer(html)))


def select_fair_slugs(
    slugs: Sequence[str],
    rotation_slot: int,
    now: datetime | None = None,
    city_timezones: dict[str, str] | None = None,
) -> list[str]:
    """Rotate the deduped slugs deterministically based on rotation_slot."""
    if not slugs:
        return []

    from polymarket_weather_arb.domain.market_eligibility import try_local_weather_day

    current = now or datetime.now(timezone.utc)
    by_days_diff: dict[Any, list[str]] = {0: [], 1: [], 2: [], "unknown": []}

    for slug in set(slugs):
        m = WEATHER_EVENT_SLUG_PATTERN.match(slug)
        if not m:
            continue
        city = m.group(1)
        date_str = m.group(2)
        parts = date_str.split("-")
        if len(parts) == 3:
            try:
                month_idx = _MONTH_NAMES.index(parts[0]) + 1
                target_date = datetime(int(parts[2]), month_idx, int(parts[1])).date()
            except ValueError:
                continue

            timezone_name = (city_timezones or {}).get(city)
            day_kwargs = {
                "location_hint": city.replace("-", " "),
                "now": current,
            }
            if timezone_name:
                day_kwargs["timezone_name"] = timezone_name
            local_day = try_local_weather_day(**day_kwargs)
            if local_day is None:
                # A timezone can move the calendar date by at most one day from
                # UTC. Keep plausible active events for qualification, but drop
                # obviously stale/far-future page artifacts.
                utc_diff = (target_date - current.date()).days
                if -1 <= utc_diff <= 3:
                    by_days_diff["unknown"].append(slug)
                continue

            days_diff = (target_date - local_day).days
            if 0 <= days_diff <= 2:
                by_days_diff[days_diff].append(slug)

    rotated_by_diff: dict[Any, list[str]] = {}
    for diff, diff_slugs in by_days_diff.items():
        sorted_slugs = sorted(diff_slugs)
        if sorted_slugs:
            shift = rotation_slot % len(sorted_slugs)
            rotated_by_diff[diff] = sorted_slugs[shift:] + sorted_slugs[:shift]
        else:
            rotated_by_diff[diff] = []

    order = [1, 0, 2, "unknown"]
    lists = [rotated_by_diff[d] for d in order]

    result = []
    max_len = max((len(lst) for lst in lists), default=0)
    for i in range(max_len):
        for lst in lists:
            if i < len(lst):
                result.append(lst[i])

    return result


class DiscoveryService:
    def __init__(self, client: PolymarketClient, repository: Repository) -> None:
        self.client = client
        self.repository = repository
        self._clob_fallback_calls = 0
        self.clob_book_calls = 0
        self._city_timezones: dict[str, str] = {}

    def discover(
        self,
        limit: int = 100,
        pages: int = 1,
        *,
        include_unsupported: bool = False,
        reset_fallback_budget: bool = True,
    ) -> int:
        if reset_fallback_budget:
            self.reset_fallback_budget()
        count = 0
        for page in range(pages):
            for market, raw_payload in self.client.list_markets(limit=limit, offset=page * limit):
                if not market.is_weather:
                    continue
                rule, module_id = self._rule_and_module_for_market(market)
                if not rule.tradable and not include_unsupported:
                    continue
                if self._persist_discovered_market(
                    market,
                    raw_payload,
                    rule,
                    module_id=module_id,
                ):
                    count += 1
            self.repository.connection.commit()
        return count

    def discover_weather_events(
        self,
        *,
        include_unsupported: bool = False,
        limit: int = 30,
        time_budget: float = float("inf"),
        rotation_slot: int = 0,
        reset_fallback_budget: bool = True,
        now: datetime | None = None,
    ) -> int:
        """Discover weather markets from Polymarket's weather event page."""
        if reset_fallback_budget:
            self.reset_fallback_budget()

        scraped_slugs, generated_slugs = self._fetch_weather_event_slugs(now=now)

        # Scraped priority: append generated only if not in scraped
        scraped_set = set(scraped_slugs)
        all_slugs = list(scraped_slugs) + [g for g in generated_slugs if g not in scraped_set]

        # Apply fair rotation and D0-D2 local date filtering
        selected_slugs = select_fair_slugs(
            all_slugs,
            rotation_slot=rotation_slot,
            now=now,
            city_timezones=self._city_timezones,
        )

        selected_count = len(selected_slugs)

        deferred = 0
        if limit > 0 and len(selected_slugs) > limit:
            deferred += len(selected_slugs) - limit
            selected_slugs = selected_slugs[:limit]

        count = 0
        reads = 0
        failures = 0
        start_time = time.monotonic()
        for slug in selected_slugs:
            if time_budget <= 0 or (time.monotonic() - start_time) >= time_budget:
                deferred += 1
                continue
            try:
                reads += 1
                markets = self.client.get_event_markets_by_slug(slug)
            except Exception:
                failures += 1
                continue
            for market, raw_payload in markets:
                if not market.is_weather:
                    continue
                rule, module_id = self._rule_and_module_for_market(market)
                if not rule.tradable and not include_unsupported:
                    continue
                if self._persist_discovered_market(
                    market,
                    raw_payload,
                    rule,
                    module_id=module_id,
                ):
                    count += 1
            self.repository.connection.commit()

        logger.info(
            f"Discovery coverage: scraped={len(scraped_slugs)} generated={len(generated_slugs)} "
            f"selected={selected_count} reads={reads} failures={failures} deferred={deferred}"
        )
        return count

    def reset_fallback_budget(self) -> None:
        """Reset the CLOB fallback counter (once per Autopilot tick / CLI call)."""
        self._clob_fallback_calls = 0
        self.clob_book_calls = 0

    def _persist_discovered_market(
        self,
        market: Market,
        raw_payload: object,
        rule,
        *,
        module_id: str,
    ) -> bool:
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        snapshot, raw_snapshot = self._resolve_discovery_snapshot(market, payload)
        self.repository.upsert_market(_market_with_module(market, module_id), raw_payload)
        if module_id == "global_temp_bucket":
            self.repository.save_temperature_bucket_rule(
                market.id, rule, module_id="global_temp_bucket"
            )
        else:
            self.repository.save_resolution_rule(market.id, rule)
        if snapshot is not None:
            self.repository.save_market_snapshot(
                snapshot,
                raw_snapshot,
                token_id=snapshot.token_id or market.yes_token_id or market.no_token_id,
            )
        # Discovery lifecycle: closed / not accepting / past end time only.
        # Past target date is enforced at selection time so research history can
        # still land with a truthful status without false "expired" from title.
        eligibility = evaluate_market_orderability(
            raw_payload=payload,
            title=market.title,
            close_time=market.close_time,
            check_target_date=False,
        )
        if not eligibility.orderable:
            candidate_status = eligibility.terminal_status or "expired"
            notes = eligibility.reason or rule.rejection_reason
        elif rule.tradable:
            candidate_status = "dry_run_ready"
            notes = rule.rejection_reason
        else:
            candidate_status = "needs_review"
            notes = rule.rejection_reason
        self.repository.upsert_candidate(
            market.id,
            rule,
            snapshot,
            status=candidate_status,
            notes=notes,
            module_id=module_id,
        )
        self.repository.connection.commit()
        return True

    def _resolve_discovery_snapshot(
        self, market: Market, payload: dict[str, Any]
    ) -> tuple[MarketSnapshot | None, dict[str, Any]]:
        """Prefer Gamma summary quotes; fall back to a strictly bounded CLOB book fetch."""
        summary = snapshot_from_gamma_summary(market.id, payload)
        if summary is not None:
            snapshot, raw = summary
            return snapshot, raw

        # Missing/invalid Gamma quotes: do not fan out to all markets.
        if self._clob_fallback_calls >= DISCOVERY_CLOB_FALLBACK_LIMIT:
            return None, {
                "error": "order book fallback budget exhausted",
                "source": "discovery-fallback-skipped",
            }

        eligibility = evaluate_market_orderability(
            raw_payload=payload,
            title=market.title,
            close_time=market.close_time,
            check_target_date=True,
        )
        # Prefer currently/future-eligible orderable markets for the limited budget.
        if not eligibility.orderable:
            return None, {
                "error": "gamma summary missing; ineligible for fallback budget",
                "source": "discovery-fallback-skipped",
                "eligibility": eligibility.reason,
            }

        try:
            self._clob_fallback_calls += 1
            self.clob_book_calls += 1
            snapshot, raw_snapshot = self.client.get_order_book(market)
        except Exception as exc:
            return None, {
                "error": f"order book fetch failed: {exc}",
                "source": "clob-fallback",
            }
        raw = raw_snapshot if isinstance(raw_snapshot, dict) else {"raw": raw_snapshot}
        return snapshot, {**raw, "source": "clob-fallback"}

    def _fetch_weather_event_slugs(
        self, now: datetime | None = None
    ) -> tuple[list[str], list[str]]:
        """Fetch weather event slugs from Polymarket and generated daily city slugs."""
        scraped: list[str] = []
        try:
            settings = getattr(self.client, "settings", None)
            with build_httpx_client(timeout=30, settings=settings) as client:
                response = client.get("https://polymarket.com/weather")
                response.raise_for_status()
            html = response.text
            scraped = extract_weather_event_slugs(html)
        except Exception:
            scraped = []

        persisted_cities, persisted_timezones = self._known_weather_catalog()
        scraped_cities = [
            match.group(1)
            for slug in scraped
            if (match := WEATHER_EVENT_SLUG_PATTERN.match(slug)) is not None
        ]
        cities = list(dict.fromkeys((*WEATHER_EVENT_CITIES, *persisted_cities, *scraped_cities)))
        self._city_timezones = persisted_timezones
        generated = dynamic_weather_event_slugs(
            now=now,
            cities=cities,
            city_timezones=persisted_timezones,
        )
        return list(scraped), list(generated)

    def _known_weather_catalog(self) -> tuple[list[str], dict[str, str]]:
        reader = getattr(self.repository, "list_global_temperature_catalog", None)
        if not callable(reader):
            return [], {}
        try:
            rows = reader()
        except Exception:
            return [], {}
        cities: list[str] = []
        timezones: dict[str, str] = {}
        for row in rows:
            event_slug = str(_row_value(row, "event_slug") or "")
            match = WEATHER_EVENT_SLUG_PATTERN.match(event_slug)
            city_slug = match.group(1) if match else _city_slug(_row_value(row, "city"))
            if not city_slug:
                continue
            cities.append(city_slug)
            timezone_name = str(_row_value(row, "settlement_timezone") or "").strip()
            if timezone_name:
                timezones[city_slug] = timezone_name
        return list(dict.fromkeys(cities)), timezones

    def _rule_and_module_for_market(self, market: Market) -> tuple[Any, str]:
        rule, module_id = _rule_and_module_for_market(market)
        if module_id != "global_temp_bucket" or rule.settlement_timezone:
            return rule, module_id
        if "unclear settlement timezone" not in str(rule.rejection_reason or ""):
            return rule, module_id
        location = str(rule.location or "").strip()
        if not location:
            return rule, module_id
        # A single Open-Meteo free-tier quota applies to geocoding and forecast
        # subdomains. During an active cooldown, do not repeat the same failed
        # geocode once for every sibling temperature bucket.
        if open_meteo_cooldown_remaining():
            return rule, module_id
        city_slug = _city_slug(location)
        try:
            _latitude, _longitude, timezone_name, _payload = geocode_location(location)
        except Exception as exc:
            logger.info("weather city qualification deferred city=%s error=%s", location, exc)
            return rule, module_id
        station = str(rule.station or "").strip().upper()
        if rule.source == "Wunderground" and not station:
            logger.info(
                "weather city qualification deferred city=%s error=station missing", location
            )
            return rule, module_id
        if station:
            try:
                fetch_awc_station_location(station)
            except Exception as exc:
                logger.info(
                    "weather station qualification deferred city=%s station=%s error=%s",
                    location,
                    station,
                    exc,
                )
                return rule, module_id
        qualified = with_settlement_timezone(rule, timezone_name)
        if qualified.settlement_timezone:
            self._city_timezones[city_slug] = qualified.settlement_timezone
        return qualified, module_id


def snapshot_from_gamma_summary(
    market_id: str, payload: dict[str, Any]
) -> tuple[MarketSnapshot, dict[str, Any]] | None:
    """Build a preliminary MarketSnapshot from Gamma summary quote fields.

    These quotes are never the final executable live quote; entry still refreshes
    the token-specific CLOB book immediately before submit.
    """
    best_bid = _decimal_field(payload, "bestBid", "best_bid")
    best_ask = _decimal_field(payload, "bestAsk", "best_ask")
    if best_bid is None and best_ask is None:
        # Some Gamma payloads only expose lastTradePrice / outcomePrices.
        last_trade = _decimal_field(payload, "lastTradePrice", "last_trade_price")
        if last_trade is None:
            return None
        best_bid = last_trade
        best_ask = last_trade

    if best_bid is not None and (best_bid < 0 or best_bid > 1):
        return None
    if best_ask is not None and (best_ask < 0 or best_ask > 1):
        return None
    if best_bid is not None and best_ask is not None and best_bid > best_ask:
        return None

    midpoint = (
        (best_bid + best_ask) / Decimal("2")
        if best_bid is not None and best_ask is not None
        else best_bid or best_ask
    )
    spread = _decimal_field(payload, "spread")
    if spread is None and best_bid is not None and best_ask is not None:
        spread = best_ask - best_bid
    liquidity = _decimal_field(payload, "liquidity", "liquidityNum", "liquidityClob")

    snapshot = MarketSnapshot(
        market_id=market_id,
        best_bid=best_bid,
        best_ask=best_ask,
        midpoint=midpoint,
        spread=spread,
        liquidity=liquidity,
        fetched_at=datetime.now(timezone.utc),
    )
    raw = {
        "source": "gamma-summary",
        "bestBid": str(best_bid) if best_bid is not None else None,
        "bestAsk": str(best_ask) if best_ask is not None else None,
        "spread": str(spread) if spread is not None else None,
        "liquidity": str(liquidity) if liquidity is not None else None,
        "lastTradePrice": payload.get("lastTradePrice") or payload.get("last_trade_price"),
    }
    return snapshot, raw


def _decimal_field(payload: dict[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        if key not in payload or payload[key] is None or payload[key] == "":
            continue
        try:
            value = Decimal(str(payload[key]))
        except (InvalidOperation, ValueError):
            continue
        return value
    return None


def dynamic_weather_event_slugs(
    *,
    now: datetime | None = None,
    cities: Sequence[str] | None = None,
    city_timezones: dict[str, str] | None = None,
) -> list[str]:
    from polymarket_weather_arb.domain.market_eligibility import try_local_weather_day

    current = now or datetime.now(timezone.utc)
    slugs: list[str] = []

    city_slugs = list(dict.fromkeys(_city_slug(city) for city in (cities or WEATHER_EVENT_CITIES)))
    for city in city_slugs:
        if not city:
            continue
        timezone_name = (city_timezones or {}).get(city)
        day_kwargs = {"location_hint": city.replace("-", " "), "now": current}
        if timezone_name:
            day_kwargs["timezone_name"] = timezone_name
        local_day = try_local_weather_day(**day_kwargs)
        if local_day is None:
            continue
        for offset in (0, 1, 2):
            target = local_day + timedelta(days=offset)
            date_slug = f"{_MONTH_NAMES[target.month - 1]}-{target.day}-{target.year}"
            slugs.append(f"highest-temperature-in-{city}-on-{date_slug}")

    return slugs


def _merge_weather_event_slugs(*sources: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for slugs in sources:
        for slug in slugs:
            if slug in seen or not WEATHER_EVENT_SLUG_PATTERN.match(slug):
                continue
            seen.add(slug)
            merged.append(slug)
    return merged


def _rule_and_module_for_market(market: Market) -> tuple[Any, str]:
    global_bucket = parse_global_temperature_bucket_rule(market.title, market.description)
    if global_bucket.tradable or _looks_like_global_temperature_bucket(global_bucket):
        return global_bucket, "global_temp_bucket"

    rule = parse_resolution_rule(market.title, market.description)
    return rule, _module_id_for_market(market, rule)


def _module_id_for_market(market: Market, rule: Any) -> str:
    if rule.variable in {"precipitation", "snowfall"}:
        return "precip_snow"
    storm = classify_hurricane_storm_market(market.title, market.description)
    if storm.research_only:
        return "hurricane_storm"
    return "weather"


def _looks_like_global_temperature_bucket(rule: Any) -> bool:
    return bool(
        rule.variable == "temperature_high"
        and rule.bucket_center is not None
        and rule.target_date
        and (rule.location or rule.station)
    )


def _city_slug(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower())
    return normalized.strip("-")


def _row_value(row: object, key: str) -> object | None:
    try:
        return row[key]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return None


def _market_with_module(market: Market, module_id: str):
    return type("ModuleMarket", (), {**market.__dict__, "module_id": module_id})()
