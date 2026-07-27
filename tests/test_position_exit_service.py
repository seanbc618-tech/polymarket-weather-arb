import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.execution import (
    build_close_confirm_phrase,
    exit_order_idempotency_key,
)
from polymarket_weather_arb.domain.markets import MarketSnapshot
from polymarket_weather_arb.services.compliance_service import ComplianceDecision, ComplianceService
from polymarket_weather_arb.services.position_exit_service import PositionExitService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


@pytest.fixture
def mock_repo():
    repo = Mock()
    repo.latest_successful_reconciliation.return_value = {
        "id": 17,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    repo.get_market.return_value = {
        "raw_payload": json.dumps(
            {
                "outcomes": ["Yes", "No"],
                "clobTokenIds": ["yes-token", "no-token"],
            }
        )
    }
    repo.list_positions.return_value = [
        {"market_id": "market-1", "outcome": "YES", "size": 100.0, "token_id": "yes-token"}
    ]
    repo.active_live_order_intent.return_value = None
    repo.active_open_order.return_value = None
    repo.get_circuit_breaker_state.return_value = {
        "circuit_breaker_tripped": 0,
        "circuit_breaker_reason": None,
        "tripped_at": None,
    }
    repo.save_order_intent_once.return_value = (42, True)
    repo.save_order_attempt.return_value = 1
    # ReconciliationService unpacks (count, newly_inserted).
    repo.save_reconciled_fills.return_value = (0, [])
    repo.replace_positions.return_value = 0
    repo.replace_open_orders.return_value = 0
    return repo


@pytest.fixture
def mock_client():
    client = Mock()
    client.place_limit_order.side_effect = RuntimeError("BUY mutation must not be called")
    client.place_sell_limit_order.side_effect = RuntimeError("SELL mutation unexpected")

    def mock_get_token_order_book(token_id):
        return MarketSnapshot(
            market_id="token_book",
            best_bid=Decimal("0.5"),
            best_ask=Decimal("0.6"),
            midpoint=Decimal("0.55"),
            spread=Decimal("0.1"),
            liquidity=Decimal("1000"),
            fetched_at=datetime.now(timezone.utc),
        ), {}

    client.get_token_order_book.side_effect = mock_get_token_order_book
    client.get_order.return_value = {"id": "order-sell-1", "status": "LIVE"}
    client.get_balances.return_value = {"balance": 1}
    client.get_orders.return_value = []
    client.get_trades.return_value = []
    client.get_positions.return_value = []
    return client


@pytest.fixture
def live_settings(tmp_path):
    return Settings(
        DATABASE_PATH=tmp_path / "exit.db",
        POLYMARKET_PRIVATE_KEY="test-key",
        POLYMARKET_FUNDER="0xfunder",
        TRADING_DISABLED=False,
        COMPLIANCE_CHECK_ENABLED=False,
        STALE_ORDER_BOOK_SECONDS=300,
    )


def _allowed_compliance():
    service = Mock(spec=ComplianceService)
    service.check_live_allowed.return_value = ComplianceDecision(
        ok=True, status="check_disabled", reason="test"
    )
    return service


def test_preview_close_yes_full_size(mock_repo, mock_client):
    service = PositionExitService(mock_repo, mock_client)
    result = service.preview_close(settings=Settings(), market_id="market-1", outcome="YES")

    assert result["market_id"] == "market-1"
    assert result["outcome"] == "YES"
    assert result["token_id"] == "yes-token"
    assert result["actual_size"] == Decimal("100")
    assert result["close_size"] == Decimal("100")
    assert result["best_bid"] == Decimal("0.5")
    assert result["estimated_usdc"] == Decimal("50.0")
    assert result["reconciliation_fresh"] is True
    mock_client.place_limit_order.assert_not_called()
    mock_client.place_sell_limit_order.assert_not_called()


def test_preview_close_no_with_percent(mock_repo, mock_client):
    mock_repo.list_positions.return_value = [
        {"market_id": "market-1", "outcome": "NO", "size": 200.0, "token_id": "no-token"}
    ]
    service = PositionExitService(mock_repo, mock_client)
    result = service.preview_close(
        settings=Settings(), market_id="market-1", outcome="NO", percent=Decimal("50")
    )

    assert result["token_id"] == "no-token"
    assert result["close_size"] == Decimal("100")
    assert result["estimated_usdc"] == Decimal("50.0")


def test_preview_close_with_partial_size(mock_repo, mock_client):
    service = PositionExitService(mock_repo, mock_client)
    result = service.preview_close(
        settings=Settings(), market_id="market-1", outcome="YES", size=Decimal("25")
    )
    assert result["close_size"] == Decimal("25")
    assert result["estimated_usdc"] == Decimal("12.5")


def test_preview_close_exceeds_size(mock_repo, mock_client):
    service = PositionExitService(mock_repo, mock_client)
    with pytest.raises(ValueError, match="exceeds actual position size"):
        service.preview_close(
            settings=Settings(), market_id="market-1", outcome="YES", size=Decimal("101")
        )


def test_preview_close_no_position(mock_repo, mock_client):
    mock_repo.list_positions.return_value = []
    service = PositionExitService(mock_repo, mock_client)
    with pytest.raises(ValueError, match="No nonzero position found"):
        service.preview_close(settings=Settings(), market_id="market-1", outcome="YES")


def test_preview_close_stale_reconciliation(mock_repo, mock_client):
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    mock_repo.latest_successful_reconciliation.return_value = {"created_at": stale_time.isoformat()}
    service = PositionExitService(mock_repo, mock_client)
    with pytest.raises(ValueError, match="Reconciliation missing or stale"):
        service.preview_close(settings=Settings(), market_id="market-1", outcome="YES")


def test_preview_close_missing_reconciliation(mock_repo, mock_client):
    mock_repo.latest_successful_reconciliation.return_value = None
    service = PositionExitService(mock_repo, mock_client)
    with pytest.raises(ValueError, match="Reconciliation missing or stale"):
        service.preview_close(settings=Settings(), market_id="market-1", outcome="YES")


def test_preview_close_no_bids(mock_repo, mock_client):
    def mock_get_token_order_book_no_bids(token_id):
        return MarketSnapshot(
            market_id="token_book",
            best_bid=None,
            best_ask=Decimal("0.6"),
            midpoint=None,
            spread=None,
            liquidity=Decimal("1000"),
            fetched_at=datetime.now(timezone.utc),
        ), {}

    mock_client.get_token_order_book.side_effect = mock_get_token_order_book_no_bids

    service = PositionExitService(mock_repo, mock_client)
    with pytest.raises(ValueError, match="No bids on the order book"):
        service.preview_close(settings=Settings(), market_id="market-1", outcome="YES")


def test_close_live_wrong_confirm_zero_mutation(mock_repo, mock_client, live_settings):
    service = PositionExitService(mock_repo, mock_client)
    with pytest.raises(ValueError, match="Confirm phrase mismatch"):
        service.close_live(
            settings=live_settings,
            market_id="market-1",
            outcome="YES",
            price=Decimal("0.50"),
            size=Decimal("10"),
            size_text="10",
            max_slippage=Decimal("0.05"),
            confirm="SELL market-1 YES 99",
            compliance_service=_allowed_compliance(),
        )
    mock_client.place_sell_limit_order.assert_not_called()
    mock_client.place_limit_order.assert_not_called()
    mock_client.get_token_order_book.assert_not_called()
    mock_repo.save_order_intent_once.assert_not_called()


def test_close_live_oversell_blocked(mock_repo, mock_client, live_settings):
    service = PositionExitService(mock_repo, mock_client)
    with pytest.raises(ValueError, match="oversell blocked"):
        service.close_live(
            settings=live_settings,
            market_id="market-1",
            outcome="YES",
            price=Decimal("0.50"),
            size=Decimal("101"),
            size_text="101",
            max_slippage=Decimal("0.05"),
            confirm="SELL market-1 YES 101",
            compliance_service=_allowed_compliance(),
        )
    mock_client.place_sell_limit_order.assert_not_called()


def test_close_live_stale_reconciliation_blocked(mock_repo, mock_client, live_settings):
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    mock_repo.latest_successful_reconciliation.return_value = {"created_at": stale_time.isoformat()}
    service = PositionExitService(mock_repo, mock_client)
    with pytest.raises(ValueError, match="Reconciliation missing or stale"):
        service.close_live(
            settings=live_settings,
            market_id="market-1",
            outcome="YES",
            price=Decimal("0.50"),
            size=Decimal("10"),
            size_text="10",
            max_slippage=Decimal("0.05"),
            confirm="SELL market-1 YES 10",
            compliance_service=_allowed_compliance(),
        )
    mock_client.place_sell_limit_order.assert_not_called()


def test_close_live_stale_quote_blocked(mock_repo, mock_client, live_settings):
    def stale_book(token_id):
        return MarketSnapshot(
            market_id="token_book",
            best_bid=Decimal("0.5"),
            best_ask=Decimal("0.6"),
            midpoint=Decimal("0.55"),
            spread=Decimal("0.1"),
            liquidity=Decimal("1000"),
            fetched_at=datetime.now(timezone.utc) - timedelta(seconds=600),
        ), {}

    mock_client.get_token_order_book.side_effect = stale_book
    service = PositionExitService(mock_repo, mock_client)
    with pytest.raises(ValueError, match="Order book quote is stale"):
        service.close_live(
            settings=live_settings,
            market_id="market-1",
            outcome="YES",
            price=Decimal("0.50"),
            size=Decimal("10"),
            size_text="10",
            max_slippage=Decimal("0.05"),
            confirm="SELL market-1 YES 10",
            compliance_service=_allowed_compliance(),
        )
    mock_client.place_sell_limit_order.assert_not_called()


def test_close_live_slippage_blocked(mock_repo, mock_client, live_settings):
    service = PositionExitService(mock_repo, mock_client)
    # best_bid=0.5, price=0.40 => slippage 0.10 > max 0.05
    with pytest.raises(ValueError, match="exceeding max-slippage"):
        service.close_live(
            settings=live_settings,
            market_id="market-1",
            outcome="YES",
            price=Decimal("0.40"),
            size=Decimal("10"),
            size_text="10",
            max_slippage=Decimal("0.05"),
            confirm="SELL market-1 YES 10",
            compliance_service=_allowed_compliance(),
        )
    mock_client.place_sell_limit_order.assert_not_called()


def test_close_live_duplicate_sell_blocked(mock_repo, mock_client, live_settings):
    mock_repo.active_live_order_intent.return_value = {"id": 7, "side": "sell_yes"}
    service = PositionExitService(mock_repo, mock_client)
    with pytest.raises(ValueError, match="Duplicate active SELL blocked"):
        service.close_live(
            settings=live_settings,
            market_id="market-1",
            outcome="YES",
            price=Decimal("0.50"),
            size=Decimal("10"),
            size_text="10",
            max_slippage=Decimal("0.05"),
            confirm="SELL market-1 YES 10",
            compliance_service=_allowed_compliance(),
        )
    mock_client.place_sell_limit_order.assert_not_called()


def test_close_live_same_reconciliation_attempt_is_idempotent(
    mock_repo, mock_client, live_settings
):
    mock_repo.save_order_intent_once.return_value = (42, False)
    service = PositionExitService(mock_repo, mock_client)

    with pytest.raises(ValueError, match="Duplicate SELL attempt blocked"):
        service.close_live(
            settings=live_settings,
            market_id="market-1",
            outcome="YES",
            price=Decimal("0.50"),
            size=Decimal("10"),
            size_text="10",
            max_slippage=Decimal("0.05"),
            confirm="SELL market-1 YES 10",
            compliance_service=_allowed_compliance(),
        )

    mock_client.place_sell_limit_order.assert_not_called()


def test_close_live_sdk_exception_records_failed_attempt(mock_repo, mock_client, live_settings):
    mock_client.place_sell_limit_order.side_effect = RuntimeError("sdk down")
    service = PositionExitService(mock_repo, mock_client)
    result = service.close_live(
        settings=live_settings,
        market_id="market-1",
        outcome="YES",
        price=Decimal("0.50"),
        size=Decimal("10"),
        size_text="10",
        max_slippage=Decimal("0.05"),
        confirm="SELL market-1 YES 10",
        compliance_service=_allowed_compliance(),
    )
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "sdk down" in result["error"]
    mock_repo.save_order_intent_once.assert_called_once()
    attempt = mock_repo.save_order_attempt.call_args.args[0]
    assert attempt.status == "failed"
    assert attempt.error == "sdk down"
    mock_repo.update_order_intent_status.assert_called_with(42, "failed")
    mock_client.place_limit_order.assert_not_called()


def test_close_live_success_records_order_and_reconcile(mock_repo, mock_client, live_settings):
    mock_client.place_sell_limit_order.side_effect = None
    mock_client.place_sell_limit_order.return_value = {
        "ok": True,
        "order_id": "order-sell-1",
        "status": "live",
    }
    service = PositionExitService(mock_repo, mock_client)
    result = service.close_live(
        settings=live_settings,
        market_id="market-1",
        outcome="YES",
        price=Decimal("0.49"),
        size=Decimal("10"),
        size_text="10",
        max_slippage=Decimal("0.05"),
        confirm="SELL market-1 YES 10",
        compliance_service=_allowed_compliance(),
    )
    assert result["ok"] is True
    assert result["verified"] is True
    assert result["order_id"] == "order-sell-1"
    assert result["intent_id"] == 42
    mock_client.place_sell_limit_order.assert_called_once_with(
        token_id="yes-token", price="0.49", size="10"
    )
    mock_client.place_limit_order.assert_not_called()
    mock_client.get_order.assert_called_once_with("order-sell-1")

    attempt_statuses = [call.args[0].status for call in mock_repo.save_order_attempt.call_args_list]
    assert attempt_statuses == ["submitted", "checked", "reconciled"]
    intent = mock_repo.save_order_intent_once.call_args.args[0]
    assert intent.side == "sell_yes"
    assert intent.idempotency_key == exit_order_idempotency_key(
        market_id="market-1",
        outcome="YES",
        token_id="yes-token",
        reconciliation_id="17",
    )
    assert intent.dry_run is False


def test_close_live_get_order_failure_keeps_submitted_audit(mock_repo, mock_client, live_settings):
    """P0: post-submit get_order errors must not erase submitted intent/attempt."""
    mock_client.place_sell_limit_order.side_effect = None
    mock_client.place_sell_limit_order.return_value = {
        "ok": True,
        "order_id": "order-sell-1",
        "status": "live",
    }
    mock_client.get_order.side_effect = RuntimeError("get_order network down")
    persisted: list[int] = []

    service = PositionExitService(mock_repo, mock_client)
    result = service.close_live(
        settings=live_settings,
        market_id="market-1",
        outcome="YES",
        price=Decimal("0.49"),
        size=Decimal("10"),
        size_text="10",
        max_slippage=Decimal("0.05"),
        confirm="SELL market-1 YES 10",
        compliance_service=_allowed_compliance(),
        on_submitted=persisted.append,
    )

    assert result["ok"] is True
    assert result["verified"] is False
    assert result["status"] == "submitted_unverified"
    assert result["order_id"] == "order-sell-1"
    assert "get_order failed" in (result.get("warning") or "")
    assert persisted == [42]
    mock_repo.save_order_intent_once.assert_called_once()
    attempt_statuses = [call.args[0].status for call in mock_repo.save_order_attempt.call_args_list]
    assert attempt_statuses == ["submitted", "check_failed"]
    # Final status update must retain durable active exit status for idempotency.
    mock_repo.update_order_intent_status.assert_any_call(42, "submitted")
    mock_repo.update_order_intent_status.assert_called_with(42, "submitted_unverified")
    # Must not raise / must not call BUY path.
    mock_client.place_limit_order.assert_not_called()
    mock_client.place_sell_limit_order.assert_called_once()


def test_close_live_reconcile_adapter_error_is_not_reported_success(
    mock_repo, mock_client, live_settings
):
    """P1: adapter-error reconcile must yield reconcile_failed, not verified success."""
    mock_client.place_sell_limit_order.side_effect = None
    mock_client.place_sell_limit_order.return_value = {
        "ok": True,
        "order_id": "order-sell-1",
        "status": "live",
    }
    mock_client.get_balances.side_effect = RuntimeError("balances api down")

    service = PositionExitService(mock_repo, mock_client)
    result = service.close_live(
        settings=live_settings,
        market_id="market-1",
        outcome="YES",
        price=Decimal("0.49"),
        size=Decimal("10"),
        size_text="10",
        max_slippage=Decimal("0.05"),
        confirm="SELL market-1 YES 10",
        compliance_service=_allowed_compliance(),
    )

    assert result["ok"] is True
    assert result["verified"] is False
    assert result["status"] == "reconcile_failed"
    assert result["order_id"] == "order-sell-1"
    assert "reconciliation status" in (result.get("warning") or "").lower() or (
        "do not re-submit" in (result.get("warning") or "").lower()
    )
    attempt_statuses = [call.args[0].status for call in mock_repo.save_order_attempt.call_args_list]
    assert attempt_statuses == ["submitted", "checked", "reconcile_failed"]
    mock_repo.update_order_intent_status.assert_called_with(42, "reconcile_failed")


def test_close_live_trading_disabled(mock_repo, mock_client, tmp_path):
    settings = Settings(
        DATABASE_PATH=tmp_path / "exit.db",
        POLYMARKET_PRIVATE_KEY="test-key",
        POLYMARKET_FUNDER="0xfunder",
        TRADING_DISABLED=True,
        COMPLIANCE_CHECK_ENABLED=False,
    )
    service = PositionExitService(mock_repo, mock_client)
    with pytest.raises(ValueError, match="TRADING_DISABLED"):
        service.close_live(
            settings=settings,
            market_id="market-1",
            outcome="YES",
            price=Decimal("0.50"),
            size=Decimal("10"),
            size_text="10",
            max_slippage=Decimal("0.05"),
            confirm="SELL market-1 YES 10",
            compliance_service=_allowed_compliance(),
        )
    mock_client.place_sell_limit_order.assert_not_called()


def test_close_live_circuit_breaker(mock_repo, mock_client, live_settings):
    mock_repo.get_circuit_breaker_state.return_value = {
        "circuit_breaker_tripped": 1,
        "circuit_breaker_reason": "resolution mismatch",
        "tripped_at": datetime.now(timezone.utc).isoformat(),
    }
    service = PositionExitService(mock_repo, mock_client)
    with pytest.raises(ValueError, match="Circuit breaker tripped"):
        service.close_live(
            settings=live_settings,
            market_id="market-1",
            outcome="YES",
            price=Decimal("0.50"),
            size=Decimal("10"),
            size_text="10",
            max_slippage=Decimal("0.05"),
            confirm="SELL market-1 YES 10",
            compliance_service=_allowed_compliance(),
        )
    mock_client.place_sell_limit_order.assert_not_called()


def test_close_confirm_phrase_helper():
    assert build_close_confirm_phrase(market_id="m1", outcome="yes", size_text="12.5") == (
        "SELL m1 YES 12.5"
    )


def test_close_live_end_to_end_with_sqlite_no_network(tmp_path):
    """Integration-style offline test with real SQLite repository."""
    db_path = tmp_path / "close-live.db"
    database = Database(db_path)
    database.init_schema()
    connection = database.connect()
    repository = Repository(connection)

    from polymarket_weather_arb.domain.markets import Market

    market = Market(
        id="market-1",
        slug="m1",
        title="Will it rain?",
        description="test",
        yes_token_id="yes-token",
        no_token_id="no-token",
        status="active",
        is_weather=True,
    )
    repository.upsert_market(
        market,
        {
            "id": "market-1",
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["yes-token", "no-token"],
        },
    )
    repository.replace_positions(
        [
            {
                "market": "market-1",
                "asset": "yes-token",
                "outcome": "Yes",
                "size": "50",
                "avgPrice": "0.4",
            }
        ]
    )
    repository.save_reconciliation(
        "ok",
        {"status": "ok"},
    )

    client = Mock()
    client.place_limit_order.side_effect = RuntimeError("BUY path forbidden")
    client.place_sell_limit_order.return_value = {
        "order_id": "sell-99",
        "status": "LIVE",
    }
    client.get_token_order_book.return_value = (
        MarketSnapshot(
            market_id="token_book",
            best_bid=Decimal("0.55"),
            best_ask=Decimal("0.60"),
            midpoint=Decimal("0.575"),
            spread=Decimal("0.05"),
            liquidity=Decimal("100"),
            fetched_at=datetime.now(timezone.utc),
        ),
        {},
    )
    client.get_order.return_value = {"id": "sell-99", "status": "LIVE"}
    client.get_balances.return_value = {"balance": 1}
    client.get_orders.return_value = []
    client.get_trades.return_value = []
    client.get_positions.return_value = []

    settings = Settings(
        DATABASE_PATH=db_path,
        POLYMARKET_PRIVATE_KEY="k",
        POLYMARKET_FUNDER="0x1",
        TRADING_DISABLED=False,
        COMPLIANCE_CHECK_ENABLED=False,
    )
    service = PositionExitService(repository, client)
    result = service.close_live(
        settings=settings,
        market_id="market-1",
        outcome="YES",
        price=Decimal("0.54"),
        size=Decimal("20"),
        size_text="20",
        max_slippage=Decimal("0.05"),
        confirm="SELL market-1 YES 20",
        compliance_service=_allowed_compliance(),
    )
    connection.commit()

    assert result["ok"] is True
    assert result["order_id"] == "sell-99"
    intents = repository.list_recent_order_intents(limit=5, market_id="market-1")
    assert len(intents) == 1
    assert intents[0]["side"] == "sell_yes"
    assert intents[0]["dry_run"] == 0
    attempts = connection.execute(
        "SELECT status FROM order_attempts WHERE intent_id = ? ORDER BY id",
        (result["intent_id"],),
    ).fetchall()
    assert [row["status"] for row in attempts] == ["submitted", "checked", "reconciled"]
    client.place_sell_limit_order.assert_called_once()
    client.place_limit_order.assert_not_called()
    connection.close()
