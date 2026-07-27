from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.services.exit_guardian_service import ExitGuardianService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def test_exit_guardian_recommends_cancel_stale_order(tmp_path):
    _, connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        _seed_open_order(repo, "order-stale", updated_at=_ago(seconds=900))
        repo.save_analysis(_analysis(decision="trade", edge="0.10"))
        connection.commit()

        recommendations = ExitGuardianService(repo).evaluate(stale_threshold_seconds=300)

        assert [(item.kind, item.action) for item in recommendations] == [
            ("open_order", "cancel_stale")
        ]
        assert recommendations[0].execute is False
        assert recommendations[0].market_id == "m1"
        assert "stale" in recommendations[0].reason
    finally:
        connection.close()


def test_exit_guardian_recommends_cancel_when_edge_is_gone(tmp_path):
    _, connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        _seed_open_order(repo, "order-edge-gone", updated_at=_ago(seconds=30))
        repo.save_analysis(_analysis(decision="skip", edge="0.00"))
        connection.commit()

        recommendations = ExitGuardianService(repo).evaluate(stale_threshold_seconds=300)

        assert [(item.kind, item.action) for item in recommendations] == [
            ("open_order", "cancel_edge_gone")
        ]
        assert recommendations[0].execute is False
        assert "decision=skip" in recommendations[0].reason
    finally:
        connection.close()


def test_exit_guardian_holds_position_when_edge_is_positive_but_below_entry_minimum(tmp_path):
    _, connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        _seed_position(repo, outcome="YES", size=10, notional=4.5)
        repo.save_analysis(_analysis(decision="trade", edge="0.01"))
        connection.commit()

        recommendations = ExitGuardianService(repo).evaluate(min_edge=Decimal("0.03"))

        assert [(item.kind, item.action) for item in recommendations] == [
            ("position", "hold_for_resolution")
        ]
        assert recommendations[0].execute is False
        assert recommendations[0].notional == Decimal("4.5")
        assert recommendations[0].policy_stage == "settlement_core"
    finally:
        connection.close()


def test_exit_guardian_holds_position_when_latest_analysis_is_healthy(tmp_path):
    _, connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        _seed_position(repo, outcome="YES", size=10, notional=4.5)
        repo.save_analysis(_analysis(decision="trade", edge="0.12"))
        connection.commit()

        recommendations = ExitGuardianService(repo).evaluate(min_edge=Decimal("0.03"))

        assert [(item.kind, item.action) for item in recommendations] == [
            ("position", "hold_for_resolution")
        ]
        assert recommendations[0].execute is False
        assert "hold full reconciled position for resolution" in recommendations[0].reason
    finally:
        connection.close()


def test_exit_guardian_settlement_route_is_not_position_at_risk(tmp_path):
    _, connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        _seed_position(repo, outcome="YES", size=10, notional=4.5)
        repo.save_analysis(
            Analysis(
                market_id="m1",
                model_version="settlement-route-v1",
                fair_lower=Decimal("0"),
                fair_upper=Decimal("0"),
                reference_price=None,
                edge=Decimal("0"),
                side=None,
                decision="skip",
                reasons=["Position expired/closed; settlement state: awaiting observation"],
            )
        )
        connection.commit()

        recommendations = ExitGuardianService(repo).evaluate(min_edge=Decimal("0.03"))

        assert [(item.kind, item.action) for item in recommendations] == [
            ("position", "settlement_pending")
        ]
        assert recommendations[0].execute is False
        assert "settlement" in recommendations[0].reason.lower()
        assert recommendations[0].action != "position_at_risk"
    finally:
        connection.close()


def test_exit_guardian_does_not_sell_on_model_direction_reversal_alone(tmp_path):
    _, connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        _seed_position(repo, outcome="YES", size=10, notional=4.5)
        # A single model reversal is not sufficient evidence to liquidate YES.
        repo.save_analysis(_analysis(decision="trade", edge="0.12", side="buy_no"))
        connection.commit()

        recommendations = ExitGuardianService(repo).evaluate(min_edge=Decimal("0.03"))

        assert [(item.kind, item.action) for item in recommendations] == [
            ("position", "hold_for_resolution")
        ]
        assert "model direction reversed" in recommendations[0].reason
    finally:
        connection.close()


def _repo(tmp_path):
    database = Database(tmp_path / "guardian.db")
    database.init_schema()
    connection = database.connect()
    return database, connection, Repository(connection)


def _seed_market(repo: Repository) -> None:
    repo.upsert_market(
        Market(
            id="m1",
            slug="m1",
            title="Will NYC high temperature exceed 80F?",
            description="NOAA station KNYC",
            yes_token_id="yes-token",
            no_token_id="no-token",
            status="active",
            is_weather=True,
        ),
        {"id": "m1"},
    )


def _seed_open_order(repo: Repository, order_id: str, *, updated_at: datetime) -> None:
    repo.connection.execute(
        """
        INSERT INTO open_orders (
            exchange_order_id, market_id, token_id, side, price, size, notional,
            status, updated_at, raw_payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            order_id,
            "m1",
            "yes-token",
            "BUY",
            0.45,
            10,
            4.5,
            "open",
            updated_at.isoformat(),
            "{}",
        ),
    )


def _seed_position(repo: Repository, *, outcome: str, size: float, notional: float) -> None:
    repo.connection.execute(
        """
        INSERT INTO positions (market_id, outcome, size, notional, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("m1", outcome, size, notional, datetime.now(timezone.utc).isoformat()),
    )


def _analysis(*, decision: str, edge: str, side: str | None = None) -> Analysis:
    if side is None:
        side = "buy_yes" if decision == "trade" else None
    return Analysis(
        market_id="m1",
        model_version="test",
        fair_lower=Decimal("0.70"),
        fair_upper=Decimal("0.80"),
        reference_price=Decimal("0.45"),
        edge=Decimal(edge),
        side=side,
        decision=decision,
        reasons=["test"],
    )


def _ago(*, seconds: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)
