from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.china_temperature_bucket import (
    parse_china_temperature_bucket_rule,
)
from polymarket_weather_arb.domain.markets import MarketSnapshot
from polymarket_weather_arb.services.cockpit_service import build_cockpit_snapshot
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def test_cockpit_suggests_discovery_when_no_candidates(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "cockpit.db")
    Database(settings.database_path).init_schema()

    snapshot = _snapshot(settings)

    assert snapshot.next_action.label == "Run discovery"
    assert snapshot.next_action.href == "/discovery"
    assert snapshot.pipeline.found == 0
    assert snapshot.top_candidates == []
    assert snapshot.blockers == []


def test_cockpit_prioritizes_ready_candidates_and_missing_signal_blocker(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "cockpit.db")
    _seed_china_candidate(settings, with_snapshot=True)

    snapshot = _snapshot(settings)

    assert snapshot.next_action.label == "Refresh missing signals"
    assert snapshot.next_action.href == "/markets/shanghai-18c"
    assert snapshot.pipeline.found == 1
    assert snapshot.pipeline.parsed == 1
    assert snapshot.pipeline.quoted == 1
    assert snapshot.pipeline.signal_ready == 0
    assert snapshot.pipeline.analyzed == 0
    assert snapshot.pipeline.dry_run == 0
    assert snapshot.top_candidates[0].market_id == "shanghai-18c"
    assert snapshot.top_candidates[0].module_id == "china_temp_bucket"
    assert snapshot.top_candidates[0].next_step == "refresh_signal"
    assert any("signal" in blocker.message.lower() for blocker in snapshot.blockers)


def _snapshot(settings: Settings):
    database = Database(settings.database_path)
    connection = database.connect()
    try:
        return build_cockpit_snapshot(Repository(connection))
    finally:
        connection.close()


def _seed_china_candidate(settings: Settings, *, with_snapshot: bool) -> None:
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        market = SimpleNamespace(
            id="shanghai-18c",
            slug="highest-temperature-in-shanghai-on-may-10-2026-18c",
            title="Highest temperature in Shanghai on May 10?",
            description="Outcome: 18°C. Resolved according to Wunderground on 10 May '26.",
            event_slug="highest-temperature-in-shanghai-on-may-10-2026",
            event_title="Highest temperature in Shanghai on May 10?",
            category=None,
            tags=(),
            yes_token_id="yes-token",
            no_token_id="no-token",
            close_time=None,
            status="active",
            is_weather=True,
            module_id="china_temp_bucket",
        )
        repo.upsert_market(market, {"id": market.id})
        rule = parse_china_temperature_bucket_rule(market.title, market.description)
        repo.save_temperature_bucket_rule(market.id, rule)
        snapshot = None
        if with_snapshot:
            snapshot = MarketSnapshot(
                market_id=market.id,
                best_bid=Decimal("0.05"),
                best_ask=Decimal("0.08"),
                midpoint=Decimal("0.065"),
                spread=Decimal("0.03"),
                liquidity=Decimal("40"),
                fetched_at=datetime.now(timezone.utc),
            )
            repo.save_market_snapshot(snapshot, {"id": market.id})
        repo.upsert_candidate(
            market.id,
            SimpleNamespace(tradable=True, rejection_reason=None),
            snapshot,
            status="dry_run_ready",
            notes="module=china_temp_bucket",
            module_id="china_temp_bucket",
        )
        connection.commit()
    finally:
        connection.close()
