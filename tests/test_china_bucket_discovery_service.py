from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.services.china_bucket_discovery_service import (
    ChinaBucketDiscoveryOptions,
    ChinaTemperatureBucketDiscoveryService,
)
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


class ChinaBucketClient:
    def __init__(self, ask: Decimal = Decimal("0.04"), *, event_markets=None):
        self.ask = ask
        self.event_markets = event_markets or []
        self.calls = []
        self.event_slug_calls = []

    def get_event_markets_by_slug(self, slug: str):
        self.event_slug_calls.append(slug)
        return (
            self.event_markets if slug == "highest-temperature-in-shanghai-on-may-10-2026" else []
        )

    def list_markets(self, limit: int = 100, offset: int = 0):
        self.calls.append((limit, offset))
        if offset:
            return []
        return [
            (
                Market(
                    id="shanghai-18c",
                    slug="highest-temperature-in-shanghai-on-may-10-2026-18c",
                    title="Highest temperature in Shanghai on May 10?",
                    description="Outcome: 18°C. Resolved according to Wunderground on 10 May '26.",
                    event_slug="highest-temperature-in-shanghai-on-may-10-2026",
                    event_title="Highest temperature in Shanghai on May 10?",
                    yes_token_id="yes-token",
                    no_token_id="no-token",
                    is_weather=True,
                ),
                {
                    "id": "shanghai-18c",
                    "slug": "highest-temperature-in-shanghai-on-may-10-2026-18c",
                    "outcomes": '["Yes", "No"]',
                    "description": "Outcome: 18°C. Resolved according to Wunderground on 10 May '26.",
                },
            ),
            (
                Market(
                    id="beijing-18c",
                    slug="highest-temperature-in-beijing-on-may-10-2026-18c",
                    title="Highest temperature in Beijing on May 10?",
                    description="Outcome: 18°C. Resolved according to Wunderground on 10 May '26.",
                    yes_token_id="yes-token-2",
                    no_token_id="no-token-2",
                    is_weather=True,
                ),
                {"id": "beijing-18c"},
            ),
        ]

    def get_order_book(self, market: Market):
        return (
            MarketSnapshot(
                market_id=market.id,
                best_bid=Decimal("0.03"),
                best_ask=self.ask,
                midpoint=(Decimal("0.03") + self.ask) / Decimal("2"),
                spread=self.ask - Decimal("0.03"),
                liquidity=Decimal("100"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {"market": market.id},
        )


class MissingBookChinaBucketClient(ChinaBucketClient):
    def get_order_book(self, market: Market):
        raise ValueError("book unavailable")


def shanghai_event_market():
    market = Market(
        id="shanghai-19c",
        slug="highest-temperature-in-shanghai-on-may-10-2026-19c",
        title="Will the highest temperature in Shanghai be 19°C on May 10?",
        description="This resolves using Wunderground for Shanghai Pudong International Airport Station on 10 May '26.",
        event_slug="highest-temperature-in-shanghai-on-may-10-2026",
        event_title="Highest temperature in Shanghai on May 10?",
        yes_token_id="yes-token",
        no_token_id="no-token",
        is_weather=True,
    )
    raw = {
        "id": market.id,
        "slug": market.slug,
        "outcomes": '["Yes", "No"]',
        "description": market.description,
    }
    return market, raw


def test_china_bucket_discovery_adds_low_ask_bucket_candidate(tmp_path):
    database = Database(tmp_path / "china-discovery.db")
    database.init_schema()
    connection = database.connect()
    client = ChinaBucketClient()
    try:
        repo = Repository(connection)
        count = ChinaTemperatureBucketDiscoveryService(client, repo).discover(limit=20, pages=1)
        connection.commit()

        assert count == 1
        assert client.calls == [(20, 0)]
        candidates = {row["market_id"]: row for row in repo.list_candidates()}
        assert candidates["shanghai-18c"]["status"] == "dry_run_ready"
        assert candidates["shanghai-18c"]["module_id"] == "china_temp_bucket"
        assert "module=china_temp_bucket" in candidates["shanghai-18c"]["notes"]
        assert "bucket=17.5-18.5C" in candidates["shanghai-18c"]["notes"]
        market = repo.get_market("shanghai-18c")
        assert market["module_id"] == "china_temp_bucket"
        bucket_rule = repo.get_temperature_bucket_rule("shanghai-18c")
        assert bucket_rule is not None
        assert bucket_rule["city"] == "Shanghai"
        assert bucket_rule["station_id"] == "ZSPD"
        assert bucket_rule["bucket_center_c"] == 18
        assert bucket_rule["bucket_lower_c"] == 17.5
        assert bucket_rule["bucket_upper_c"] == 18.5
        assert bucket_rule["target_date"] == "2026-05-10"
        assert "beijing-18c" not in candidates
    finally:
        connection.close()


def test_china_bucket_discovery_adds_event_slug_bucket_candidate(tmp_path):
    database = Database(tmp_path / "china-discovery.db")
    database.init_schema()
    connection = database.connect()
    client = ChinaBucketClient(event_markets=[shanghai_event_market()])
    try:
        repo = Repository(connection)
        count = ChinaTemperatureBucketDiscoveryService(client, repo).discover(
            limit=20,
            pages=1,
            options=ChinaBucketDiscoveryOptions(event_dates=("2026-05-10",)),
        )
        connection.commit()

        assert count == 1
        assert client.calls == []
        assert client.event_slug_calls == [
            "highest-temperature-in-qingdao-on-may-10-2026",
            "highest-temperature-in-chengdu-on-may-10-2026",
            "highest-temperature-in-shanghai-on-may-10-2026",
            "highest-temperature-in-wuhan-on-may-10-2026",
        ]
        candidates = {
            row["market_id"]: row for row in repo.list_candidates(module_id="china_temp_bucket")
        }
        assert candidates["shanghai-19c"]["status"] == "dry_run_ready"
        assert candidates["shanghai-19c"]["bucket_center_c"] == 19
        assert candidates["shanghai-19c"]["target_date"] == "2026-05-10"
    finally:
        connection.close()


def test_china_bucket_discovery_skips_missing_order_books_by_default(tmp_path):
    database = Database(tmp_path / "china-discovery.db")
    database.init_schema()
    connection = database.connect()
    client = MissingBookChinaBucketClient(event_markets=[shanghai_event_market()])
    try:
        repo = Repository(connection)
        count = ChinaTemperatureBucketDiscoveryService(client, repo).discover(
            options=ChinaBucketDiscoveryOptions(event_dates=("2026-05-10",))
        )
        connection.commit()

        assert count == 0
        assert repo.list_candidates(module_id="china_temp_bucket") == []
        assert repo.get_market("shanghai-19c") is None
    finally:
        connection.close()


def test_china_bucket_discovery_keeps_missing_books_when_including_unsupported(tmp_path):
    database = Database(tmp_path / "china-discovery.db")
    database.init_schema()
    connection = database.connect()
    client = MissingBookChinaBucketClient(event_markets=[shanghai_event_market()])
    try:
        repo = Repository(connection)
        count = ChinaTemperatureBucketDiscoveryService(client, repo).discover(
            options=ChinaBucketDiscoveryOptions(
                event_dates=("2026-05-10",), include_unsupported=True
            )
        )
        connection.commit()

        candidates = repo.list_candidates(module_id="china_temp_bucket")
        assert count == 1
        assert candidates[0]["status"] == "needs_review"
        assert "order_book=order book fetch failed" in candidates[0]["notes"]
    finally:
        connection.close()


def test_china_bucket_discovery_filters_high_ask_candidates(tmp_path):
    database = Database(tmp_path / "china-discovery.db")
    database.init_schema()
    connection = database.connect()
    client = ChinaBucketClient(ask=Decimal("0.20"))
    try:
        repo = Repository(connection)
        count = ChinaTemperatureBucketDiscoveryService(client, repo).discover(
            limit=20,
            pages=1,
            options=ChinaBucketDiscoveryOptions(max_ask=Decimal("0.10")),
        )
        connection.commit()

        assert count == 0
        assert repo.list_candidates() == []
        assert repo.get_market("shanghai-18c") is None
    finally:
        connection.close()
