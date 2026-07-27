from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from typer.testing import CliRunner

from helpers import strip_ansi

from polymarket_weather_arb.cli import app
from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.services.order_lifecycle_service import (
    LifecycleRecommendation,
    OrderLifecycleService,
)
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository

runner = CliRunner()


class RecordingClient:
    """Client that records any cancel attempts (review must never call these)."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.cancel_stale_calls = 0

    def get_orders(self):
        return []

    def get_order(self, order_id: str):
        return {"id": order_id}

    def cancel_order(self, order_id: str):
        self.cancelled.append(order_id)
        return {"id": order_id, "status": "cancelled"}


def test_lifecycle_review_recommends_cancel_stale_order(tmp_path):
    _, connection, repo = _repo(tmp_path)
    client = RecordingClient()
    try:
        _seed_market(repo)
        _seed_open_order(repo, "order-stale", updated_at=_ago(seconds=900))
        repo.save_analysis(_analysis(decision="trade", edge="0.10"))
        connection.commit()

        recommendations = OrderLifecycleService(client, repo).review_lifecycle(
            stale_threshold_seconds=300
        )

        assert [(item.kind, item.action) for item in recommendations] == [
            ("open_order", "cancel_stale")
        ]
        assert recommendations[0].execute is False
        assert recommendations[0].dry_run is True
        assert recommendations[0].as_dict()["notional"] == "4.5"
        assert client.cancelled == []
    finally:
        connection.close()


def test_lifecycle_review_recommends_cancel_when_edge_is_gone(tmp_path):
    _, connection, repo = _repo(tmp_path)
    client = RecordingClient()
    try:
        _seed_market(repo)
        _seed_open_order(repo, "order-edge-gone", updated_at=_ago(seconds=30))
        repo.save_analysis(_analysis(decision="skip", edge="0.00"))
        connection.commit()

        recommendations = OrderLifecycleService(client, repo).review_lifecycle(
            stale_threshold_seconds=300
        )

        assert [(item.kind, item.action) for item in recommendations] == [
            ("open_order", "cancel_edge_gone")
        ]
        assert recommendations[0].execute is False
        assert client.cancelled == []
    finally:
        connection.close()


def test_lifecycle_review_marks_nonzero_position_at_risk(tmp_path):
    _, connection, repo = _repo(tmp_path)
    client = RecordingClient()
    try:
        _seed_market(repo)
        _seed_position(repo, outcome="YES", size=10, notional="4.5")
        repo.save_analysis(_analysis(decision="trade", edge="0.01"))
        connection.commit()

        recommendations = OrderLifecycleService(client, repo).review_lifecycle(
            min_edge=Decimal("0.03")
        )

        assert [(item.kind, item.action) for item in recommendations] == [
            ("position", "hold_for_resolution")
        ]
        assert recommendations[0].notional == Decimal("4.5")
        assert recommendations[0].latest_edge == Decimal("0.01")
        assert client.cancelled == []
    finally:
        connection.close()


def test_lifecycle_review_keeps_healthy_order_and_holds_healthy_position(tmp_path):
    _, connection, repo = _repo(tmp_path)
    client = RecordingClient()
    try:
        _seed_market(repo)
        _seed_open_order(repo, "order-healthy", updated_at=_ago(seconds=30))
        _seed_position(repo, outcome="YES", size=10, notional="4.5")
        repo.save_analysis(_analysis(decision="trade", edge="0.12"))
        connection.commit()

        recommendations = OrderLifecycleService(client, repo).review_lifecycle(
            stale_threshold_seconds=300,
            min_edge=Decimal("0.03"),
        )

        by_kind = {(item.kind, item.action) for item in recommendations}
        assert ("open_order", "keep_order") in by_kind
        assert ("position", "hold_for_resolution") in by_kind
        assert all(item.execute is False for item in recommendations)
        assert all(item.dry_run is True for item in recommendations)
        assert client.cancelled == []
    finally:
        connection.close()


def test_lifecycle_recommendation_is_serializable_with_decimals():
    rec = LifecycleRecommendation(
        kind="open_order",
        action="keep_order",
        market_id="m1",
        reason="ok",
        notional=Decimal("4.50"),
        latest_edge=Decimal("0.12"),
    )
    payload = rec.as_dict()
    assert payload["notional"] == "4.50"
    assert payload["latest_edge"] == "0.12"
    assert payload["execute"] is False
    assert payload["dry_run"] is True


def test_operator_lifecycle_review_cli_is_dry_run_only(tmp_path, monkeypatch):
    database_path = tmp_path / "operator.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    database = Database(database_path)
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        repo.upsert_market(
            SimpleNamespace(
                id="m1",
                slug="m1",
                title="Test weather market",
                description="NOAA station KNYC",
                event_slug=None,
                event_title=None,
                category=None,
                tags=(),
                yes_token_id="yes-token",
                no_token_id="no-token",
                close_time=None,
                status="active",
                is_weather=True,
            ),
            {"id": "m1"},
        )
        repo.connection.execute(
            """
            INSERT INTO open_orders (
                exchange_order_id, market_id, token_id, side, price, size, notional,
                status, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("order-1", "m1", "yes-token", "BUY", 0.5, 10, 5, "open", "{}"),
        )
        repo.save_analysis(
            Analysis(
                market_id="m1",
                model_version="test",
                fair_lower=Decimal("0.40"),
                fair_upper=Decimal("0.45"),
                reference_price=Decimal("0.50"),
                edge=Decimal("0.00"),
                side=None,
                decision="skip",
                reasons=["edge gone"],
            )
        )
        connection.commit()
    finally:
        connection.close()

    result = runner.invoke(app, ["operator", "lifecycle-review"])

    output = strip_ansi(result.stdout)
    assert result.exit_code == 0
    assert "cancel_edge_gone" in output
    assert "order-1" in output
    assert "Lifecycle recommendations:" in output
    assert "Dry-run only: no orders were cancelled or closed." in output

    # Confirm open order was not cancelled by the CLI path.
    connection = database.connect()
    try:
        row = connection.execute(
            "SELECT status FROM open_orders WHERE exchange_order_id = ?",
            ("order-1",),
        ).fetchone()
        assert row is not None
        assert row["status"] == "open"
    finally:
        connection.close()


def _repo(tmp_path):
    database = Database(tmp_path / "lifecycle.db")
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
            "0.45",
            "10",
            "4.5",
            "open",
            updated_at.isoformat(),
            "{}",
        ),
    )


def _seed_position(repo: Repository, *, outcome: str, size: object, notional: object) -> None:
    repo.connection.execute(
        """
        INSERT INTO positions (market_id, outcome, size, notional, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("m1", outcome, str(size), str(notional), datetime.now(timezone.utc).isoformat()),
    )


def _analysis(*, decision: str, edge: str) -> Analysis:
    return Analysis(
        market_id="m1",
        model_version="test",
        fair_lower=Decimal("0.70"),
        fair_upper=Decimal("0.80"),
        reference_price=Decimal("0.45"),
        edge=Decimal(edge),
        side="buy_yes" if decision == "trade" else None,
        decision=decision,
        reasons=["test"],
    )


def _ago(*, seconds: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)
