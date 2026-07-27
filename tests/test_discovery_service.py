from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import polymarket_weather_arb.services.discovery_service as discovery_module
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.services.discovery_service import (
    DiscoveryService,
    dynamic_weather_event_slugs,
)
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


class PagedClient:
    def __init__(self):
        self.calls = []

    def list_markets(self, limit: int = 100, offset: int = 0):
        self.calls.append((limit, offset))
        page = offset // limit
        if page == 2:
            return [
                (
                    Market(
                        id="weather-m1",
                        slug="weather-m1",
                        title="Will the high temperature in New York exceed 80°F on May 8, 2026?",
                        description="According to NOAA station KNYC.",
                        yes_token_id="yes-token",
                        no_token_id="no-token",
                        is_weather=True,
                    ),
                    {"id": "weather-m1"},
                ),
                (
                    Market(
                        id="climate-m1",
                        slug="climate-m1",
                        title="Will 2026 be the hottest year on record?",
                        description="According to global climate datasets.",
                        yes_token_id="yes-token-2",
                        no_token_id="no-token-2",
                        is_weather=True,
                    ),
                    {"id": "climate-m1"},
                ),
            ]
        return [
            (
                Market(
                    id=f"non-weather-{page}",
                    title="Non weather market",
                    is_weather=False,
                ),
                {"id": f"non-weather-{page}"},
            )
        ]

    def get_order_book(self, market: Market):
        return (
            MarketSnapshot(
                market_id=market.id,
                best_bid=Decimal("0.40"),
                best_ask=Decimal("0.45"),
                midpoint=Decimal("0.425"),
                spread=Decimal("0.05"),
                liquidity=Decimal("100"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {"market": market.id},
        )


class PrecipClient:
    def list_markets(self, limit: int = 100, offset: int = 0):
        return [
            (
                Market(
                    id="rain-m1",
                    slug="rain-m1",
                    title="Will rainfall in New York exceed 1 inch on May 8, 2026?",
                    description="According to NOAA station KNYC.",
                    yes_token_id="yes-token",
                    no_token_id="no-token",
                    is_weather=True,
                ),
                {"id": "rain-m1"},
            )
        ]

    def get_order_book(self, market: Market):
        return (
            MarketSnapshot(
                market_id=market.id,
                best_bid=Decimal("0.40"),
                best_ask=Decimal("0.45"),
                midpoint=Decimal("0.425"),
                spread=Decimal("0.05"),
                liquidity=Decimal("100"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {"market": market.id},
        )


class GlobalBucketClient:
    def list_markets(self, limit: int = 100, offset: int = 0):
        return [
            (
                Market(
                    id="global-bucket-m1",
                    slug="miami-temp-84-85",
                    title="Will the highest temperature in Miami be between 84-85°F on July 9?",
                    description=(
                        "This market will resolve to the temperature range that contains the "
                        "highest temperature recorded at the Miami Intl Airport Station in "
                        "degrees Fahrenheit on 9 Jul '26. The resolution source for this "
                        "market will be information from Wunderground."
                    ),
                    yes_token_id="yes-token",
                    no_token_id="no-token",
                    is_weather=True,
                ),
                {"id": "global-bucket-m1"},
            )
        ]

    def get_order_book(self, market: Market):
        return (
            MarketSnapshot(
                market_id=market.id,
                best_bid=Decimal("0.40"),
                best_ask=Decimal("0.45"),
                midpoint=Decimal("0.425"),
                spread=Decimal("0.05"),
                liquidity=Decimal("100"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {"market": market.id},
        )


class WeatherEventClient:
    def get_event_markets_by_slug(self, slug: str):
        return [
            (
                Market(
                    id="weather-event-m1",
                    slug="weather-event-m1",
                    title="Will the high temperature in Miami exceed 90°F on July 7, 2026?",
                    description="According to Wunderground.",
                    yes_token_id="yes-token",
                    no_token_id="no-token",
                    is_weather=True,
                ),
                {"id": "weather-event-m1", "slug": slug},
            )
        ]

    def get_order_book(self, market: Market):
        return (
            MarketSnapshot(
                market_id=market.id,
                best_bid=Decimal("0.40"),
                best_ask=Decimal("0.45"),
                midpoint=Decimal("0.425"),
                spread=Decimal("0.05"),
                liquidity=Decimal("100"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {"market": market.id},
        )


class StormClient:
    def list_markets(self, limit: int = 100, offset: int = 0):
        return [
            (
                Market(
                    id="storm-m1",
                    slug="hurricane-alice-landfall-florida",
                    title="Will Hurricane Alice make landfall in Florida by June 10, 2026?",
                    description="Resolved according to NOAA/NHC advisories.",
                    yes_token_id="yes-token",
                    no_token_id="no-token",
                    is_weather=True,
                ),
                {"id": "storm-m1"},
            )
        ]

    def get_order_book(self, market: Market):
        return (
            MarketSnapshot(
                market_id=market.id,
                best_bid=Decimal("0.20"),
                best_ask=Decimal("0.25"),
                midpoint=Decimal("0.225"),
                spread=Decimal("0.05"),
                liquidity=Decimal("100"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {"market": market.id},
        )


def test_discovery_scans_multiple_pages_until_weather_market(tmp_path):
    database = Database(tmp_path / "discovery.db")
    database.init_schema()
    connection = database.connect()
    client = PagedClient()
    try:
        repo = Repository(connection)
        count = DiscoveryService(client, repo).discover(limit=10, pages=3)
        connection.commit()

        assert count == 1
        assert client.calls == [(10, 0), (10, 10), (10, 20)]
        assert repo.get_market("weather-m1") is not None
        candidates = {row["market_id"]: row for row in repo.list_candidates()}
        assert candidates["weather-m1"]["status"] == "dry_run_ready"
        assert "climate-m1" not in candidates
    finally:
        connection.close()


def test_discovery_routes_precipitation_to_precip_snow_module(tmp_path):
    database = Database(tmp_path / "discovery.db")
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        count = DiscoveryService(PrecipClient(), repo).discover(limit=10, pages=1)
        connection.commit()

        market = repo.get_market("rain-m1")
        candidates = {
            row["market_id"]: row for row in repo.list_candidates(module_id="precip_snow")
        }
        assert count == 1
        assert market["module_id"] == "precip_snow"
        assert candidates["rain-m1"]["module_id"] == "precip_snow"
        assert candidates["rain-m1"]["status"] == "dry_run_ready"
    finally:
        connection.close()


def test_discovery_routes_global_temperature_bucket_to_bucket_module(tmp_path):
    database = Database(tmp_path / "discovery.db")
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        count = DiscoveryService(GlobalBucketClient(), repo).discover(limit=10, pages=1)
        connection.commit()

        market = repo.get_market("global-bucket-m1")
        candidates = {
            row["market_id"]: row for row in repo.list_candidates(module_id="global_temp_bucket")
        }
        bucket_rule = repo.get_temperature_bucket_rule("global-bucket-m1")
        assert count == 1
        assert market["module_id"] == "global_temp_bucket"
        assert candidates["global-bucket-m1"]["module_id"] == "global_temp_bucket"
        assert candidates["global-bucket-m1"]["status"] == "dry_run_ready"
        assert bucket_rule is not None
        assert bucket_rule["module_id"] == "global_temp_bucket"
    finally:
        connection.close()


def test_discovery_routes_storm_markets_to_research_module_when_including_unsupported(tmp_path):
    database = Database(tmp_path / "discovery.db")
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        count = DiscoveryService(StormClient(), repo).discover(
            limit=10, pages=1, include_unsupported=True
        )
        connection.commit()

        market = repo.get_market("storm-m1")
        candidates = {
            row["market_id"]: row for row in repo.list_candidates(module_id="hurricane_storm")
        }
        assert count == 1
        assert market["module_id"] == "hurricane_storm"
        assert candidates["storm-m1"]["module_id"] == "hurricane_storm"
        assert candidates["storm-m1"]["status"] == "needs_review"
    finally:
        connection.close()


def test_discovery_can_include_unsupported_markets_for_review(tmp_path):
    database = Database(tmp_path / "discovery.db")
    database.init_schema()
    connection = database.connect()
    client = PagedClient()
    try:
        repo = Repository(connection)
        count = DiscoveryService(client, repo).discover(limit=10, pages=3, include_unsupported=True)
        connection.commit()

        assert count == 2
        candidates = {row["market_id"]: row for row in repo.list_candidates()}
        assert candidates["weather-m1"]["status"] == "dry_run_ready"
        assert candidates["climate-m1"]["status"] == "needs_review"
        assert "unclear location/station" in candidates["climate-m1"]["notes"]
    finally:
        connection.close()


def test_discover_weather_events_persists_without_caller_commit(tmp_path, monkeypatch):
    database = Database(tmp_path / "discovery.db")
    database.init_schema()
    connection = database.connect()
    client = WeatherEventClient()
    from datetime import datetime, timezone
    from polymarket_weather_arb.services.discovery_service import _MONTH_NAMES

    today = datetime.now(timezone.utc).date()
    date_slug = f"{_MONTH_NAMES[today.month - 1]}-{today.day}-{today.year}"
    monkeypatch.setattr(
        DiscoveryService,
        "_fetch_weather_event_slugs",
        lambda self, now=None: ([f"highest-temperature-in-miami-on-{date_slug}"], []),
    )
    try:
        repo = Repository(connection)
        count = DiscoveryService(client, repo).discover_weather_events(limit=1)
        assert count == 1
    finally:
        connection.close()

    verify = database.connect()
    try:
        row = verify.execute("SELECT id FROM markets WHERE id = 'weather-event-m1'").fetchone()
    finally:
        verify.close()
    assert row is not None


def test_dynamic_weather_event_slugs_include_today_cities_first():
    slugs = dynamic_weather_event_slugs()
    assert len(slugs) == 33
    assert slugs[0].startswith("highest-temperature-in-shanghai-on-")
    assert any("highest-temperature-in-nyc-on-" in slug for slug in slugs)


def test_scraped_stale_weather_events_are_filtered_and_dates_interleaved(monkeypatch):
    from datetime import datetime, timezone, timedelta
    from polymarket_weather_arb.services.discovery_service import _MONTH_NAMES

    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    today = now.date()
    d0_slug = f"{_MONTH_NAMES[today.month - 1]}-{today.day}-{today.year}"
    d1 = today + timedelta(days=1)
    d1_slug = f"{_MONTH_NAMES[d1.month - 1]}-{d1.day}-{d1.year}"
    d_minus_1 = today - timedelta(days=1)
    d_minus_1_slug = f"{_MONTH_NAMES[d_minus_1.month - 1]}-{d_minus_1.day}-{d_minus_1.year}"

    stale = [
        f"highest-temperature-in-{city}-on-{d_minus_1_slug}"
        for city in discovery_module.WEATHER_EVENT_CITIES[:2]
    ]
    current_d0 = [
        f"highest-temperature-in-{city}-on-{d0_slug}"
        for city in discovery_module.WEATHER_EVENT_CITIES[:2]
    ]
    current_d1 = [
        f"highest-temperature-in-{city}-on-{d1_slug}"
        for city in discovery_module.WEATHER_EVENT_CITIES[:2]
    ]

    class Response:
        text = "".join(f'{{"slug":"{slug}"}}' for slug in (stale + current_d1))

        def raise_for_status(self):
            return None

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url):
            return Response()

    monkeypatch.setattr(discovery_module, "build_httpx_client", lambda **_kwargs: Client())
    monkeypatch.setattr(
        discovery_module, "dynamic_weather_event_slugs", lambda **kwargs: current_d0
    )

    scraped, generated = DiscoveryService(object(), object())._fetch_weather_event_slugs(now=now)
    all_slugs = list(scraped) + [g for g in generated if g not in set(scraped)]
    # Should only contain D0 and D1, stale D-1 is filtered out by select_fair_slugs.
    filtered_slugs = discovery_module.select_fair_slugs(all_slugs, rotation_slot=0, now=now)

    assert set(filtered_slugs) == set(current_d0 + current_d1)

    # Check interleaving: D1 should be first, then D0
    for i in range(len(current_d0)):
        assert d1_slug in filtered_slugs[i * 2]
        assert d0_slug in filtered_slugs[i * 2 + 1]


def test_discovery_select_fair_slugs_local_date_nyc_midnight():
    # NYC UTC midnight. In UTC it is July 14, 00:00. In NY it is July 13, 20:00.
    from datetime import datetime, timezone
    from polymarket_weather_arb.services.discovery_service import select_fair_slugs

    now = datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc)
    # So local day for NY is July 13.
    # D0 = July 13, D1 = July 14, D2 = July 15. D-1 = July 12.
    slugs = [
        "highest-temperature-in-nyc-on-july-12-2026",
        "highest-temperature-in-nyc-on-july-13-2026",
        "highest-temperature-in-nyc-on-july-14-2026",
        "highest-temperature-in-nyc-on-july-15-2026",
        "highest-temperature-in-nyc-on-july-16-2026",
    ]

    filtered = select_fair_slugs(slugs, rotation_slot=0, now=now)
    assert "highest-temperature-in-nyc-on-july-12-2026" not in filtered
    assert "highest-temperature-in-nyc-on-july-13-2026" in filtered  # D0
    assert "highest-temperature-in-nyc-on-july-14-2026" in filtered  # D1
    assert "highest-temperature-in-nyc-on-july-15-2026" in filtered  # D2
    assert "highest-temperature-in-nyc-on-july-16-2026" not in filtered


def test_discovery_select_fair_slugs_local_date_tokyo_night():
    # Tokyo UTC late night. In UTC it is July 13, 20:00. In Tokyo (UTC+9) it is July 14, 05:00.
    from datetime import datetime, timezone
    from polymarket_weather_arb.services.discovery_service import select_fair_slugs

    now = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)
    # Local day for Tokyo is July 14.
    # D0 = July 14, D1 = July 15, D2 = July 16. D-1 = July 13.
    slugs = [
        "highest-temperature-in-tokyo-on-july-13-2026",
        "highest-temperature-in-tokyo-on-july-14-2026",
        "highest-temperature-in-tokyo-on-july-15-2026",
        "highest-temperature-in-tokyo-on-july-16-2026",
        "highest-temperature-in-tokyo-on-july-17-2026",
    ]

    filtered = select_fair_slugs(slugs, rotation_slot=0, now=now)
    assert "highest-temperature-in-tokyo-on-july-13-2026" not in filtered
    assert "highest-temperature-in-tokyo-on-july-14-2026" in filtered  # D0
    assert "highest-temperature-in-tokyo-on-july-15-2026" in filtered  # D1
    assert "highest-temperature-in-tokyo-on-july-16-2026" in filtered  # D2
    assert "highest-temperature-in-tokyo-on-july-17-2026" not in filtered


def test_discovery_rotation_covers_all():
    from datetime import datetime, timezone
    from polymarket_weather_arb.services.discovery_service import (
        select_fair_slugs,
        WEATHER_EVENT_CITIES,
    )

    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    slugs = []
    # 3 dates for each of the cities
    dates = ["july-14-2026", "july-15-2026", "july-16-2026"]
    for city in WEATHER_EVENT_CITIES:
        for d in dates:
            slugs.append(f"highest-temperature-in-{city}-on-{d}")

    # We have N cities. Let's trace the first element over N slots
    first_elements = set()
    for slot in range(len(WEATHER_EVENT_CITIES)):
        filtered = select_fair_slugs(slugs, rotation_slot=slot, now=now)
        first_elements.add(filtered[0])

    # Since we have N cities, and each slot shifts the cities list by 1,
    # the first element should be different across N slots for D1 (since D1 comes first).
    assert len(first_elements) == len(WEATHER_EVENT_CITIES)


def test_discover_weather_events_deferred_coverage(tmp_path, monkeypatch):
    from polymarket_weather_arb.storage.db import Database
    from polymarket_weather_arb.storage.repositories import Repository
    from polymarket_weather_arb.services.discovery_service import DiscoveryService
    import logging

    database = Database(tmp_path / "discovery.db")
    database.init_schema()
    connection = database.connect()

    # 10 scraped, 10 generated with distinct city names so deduplication doesn't merge them
    scraped = [f"highest-temperature-in-city{i}-on-july-14-2026" for i in range(10)]
    generated = [f"highest-temperature-in-city{i}-on-july-14-2026" for i in range(10, 20)]

    # We bypass actual client
    class FakeClient:
        def get_event_markets_by_slug(self, slug):
            return []

    monkeypatch.setattr(
        DiscoveryService, "_fetch_weather_event_slugs", lambda self, now=None: (scraped, generated)
    )

    # Also mock try_local_weather_day to return a fixed day so these fake cities work
    from polymarket_weather_arb.domain import market_eligibility
    from datetime import datetime, timezone

    target_date = datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc).date()
    monkeypatch.setattr(
        market_eligibility, "try_local_weather_day", lambda location_hint, now: target_date
    )

    # Limit to 5.
    svc = DiscoveryService(FakeClient(), Repository(connection))

    # Capture logs
    log_messages = []

    class ListHandler(logging.Handler):
        def emit(self, record):
            log_messages.append(record.getMessage())

    logger = logging.getLogger("polymarket_weather_arb.services.discovery_service")
    logger.setLevel(logging.INFO)
    handler = ListHandler()
    logger.addHandler(handler)

    try:
        now_ts = datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc)
        svc.discover_weather_events(
            limit=5, time_budget=0.0, now=now_ts
        )  # budget 0 means all 5 selected are deferred due to time budget too!
    finally:
        logger.removeHandler(handler)
        connection.close()

    assert any(
        "scraped=10 generated=10 selected=20 reads=0 failures=0 deferred=20" in msg
        for msg in log_messages
    )


def test_extract_weather_event_slugs_does_not_require_json_slug_field():
    html = """
    <a href="/event/highest-temperature-in-cape-town-on-july-20-2026">
      highest-temperature-in-cape-town-on-july-20-2026-21c
    </a>
    <script>\"eventSlug\":\"highest-temperature-in-lagos-on-july-21-2026\"</script>
    <div>highest-temperature-in-cape-town-on-july-20-2026</div>
    """

    assert discovery_module.extract_weather_event_slugs(html) == [
        "highest-temperature-in-cape-town-on-july-20-2026",
        "highest-temperature-in-lagos-on-july-21-2026",
    ]


def test_dynamic_global_city_is_qualified_and_persisted(tmp_path, monkeypatch):
    database = Database(tmp_path / "global-city.db")
    database.init_schema()
    connection = database.connect()
    event_slug = "highest-temperature-in-cape-town-on-july-20-2026"

    class Client:
        def get_event_markets_by_slug(self, slug):
            assert slug == event_slug
            return [
                (
                    Market(
                        id="cape-town-21c",
                        slug="cape-town-21c",
                        event_slug=event_slug,
                        title=(
                            "Will the highest temperature in Cape Town be 21C on July 20, 2026?"
                        ),
                        description=(
                            "Settlement source: Wunderground. "
                            "https://www.wunderground.com/history/daily/za/cape-town/FACT"
                        ),
                        yes_token_id="cape-yes",
                        no_token_id="cape-no",
                        is_weather=True,
                    ),
                    {"bestBid": "0.10", "bestAsk": "0.12"},
                )
            ]

    monkeypatch.setattr(
        DiscoveryService,
        "_fetch_weather_event_slugs",
        lambda self, now=None: ([event_slug], []),
    )
    monkeypatch.setattr(
        discovery_module,
        "geocode_location",
        lambda location: (-33.92, 18.42, "Africa/Johannesburg", {"city": location}),
    )
    monkeypatch.setattr(
        discovery_module,
        "fetch_awc_station_location",
        lambda station: (-33.97, 18.60, {"icaoId": station}),
    )
    try:
        repository = Repository(connection)
        service = DiscoveryService(Client(), repository)
        service._city_timezones["cape-town"] = "America/Los_Angeles"
        count = service.discover_weather_events(
            limit=1,
            now=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
        )
        rule = repository.get_temperature_bucket_rule("cape-town-21c")
        candidate = repository.list_candidates(limit=1, module_id="global_temp_bucket")[0]

        assert count == 1
        assert rule is not None
        assert rule["station_id"] == "FACT"
        assert rule["settlement_timezone"] == "Africa/Johannesburg"
        assert bool(rule["tradable"]) is True
        assert candidate["status"] == "dry_run_ready"
        assert candidate["settlement_timezone"] == "Africa/Johannesburg"
    finally:
        connection.close()


def test_dynamic_city_geocode_failure_remains_review_only(tmp_path, monkeypatch):
    database = Database(tmp_path / "global-city-review.db")
    database.init_schema()
    connection = database.connect()
    event_slug = "highest-temperature-in-cape-town-on-july-20-2026"

    class Client:
        def get_event_markets_by_slug(self, _slug):
            return [
                (
                    Market(
                        id="cape-review",
                        event_slug=event_slug,
                        title=(
                            "Will the highest temperature in Cape Town be 21C on July 20, 2026?"
                        ),
                        description=(
                            "Settlement source: Wunderground. "
                            "https://www.wunderground.com/history/daily/za/cape-town/FACT"
                        ),
                        yes_token_id="yes",
                        no_token_id="no",
                        is_weather=True,
                    ),
                    {"bestBid": "0.10", "bestAsk": "0.12"},
                )
            ]

    monkeypatch.setattr(
        DiscoveryService,
        "_fetch_weather_event_slugs",
        lambda self, now=None: ([event_slug], []),
    )
    monkeypatch.setattr(
        discovery_module,
        "geocode_location",
        lambda _location: (_ for _ in ()).throw(ValueError("no verified timezone")),
    )
    try:
        repository = Repository(connection)
        count = DiscoveryService(Client(), repository).discover_weather_events(
            limit=1,
            include_unsupported=True,
            now=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
        )
        rule = repository.get_temperature_bucket_rule("cape-review")
        candidate = repository.list_candidates(limit=1, module_id="global_temp_bucket")[0]

        assert count == 1
        assert rule is not None
        assert bool(rule["tradable"]) is False
        assert rule["settlement_timezone"] == ""
        assert candidate["status"] == "needs_review"
    finally:
        connection.close()


def test_active_open_meteo_cooldown_skips_repeated_city_geocode(tmp_path, monkeypatch):
    database = Database(tmp_path / "global-city-cooldown.db")
    database.init_schema()
    connection = database.connect()
    market = Market(
        id="cape-cooldown",
        title="Will the highest temperature in Cape Town be 21C on July 20, 2026?",
        description=(
            "Settlement source: Wunderground. "
            "https://www.wunderground.com/history/daily/za/cape-town/FACT"
        ),
        yes_token_id="yes",
        no_token_id="no",
        is_weather=True,
    )
    geocode_calls = 0

    def fail_if_called(_location):
        nonlocal geocode_calls
        geocode_calls += 1
        raise AssertionError("geocode must not run during provider cooldown")

    monkeypatch.setattr(discovery_module, "open_meteo_cooldown_remaining", lambda: 600)
    monkeypatch.setattr(discovery_module, "geocode_location", fail_if_called)
    try:
        service = DiscoveryService(object(), Repository(connection))
        first_rule, first_module = service._rule_and_module_for_market(market)
        second_rule, second_module = service._rule_and_module_for_market(market)

        assert first_module == second_module == "global_temp_bucket"
        assert first_rule.tradable is False
        assert second_rule.tradable is False
        assert geocode_calls == 0
    finally:
        connection.close()


def test_persisted_global_city_expands_three_day_fallback(tmp_path, monkeypatch):
    database = Database(tmp_path / "global-catalog.db")
    database.init_schema()
    connection = database.connect()
    event_slug = "highest-temperature-in-cape-town-on-july-20-2026"
    title = "Will the highest temperature in Cape Town be 21C on July 20, 2026?"
    description = (
        "Settlement source: Wunderground. "
        "https://www.wunderground.com/history/daily/za/cape-town/FACT"
    )
    try:
        repository = Repository(connection)
        repository.upsert_market(
            Market(
                id="cape-catalog",
                event_slug=event_slug,
                title=title,
                description=description,
                is_weather=True,
            ),
            {},
            module_id="global_temp_bucket",
        )
        rule = discovery_module.with_settlement_timezone(
            discovery_module.parse_global_temperature_bucket_rule(title, description),
            "Africa/Johannesburg",
        )
        repository.save_temperature_bucket_rule(
            "cape-catalog", rule, module_id="global_temp_bucket"
        )
        connection.commit()

        class BrokenHttpClient:
            def __enter__(self):
                raise OSError("weather page unavailable")

            def __exit__(self, *_args):
                return None

        monkeypatch.setattr(
            discovery_module,
            "build_httpx_client",
            lambda **_kwargs: BrokenHttpClient(),
        )
        scraped, generated = DiscoveryService(object(), repository)._fetch_weather_event_slugs(
            now=datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
        )

        assert scraped == []
        assert {
            "highest-temperature-in-cape-town-on-july-20-2026",
            "highest-temperature-in-cape-town-on-july-21-2026",
            "highest-temperature-in-cape-town-on-july-22-2026",
        }.issubset(set(generated))
    finally:
        connection.close()
