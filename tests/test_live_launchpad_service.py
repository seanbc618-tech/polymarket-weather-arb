from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.execution import OrderIntent
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.rules import ResolutionRule
from polymarket_weather_arb.domain.weather import ForecastSnapshot
from polymarket_weather_arb.services.live_launchpad_service import build_live_launchpad_snapshot
from polymarket_weather_arb.services.live_launchpad_service import live_market_ids_from_settings
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def test_live_market_ids_from_settings_splits_comma_list():
    settings = Settings(LIVE_MARKET_IDS=" m1,https://polymarket.com/event/foo?tid=m2,,m3 ")

    assert live_market_ids_from_settings(settings) == {"m1", "m2", "m3"}


def test_launchpad_snapshot_is_locked_without_live_gates(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        connection.commit()

        snapshot = build_live_launchpad_snapshot(
            repo,
            Settings(TRADING_DISABLED=True),
            check_exchange=False,
            live_market_ids=set(),
        )

        assert snapshot.can_execute is False
        assert snapshot.candidates
        candidate = snapshot.candidates[0]
        assert candidate.market_id == "m1"
        assert candidate.can_preview is False
        assert candidate.whitelisted is False
        assert candidate.override_enabled is False
        assert candidate.reconciliation_fresh is False
        assert "market is not whitelisted" in candidate.blockers
        assert any(gate.name == "credentials" for gate in snapshot.readiness_gates)
        assert "TRADING_DISABLED=true blocks live trading" in snapshot.blockers
    finally:
        connection.close()


def test_launchpad_candidate_can_preview_with_whitelist_override_and_fresh_reconciliation(
    tmp_path,
):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        repo.upsert_strategy_override(
            market_id="m1",
            profile="micro-live",
            live_auto_enabled=True,
            max_order_usdc="2",
        )
        repo.save_reconciliation("ok", {"balances": [], "orders": [], "positions": []})
        connection.commit()

        snapshot = build_live_launchpad_snapshot(
            repo,
            Settings(
                POLYMARKET_PRIVATE_KEY="key",
                POLYMARKET_FUNDER="funder",
                COMPLIANCE_CHECK_ENABLED=False,
                MAX_ORDER_USDC="25",
                MAX_DAILY_USDC="100",
                MAX_MARKET_USDC="50",
            ),
            check_exchange=False,
            live_market_ids={"m1"},
        )

        candidate = snapshot.candidates[0]
        assert candidate.can_preview is True
        assert candidate.whitelisted is True
        assert candidate.override_enabled is True
        assert candidate.reconciliation_fresh is True
        assert candidate.profile == "micro-live"
        assert candidate.max_order_usdc == "2"
        assert candidate.blockers == []
        assert snapshot.preview is None
    finally:
        connection.close()


def test_launchpad_preview_uses_latest_analysis_and_risk_engine(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo, include_forecast=True)
        repo.upsert_strategy_override(
            market_id="m1",
            profile="micro-live",
            live_auto_enabled=True,
            max_order_usdc="2",
        )
        repo.save_reconciliation("ok", {"balances": [], "orders": [], "positions": []})
        connection.commit()

        snapshot = build_live_launchpad_snapshot(
            repo,
            Settings(
                POLYMARKET_PRIVATE_KEY="key",
                POLYMARKET_FUNDER="funder",
                COMPLIANCE_CHECK_ENABLED=False,
                LIVE_MARKET_IDS="m1",
                MAX_ORDER_USDC="25",
                MAX_DAILY_USDC="100",
                MAX_MARKET_USDC="50",
            ),
            check_exchange=False,
            preview_market_id="m1",
        )

        assert snapshot.live_market_ids == ["m1"]
        assert snapshot.preview is not None
        assert snapshot.preview.market_id == "m1"
        assert snapshot.preview.side == "buy_yes"
        assert snapshot.preview.token_id == "yes-token"
        assert snapshot.preview.limit_price == "0.45"
        assert snapshot.preview.size == "4.44"
        assert snapshot.preview.notional == "1.998"
        assert snapshot.preview.accepted is True
        assert snapshot.preview.risk_reasons == ["accepted"]
    finally:
        connection.close()


def test_launchpad_candidate_includes_calibration_trust(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo, include_forecast=True)
        repo.settle_model_signals_for_market(
            "m1",
            resolved_outcome="yes",
            settlement_value=Decimal("83"),
            settlement_source="nws-observation",
        )
        connection.commit()

        snapshot = build_live_launchpad_snapshot(
            repo,
            Settings(TRADING_DISABLED=True),
            check_exchange=False,
            live_market_ids=set(),
        )

        candidate = snapshot.candidates[0]
        assert candidate.calibration_status == "collecting"
        assert candidate.calibration_total_signals == 1
        assert candidate.calibration_resolved_signals == 1
        assert candidate.calibration_brier_score == "0.1225"
        assert candidate.calibration_hit_rate == "1"
    finally:
        connection.close()


def test_launchpad_snapshot_includes_order_and_position_risk(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        now = datetime.now(timezone.utc)
        stale_time = now - timedelta(seconds=600)
        repo.connection.execute(
            """
            INSERT INTO open_orders (
                exchange_order_id, market_id, side, price, size, notional,
                status, updated_at, raw_payload
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("order-stale", "m1", "buy", 0.5, 10, 5.0, "open", stale_time.isoformat(), "{}"),
        )
        repo.connection.execute(
            """
            INSERT INTO positions (market_id, outcome, size, notional, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("m1", "YES", -4, -3.2, now.isoformat()),
        )
        connection.commit()

        snapshot = build_live_launchpad_snapshot(
            repo,
            Settings(TRADING_DISABLED=True),
            check_exchange=False,
            live_market_ids=set(),
        )

        assert snapshot.open_orders_count == 1
        assert snapshot.stale_open_orders_count == 1
        assert snapshot.open_orders_notional == "5"
        assert snapshot.positions_count == 1
        assert snapshot.nonzero_positions_count == 1
        assert snapshot.position_total_exposure == "3.2"
        assert snapshot.position_max_market_exposure == "3.2"
        assert snapshot.position_concentration_risk == "1"
        assert snapshot.position_market_exposures == {"m1": "3.2"}
    finally:
        connection.close()


def test_launchpad_allows_micro_live_ready_global_bucket_preview(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo, market_id="g1", module_id="global_temp_bucket")
        repo.upsert_strategy_override(
            market_id="g1",
            profile="micro-live",
            live_auto_enabled=True,
            max_order_usdc="2",
        )
        repo.save_reconciliation("ok", {"balances": [], "orders": [], "positions": []})
        connection.commit()

        snapshot = build_live_launchpad_snapshot(
            repo,
            Settings(
                POLYMARKET_PRIVATE_KEY="key",
                POLYMARKET_FUNDER="funder",
                COMPLIANCE_CHECK_ENABLED=False,
            ),
            check_exchange=False,
            live_market_ids={"g1"},
            preview_market_id="g1",
        )

        candidate = snapshot.candidates[0]
        assert candidate.module_id == "global_temp_bucket"
        assert candidate.credibility_live_eligibility == "micro_live_ready"
        assert candidate.can_preview is True
        assert candidate.blockers == []
        assert snapshot.preview is not None
    finally:
        connection.close()


def _repo(tmp_path):
    database = Database(tmp_path / "launchpad.db")
    database.init_schema()
    connection = database.connect()
    return connection, Repository(connection)


def _seed_market(
    repo: Repository,
    market_id: str = "m1",
    *,
    include_forecast: bool = False,
    module_id: str = "weather",
) -> None:
    market = Market(
        id=market_id,
        slug=market_id,
        title="NYC high temp live candidate",
        description="Will NYC high temperature be above 80F?",
        yes_token_id="yes-token",
        no_token_id="no-token",
        status="active",
        is_weather=True,
    )
    rule = ResolutionRule(
        raw_text=market.description,
        location="New York",
        source="NOAA",
        station="KNYC",
        variable="temperature_high",
        threshold=Decimal("80"),
        operator=">",
        window_start="2026-06-09",
        window_end=None,
        unit="F",
        confidence=0.9,
        tradable=True,
        rejection_reason=None,
    )
    snapshot = MarketSnapshot(
        market_id=market_id,
        best_bid=Decimal("0.40"),
        best_ask=Decimal("0.45"),
        midpoint=Decimal("0.425"),
        spread=Decimal("0.05"),
        liquidity=Decimal("100"),
        fetched_at=datetime.now(timezone.utc),
    )
    repo.upsert_market(
        type("ModuleMarket", (), {**market.__dict__, "module_id": module_id})(), {"id": market_id}
    )
    repo.save_resolution_rule(market_id, rule)
    repo.save_market_snapshot(snapshot, {"id": market_id})
    if include_forecast:
        now = datetime.now(timezone.utc)
        repo.save_forecast(
            ForecastSnapshot(
                provider="test",
                variable="temperature_high",
                value=Decimal("83"),
                unit="F",
                issue_time=now,
                valid_time=now,
                market_id=market_id,
                location="New York",
                station="KNYC",
                fetched_at=now,
            ),
            {"id": market_id},
        )
    repo.save_analysis(
        Analysis(
            market_id=market_id,
            model_version="test",
            fair_lower=Decimal("0.60"),
            fair_upper=Decimal("0.70"),
            reference_price=Decimal("0.45"),
            edge=Decimal("0.15"),
            side="buy_yes",
            decision="trade",
            reasons=["edge exists"],
        )
    )
    repo.save_order_intent(
        OrderIntent(
            market_id=market_id,
            side="buy_yes",
            token_id="yes-token",
            limit_price=Decimal("0.45"),
            size=Decimal("4"),
            notional=Decimal("1.80"),
            rationale="dry run",
            dry_run=True,
            status="dry_run",
        )
    )
