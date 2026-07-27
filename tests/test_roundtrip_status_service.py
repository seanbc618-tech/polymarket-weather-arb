from unittest.mock import Mock
from polymarket_weather_arb.services.roundtrip_status_service import RoundtripStatusService


def _mock_repo(
    run=None,
    intents=None,
    open_orders=None,
    positions=None,
    fills=None,
    recon_fresh=True,
    buy_order_ids=None,
    sell_order_ids=None,
):
    repo = Mock()
    repo.list_recent_order_intents.return_value = intents or []
    repo.list_open_orders.return_value = open_orders or []
    repo.list_positions.return_value = positions or []
    repo.list_fills.return_value = fills or []

    repo.get_active_roundtrip_run.return_value = run

    def mock_get_order_ids(intent_id):
        if run and intent_id == run.get("buy_intent_id"):
            return buy_order_ids or []
        if run and intent_id == run.get("sell_intent_id"):
            return sell_order_ids or []
        return []

    repo.get_order_ids_for_intent.side_effect = mock_get_order_ids

    if recon_fresh:
        from datetime import datetime, timezone

        repo.latest_successful_reconciliation.return_value = {
            "created_at": datetime.now(timezone.utc).isoformat()
        }
    else:
        from datetime import datetime, timezone, timedelta

        repo.latest_successful_reconciliation.return_value = {
            "created_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        }

    return repo


def test_roundtrip_ready_to_buy_no_run():
    repo = _mock_repo()
    service = RoundtripStatusService(repo)
    result = service.get_status("market-1")
    assert result.stage == "ready_to_buy"
    assert result.reconciliation_fresh is True


def test_roundtrip_ready_to_buy_with_run():
    repo = _mock_repo(run={"buy_intent_id": None, "sell_intent_id": None})
    service = RoundtripStatusService(repo)
    result = service.get_status("market-1")
    assert result.stage == "ready_to_buy"


def test_roundtrip_buy_open():
    repo = _mock_repo(
        run={"buy_intent_id": 1, "sell_intent_id": None},
        open_orders=[{"side": "buy_yes", "status": "open"}],
    )
    service = RoundtripStatusService(repo)
    result = service.get_status("market-1")
    assert result.stage == "buy_open"


def test_roundtrip_position_confirmed():
    repo = _mock_repo(
        run={"buy_intent_id": 1, "sell_intent_id": None},
        positions=[{"size": "100.0", "outcome": "YES"}],
        buy_order_ids=["order1"],
        fills=[{"order_id": "order1"}],
    )
    service = RoundtripStatusService(repo)
    result = service.get_status("market-1")
    assert result.stage == "position_confirmed"


def test_roundtrip_ready_to_sell():
    repo = _mock_repo(
        run={"buy_intent_id": 1, "sell_intent_id": 2},
        positions=[{"size": "100.0", "outcome": "YES"}],
        buy_order_ids=["order1"],
        sell_order_ids=["order2"],
        fills=[{"order_id": "order1"}],
    )
    service = RoundtripStatusService(repo)
    result = service.get_status("market-1")
    assert result.stage == "ready_to_sell"


def test_roundtrip_sell_open():
    repo = _mock_repo(
        run={"buy_intent_id": 1, "sell_intent_id": 2},
        open_orders=[{"side": "sell_yes", "status": "open"}],
        positions=[{"size": "100.0", "outcome": "YES"}],
        buy_order_ids=["order1"],
        fills=[{"order_id": "order1"}],
    )
    service = RoundtripStatusService(repo)
    result = service.get_status("market-1")
    assert result.stage == "sell_open"


def test_roundtrip_completed():
    repo = _mock_repo(
        run={"buy_intent_id": 1, "sell_intent_id": 2},
        positions=[{"size": "0.0", "outcome": "YES"}],
        buy_order_ids=["order1"],
        sell_order_ids=["order2"],
        fills=[
            {"order_id": "order1", "side": "buy_yes", "size": "1.0"},
            {"order_id": "order2", "side": "sell_yes", "size": "1.0"},
        ],
    )
    service = RoundtripStatusService(repo)
    result = service.get_status("market-1")
    assert result.stage == "completed"


def test_roundtrip_completed_after_terminal_partial_buy_is_fully_sold():
    repo = _mock_repo(
        run={"buy_intent_id": 1, "sell_intent_id": 2},
        intents=[
            {"id": 1, "side": "buy_yes", "status": "partially_filled_closed"},
            {"id": 2, "side": "sell_yes", "status": "filled"},
        ],
        positions=[{"size": "0.0", "outcome": "YES"}],
        buy_order_ids=["partial-buy-order"],
        sell_order_ids=["close-order"],
        fills=[
            {"order_id": "partial-buy-order", "side": "buy_yes", "size": "10.44"},
            {"order_id": "close-order", "side": "sell_yes", "size": "10.44"},
        ],
    )

    result = RoundtripStatusService(repo).get_status("market-1")

    assert result.stage == "completed"


def test_roundtrip_failed_buy():
    repo = _mock_repo(
        run={"buy_intent_id": 1, "sell_intent_id": None},
        intents=[{"id": 1, "side": "buy_yes", "status": "failed"}],
        buy_order_ids=["order1"],
        fills=[],  # no fills match order1
    )
    service = RoundtripStatusService(repo)
    result = service.get_status("market-1")
    assert result.stage == "failed"


def test_roundtrip_matched_buy_without_local_fill_is_unverified_not_failed():
    repo = _mock_repo(
        run={"buy_intent_id": 1, "sell_intent_id": None},
        intents=[{"id": 1, "side": "buy_yes", "status": "matched"}],
        buy_order_ids=["order1"],
        fills=[],
    )
    service = RoundtripStatusService(repo)

    result = service.get_status("market-1")

    assert result.stage == "buy_unverified"
    repo.update_roundtrip_run_status.assert_not_called()


def test_roundtrip_stale_reconciliation():
    repo = _mock_repo(recon_fresh=False)
    service = RoundtripStatusService(repo)
    result = service.get_status("market-1")
    assert result.stage == "ready_to_buy"
    assert result.reconciliation_fresh is False


def test_roundtrip_uppercase_compatibility():
    # Ensure open_orders with UPPERCASE side are matched
    repo = _mock_repo(
        run={"buy_intent_id": 1, "sell_intent_id": None},
        open_orders=[{"side": "BUY_YES", "status": "open"}],
    )
    service = RoundtripStatusService(repo)
    result = service.get_status("market-1")
    assert result.stage == "buy_open"
