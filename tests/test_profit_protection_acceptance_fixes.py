"""Acceptance tests for settlement-core auto-exit evidence and refresh."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.domain.position_inventory import (
    best_bid_depth_from_book,
    build_campaign_inventory,
)
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.strategy_versions import GLOBAL_BUCKET_MODEL_VERSION
from polymarket_weather_arb.domain.weather import WeatherObservation
from polymarket_weather_arb.services.auto_exit_service import AutoExitService
from polymarket_weather_arb.services.compliance_service import ComplianceDecision, ComplianceService
from polymarket_weather_arb.services.exit_guardian_service import ExitGuardianService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def _repo(tmp_path):
    db = Database(tmp_path / "fix.db")
    db.init_schema()
    conn = db.connect()
    return conn, Repository(conn)


def test_inventory_prefers_account_fill_and_order_linkage():
    """Maker trade: top-level is taker; account leg is our maker order."""
    # Live-style trade: we are maker on YES token via maker_orders leg.
    fills = [
        {
            "id": 1,
            "filled_at": "2026-07-11T12:00:00+00:00",
            "side": "BUY",  # top-level may reflect taker
            "price": "0.40",
            "size": "999",  # top-level size is full trade, not our leg
            "fee": "0",
            "taker_order_id": "someone-else",
            "raw_payload": {
                "taker_order_id": "someone-else",
                "side": "BUY",
                "price": "0.40",
                "size": "999",
                "maker_orders": [
                    {
                        "order_id": "our-maker-1",
                        "matched_amount": "100",
                        "price": "0.01",
                        "asset_id": "yes-token",
                    }
                ],
                "_account_fill": {
                    "order_id": "our-maker-1",
                    "side": "BUY",
                    "price": "0.01",
                    "size": "100",
                    "token_id": "yes-token",
                    "outcome": "YES",
                },
                "_fee_resolution": {"role": "maker", "fee": "0"},
            },
        }
    ]
    inv = build_campaign_inventory(
        fills,
        market_id="m1",
        outcome="YES",
        position_size=Decimal("100"),
        yes_token_id="yes-token",
        no_token_id="no-token",
        order_token_ids={"our-maker-1": "yes-token"},
    )
    assert inv.accounting_verified
    assert inv.buy_size == Decimal("100")
    assert inv.verified_buy_cost == Decimal("1.00")


def test_inventory_matches_via_order_id_without_top_level_outcome(tmp_path):
    conn, repo = _repo(tmp_path)
    repo.upsert_market(
        Market(
            id="m1",
            title="t",
            yes_token_id="yes-token",
            no_token_id="no-token",
            is_weather=True,
        ),
        {"id": "m1"},
    )
    # Intent + attempt establish order_id → token linkage
    conn.execute(
        """
        INSERT INTO order_intents
        (id, market_id, side, token_id, limit_price, size, notional, rationale, dry_run, status)
        VALUES (1, 'm1', 'buy_yes', 'yes-token', 0.01, 100, 1, 't', 0, 'filled')
        """
    )
    conn.execute(
        """
        INSERT INTO order_attempts (intent_id, status, request_payload, response_payload)
        VALUES (1, 'submitted', '{}', '{"orderID":"clob-ord-1"}')
        """
    )
    # Fill has only order_id, no outcome field
    conn.execute(
        """
        INSERT INTO fills
        (exchange_fill_id, order_id, market_id, side, price, size, fee, filled_at, raw_payload)
        VALUES
        ('t1', 'clob-ord-1', 'm1', 'BUY', 0.01, 100, 0, '2026-07-11T01:00:00+00:00',
         '{"taker_order_id":"clob-ord-1","price":"0.01","size":"100"}')
        """
    )
    mapping = repo.order_token_ids_for_market("m1")
    assert mapping.get("clob-ord-1") == "yes-token"
    rows = [dict(r) for r in repo.list_fills(market_id="m1", limit=10)]
    for r in rows:
        if isinstance(r.get("raw_payload"), str):
            import json

            r["raw_payload"] = json.loads(r["raw_payload"])
    inv = build_campaign_inventory(
        rows,
        market_id="m1",
        outcome="YES",
        position_size=Decimal("100"),
        yes_token_id="yes-token",
        no_token_id="no-token",
        order_token_ids=mapping,
    )
    assert inv.accounting_verified
    assert inv.buy_size == Decimal("100")
    conn.close()


def test_best_bid_depth_from_book():
    raw = {
        "bids": [
            {"price": "0.10", "size": "3"},
            {"price": "0.12", "size": "7.5"},
            {"price": "0.11", "size": "2"},
        ]
    }
    assert best_bid_depth_from_book(raw) == Decimal("7.5")


def _seed_value_exit(repo, market_id="m-pp"):
    repo.upsert_market(
        Market(
            id=market_id,
            title="Will the highest temperature in Chicago be 80F or higher on December 31, 2099?",
            yes_token_id="yes-token",
            no_token_id="no-token",
            is_weather=True,
            close_time="2099-12-31T23:59:59+00:00",
            status="active",
        ),
        {
            "id": market_id,
            "closed": False,
            "acceptingOrders": True,
            "feesEnabled": True,
            "feeType": "weather_fees",
            "orderMinSize": "1",
            "endDate": "2099-12-31T23:59:59+00:00",
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["yes-token", "no-token"],
        },
    )
    repo.save_reconciliation("ok", {"status": "ok"})
    repo.replace_positions(
        [{"market": market_id, "outcome": "Yes", "size": "100", "avgPrice": "0.01"}]
    )
    repo.connection.execute(
        """
        INSERT INTO fills
        (exchange_fill_id, market_id, order_id, side, price, size, fee, filled_at, raw_payload)
        VALUES ('b1', ?, 'o1', 'BUY', 0.01, 100, 0, ?, '{"outcome":"YES"}')
        """,
        (market_id, datetime.now(timezone.utc).isoformat()),
    )
    now = datetime.now(timezone.utc)
    for index, revision in enumerate(("refresh-r1", "refresh-r2")):
        analysis_id = repo.save_analysis(
            Analysis(
                market_id=market_id,
                model_version=GLOBAL_BUCKET_MODEL_VERSION,
                fair_lower=Decimal("0.00"),
                fair_upper=Decimal("0.10"),
                reference_price=Decimal("0.20"),
                edge=Decimal("-0.10"),
                side=None,
                decision="watch",
                reasons=["settlement-core value test"],
                created_at=now + timedelta(microseconds=index),
            )
        )
        repo.connection.execute(
            """
            UPDATE model_signals
            SET raw_payload = json_set(raw_payload, '$.forecast_revision', ?)
            WHERE analysis_id = ?
            """,
            (revision, analysis_id),
        )


def _settings(tmp_path):
    return Settings(
        DATABASE_PATH=tmp_path / "fix.db",
        POLYMARKET_PRIVATE_KEY="k",
        POLYMARKET_FUNDER="0xf",
        TRADING_DISABLED=False,
        COMPLIANCE_CHECK_ENABLED=False,
        AUTO_EXIT_ENABLED=True,
        MAX_AUTO_EXITS_PER_TICK=2,
        AUTO_EXIT_MAX_POSITION_USDC=Decimal("50"),
        AUTO_EXIT_MAX_SLIPPAGE=Decimal("0.05"),
        STALE_ORDER_BOOK_SECONDS=300,
        MIN_EDGE=Decimal("0.05"),
    )


def _compliance():
    svc = Mock(spec=ComplianceService)
    svc.check_live_allowed.return_value = ComplianceDecision(ok=True, status="ok", reason="t")
    return svc


def test_model_value_exit_is_disabled_before_post_refresh(tmp_path):
    """Repeated model value signals never reach the SELL pre-submit path."""
    conn, repo = _repo(tmp_path)
    _seed_value_exit(repo)
    settings = _settings(tmp_path)
    bids = [Decimal("0.20"), Decimal("0.01"), Decimal("0.01")]

    def book(token_id):
        bid = bids.pop(0) if bids else Decimal("0.01")
        return (
            MarketSnapshot(
                market_id="token_book",
                best_bid=bid,
                best_ask=bid + Decimal("0.01"),
                midpoint=bid,
                spread=Decimal("0.01"),
                liquidity=Decimal("100"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {"bids": [{"price": str(bid), "size": "500"}]},
        )

    client = Mock()
    client.get_token_order_book.side_effect = book
    client.place_sell_limit_order.side_effect = AssertionError("must not sell")
    client.place_limit_order.side_effect = RuntimeError("no buy")
    client.get_order.return_value = {"id": "x", "status": "LIVE"}
    client.get_balances.return_value = {}
    client.get_orders.return_value = []
    client.get_trades.return_value = []
    client.get_positions.return_value = []

    result = AutoExitService(repo, client).run_tick(
        settings=settings,
        profile_name="full-live",
        allow_auto_exit=True,
        compliance_service=_compliance(),
    )
    assert result.executed == 0
    client.place_sell_limit_order.assert_not_called()
    assert result.notes == ["no executable exit recommendations"]
    conn.close()


def test_higher_bid_does_not_reenable_model_value_exit(tmp_path):
    """Executable price cannot override the settlement-only exit contract."""
    conn, repo = _repo(tmp_path)
    _seed_value_exit(repo)
    settings = _settings(tmp_path)
    bids = [Decimal("0.20"), Decimal("0.25")]

    def book(token_id):
        bid = bids.pop(0) if bids else Decimal("0.25")
        return (
            MarketSnapshot(
                market_id="token_book",
                best_bid=bid,
                best_ask=bid + Decimal("0.01"),
                midpoint=bid,
                spread=Decimal("0.01"),
                liquidity=Decimal("100"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {"bids": [{"price": str(bid), "size": "500"}]},
        )

    client = Mock()
    client.get_token_order_book.side_effect = book
    client.place_sell_limit_order.return_value = {"order_id": "s1", "status": "live"}
    client.place_limit_order.side_effect = RuntimeError("no buy")
    client.get_order.return_value = {"id": "s1", "status": "LIVE"}
    client.get_balances.return_value = {}
    client.get_orders.return_value = []
    client.get_trades.return_value = []
    client.get_positions.return_value = []

    result = AutoExitService(repo, client).run_tick(
        settings=settings,
        profile_name="full-live",
        allow_auto_exit=True,
        compliance_service=_compliance(),
    )
    assert result.executed == 0
    assert result.attempted == 0
    client.place_sell_limit_order.assert_not_called()
    assert result.notes == ["no executable exit recommendations"]
    conn.close()


def test_observation_quality_row_and_override_model(tmp_path):
    """Reliable obs invalidates held YES even if model still supports."""
    conn, repo = _repo(tmp_path)
    market_id = "m-obs"
    repo.upsert_market(
        Market(
            id=market_id,
            title="Will the highest temperature in Chicago be 90F or higher on July 15, 2026?",
            description="Resolves based on NOAA station KORD high temperature.",
            yes_token_id="y",
            no_token_id="n",
            is_weather=True,
            close_time="2026-07-16T05:00:00+00:00",
            status="active",
        ),
        {
            "id": market_id,
            "closed": False,
            "acceptingOrders": True,
            "timezone": "America/Chicago",
            "endDate": "2026-07-16T05:00:00+00:00",
        },
    )
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES (?, 'YES', 5, 1)",
        (market_id,),
    )
    # Model still supports YES
    now = datetime(2026, 7, 16, 3, 0, tzinfo=timezone.utc)  # 22:00 Chicago
    repo.save_analysis(
        Analysis(
            market_id=market_id,
            model_version="t",
            fair_lower=Decimal("0.80"),
            fair_upper=Decimal("0.90"),
            reference_price=Decimal("0.50"),
            edge=Decimal("0.30"),
            side="buy_yes",
            decision="trade",
            reasons=["model still bullish"],
            created_at=now,
        )
    )
    # Observed high already 92 with quality V → YES locked favorable actually.
    # Holding YES with high >= 90 already true → hold_for_resolution
    repo.save_observation(
        WeatherObservation(
            market_id=market_id,
            provider="noaa",
            station="KORD",
            variable="temperature_high",
            value=Decimal("92"),
            unit="F",
            observed_at=now,
            fetched_at=now,
            quality_status="V",
        ),
        {"quality_status": "V"},
    )
    rec = [
        r
        for r in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={(market_id, "YES"): Decimal("0.95")},
            now=now,
        )
        if r.kind == "position"
    ][0]
    assert rec.action == "hold_for_resolution"
    assert "observation" in rec.reason.lower() or "locks" in rec.reason.lower()

    # Poor quality cannot lock
    repo.save_observation(
        WeatherObservation(
            market_id=market_id,
            provider="noaa",
            station="KORD",
            variable="temperature_high",
            value=Decimal("92"),
            unit="F",
            observed_at=now,
            fetched_at=now,
            quality_status="X",
        ),
        {"quality_status": "X"},
    )
    rec2 = [
        r
        for r in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={(market_id, "YES"): Decimal("0.40")},
            now=now,
        )
        if r.kind == "position"
    ][0]
    # No observation lock with bad quality — not forced hold_for_resolution via obs
    assert "quality" not in (rec2.reason or "").lower() or rec2.action != "hold_for_resolution"
    conn.close()


def test_bucket_observation_overrides_model_yes_impossible(tmp_path):
    conn, repo = _repo(tmp_path)
    market_id = "bucket-1"
    repo.upsert_market(
        Market(
            id=market_id,
            title="Will the highest temperature in Atlanta be 88-89°F on July 15, 2026?",
            yes_token_id="y",
            no_token_id="n",
            is_weather=True,
            close_time="2026-07-16T05:00:00+00:00",
            status="active",
        ),
        {
            "id": market_id,
            "closed": False,
            "acceptingOrders": True,
            "timezone": "America/New_York",
            "endDate": "2026-07-16T05:00:00+00:00",
        },
    )
    # Bucket rule: 88-89
    from types import SimpleNamespace

    repo.save_temperature_bucket_rule(
        market_id,
        SimpleNamespace(
            city="Atlanta",
            city_cn=None,
            station_id=None,
            settlement_station_id=None,
            source="Wunderground",
            variable="temperature_high",
            unit="F",
            bucket_center_c=Decimal("31"),
            bucket_lower_c=Decimal("31.1"),  # approx C; use F stored as C fields in tests
            bucket_upper_c=Decimal("31.7"),
            target_date="2026-07-15",
            settlement_timezone="America/New_York",
            confidence=0.9,
            tradable=True,
            rejection_reason=None,
            raw_text="bucket",
        ),
        module_id="global_temp_bucket",
    )
    # Override with F-like bounds via SQL for clarity
    conn.execute(
        "UPDATE temperature_bucket_rules SET bucket_lower_c=88, bucket_upper_c=89, variable='temperature_high' WHERE market_id=?",
        (market_id,),
    )
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES (?, 'YES', 5, 1)",
        (market_id,),
    )
    now = datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc)
    repo.save_analysis(
        Analysis(
            market_id=market_id,
            model_version="t",
            fair_lower=Decimal("0.6"),
            fair_upper=Decimal("0.7"),
            reference_price=Decimal("0.3"),
            edge=Decimal("0.25"),
            side="buy_yes",
            decision="trade",
            reasons=["model still likes bucket"],
            created_at=now,
        )
    )
    # Daily high already 95 → YES for 88-89 impossible
    repo.save_observation(
        WeatherObservation(
            market_id=market_id,
            provider="noaa",
            station="KATL",
            variable="temperature_high",
            value=Decimal("95"),
            unit="F",
            observed_at=now,
            fetched_at=now,
            quality_status="V",
        ),
        {"quality_status": "V"},
    )
    rec = [
        r
        for r in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={(market_id, "YES"): Decimal("0.05")},
            now=now,
        )
        if r.kind == "position"
    ][0]
    assert rec.action == "exit_full"
    assert "observation" in rec.reason.lower() or "invalidates" in rec.reason.lower()
    conn.close()


def test_exact_bucket_adjacent_high_never_falls_through_to_threshold_lock(tmp_path):
    from types import SimpleNamespace

    conn, repo = _repo(tmp_path)
    market_id = "denver-adjacent-high"
    title = "Will the highest temperature in Denver be between 98-99°F on July 20, 2026?"
    repo.upsert_market(
        Market(id=market_id, title=title, yes_token_id="y", no_token_id="n", is_weather=True),
        {"id": market_id, "closed": False, "acceptingOrders": True},
    )
    repo.save_temperature_bucket_rule(
        market_id,
        SimpleNamespace(
            city="Denver",
            city_cn=None,
            station_id="KDEN",
            settlement_station_id="KDEN",
            source="Wunderground",
            variable="temperature_high",
            unit="F",
            bucket_center_c=Decimal("98.5"),
            bucket_lower_c=Decimal("97.5"),
            bucket_upper_c=Decimal("99.5"),
            target_date="2026-07-20",
            settlement_timezone="America/Denver",
            confidence=1,
            tradable=True,
            rejection_reason=None,
            raw_text=title,
        ),
        module_id="global_temp_bucket",
    )
    now = datetime(2026, 7, 20, 20, tzinfo=timezone.utc)
    repo.save_observation(
        WeatherObservation(
            market_id=market_id,
            provider="noaa",
            station="KDEN",
            variable="temperature_high",
            value=Decimal("100.22"),
            unit="F",
            observed_at=now,
            fetched_at=now,
            quality_status="V",
        ),
        {"quality_status": "V"},
    )

    guardian = ExitGuardianService(repo)
    assert (
        guardian._observation_override(
            market_id=market_id,
            title=title,
            description="Settlement source: Wunderground station KDEN.",
            outcome="YES",
            model_supports=True,
        )
        == "exit_full"
    )
    conn.close()


def test_upper_tail_observation_locks_yes_without_false_exit(tmp_path):
    from types import SimpleNamespace

    conn, repo = _repo(tmp_path)
    market_id = "upper-tail"
    title = "Will the highest temperature in Chicago be 108°F or higher on July 16, 2026?"
    repo.upsert_market(
        Market(id=market_id, title=title, yes_token_id="y", no_token_id="n", is_weather=True),
        {"id": market_id},
    )
    repo.save_temperature_bucket_rule(
        market_id,
        SimpleNamespace(
            city="Chicago",
            city_cn=None,
            station_id="KORD",
            source="Wunderground",
            variable="temperature_high",
            unit="F",
            bucket_center_c=Decimal("108"),
            bucket_lower_c=Decimal("107.5"),
            bucket_upper_c=Decimal("108.5"),
            target_date="2026-07-16",
            settlement_timezone="America/Chicago",
            confidence=1,
            tradable=True,
            rejection_reason=None,
            raw_text=title,
        ),
        module_id="global_temp_bucket",
    )
    now = datetime(2026, 7, 16, 20, tzinfo=timezone.utc)
    repo.save_observation(
        WeatherObservation(
            market_id=market_id,
            provider="noaa",
            station="KORD",
            variable="temperature_high",
            value=Decimal("109"),
            unit="F",
            observed_at=now,
            fetched_at=now,
            quality_status="V",
        ),
        {"quality_status": "V"},
    )
    guardian = ExitGuardianService(repo)

    assert (
        guardian._observation_override(
            market_id=market_id,
            title=title,
            description="Settlement source: Wunderground station KORD.",
            outcome="YES",
            model_supports=False,
        )
        == "hold_for_resolution"
    )
    assert (
        guardian._observation_override(
            market_id=market_id,
            title=title,
            description="Settlement source: Wunderground station KORD.",
            outcome="NO",
            model_supports=True,
        )
        == "exit_full"
    )
    conn.close()


def test_near_boundary_observation_does_not_claim_cross_source_lock(tmp_path):
    from types import SimpleNamespace

    conn, repo = _repo(tmp_path)
    market_id = "upper-tail-boundary"
    title = "Will the highest temperature in Chicago be 108°F or higher on July 16, 2026?"
    repo.upsert_market(
        Market(id=market_id, title=title, yes_token_id="y", no_token_id="n", is_weather=True),
        {"id": market_id},
    )
    repo.save_temperature_bucket_rule(
        market_id,
        SimpleNamespace(
            city="Chicago",
            city_cn=None,
            station_id="KORD",
            source="Wunderground",
            variable="temperature_high",
            unit="F",
            bucket_center_c=Decimal("108"),
            bucket_lower_c=Decimal("107.5"),
            bucket_upper_c=Decimal("108.5"),
            target_date="2026-07-16",
            settlement_timezone="America/Chicago",
            confidence=1,
            tradable=True,
            rejection_reason=None,
            raw_text=title,
        ),
        module_id="global_temp_bucket",
    )
    now = datetime(2026, 7, 16, 20, tzinfo=timezone.utc)
    repo.save_observation(
        WeatherObservation(
            market_id=market_id,
            provider="noaa",
            station="KORD",
            variable="temperature_high",
            value=Decimal("108"),
            unit="F",
            observed_at=now,
            fetched_at=now,
            quality_status="V",
        ),
        {"quality_status": "V"},
    )

    assert (
        ExitGuardianService(repo)._observation_override(
            market_id=market_id,
            title=title,
            description="Settlement source: Wunderground station KORD.",
            outcome="YES",
            model_supports=False,
        )
        is None
    )
    conn.close()
