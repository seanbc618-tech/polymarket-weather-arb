from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest

from polymarket_weather_arb.services.reconciliation_service import ReconciliationService
from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


class ReconciledClient:
    def get_balances(self):
        return {"usdc": "10"}

    def get_orders(self):
        return [
            {
                "id": "order-1",
                "market": "m1",
                "asset_id": "yes-token",
                "side": "BUY",
                "price": "0.25",
                "size": "10",
                "status": "live",
            }
        ]

    def get_trades(self):
        return [
            {
                "id": "trade-1",
                "order_id": "order-1",
                "market": "m1",
                "side": "BUY",
                "price": "0.20",
                "size": "5",
                "fee": "0",
                "timestamp": "2026-05-06T00:00:00+00:00",
            }
        ]

    def get_positions(self):
        return [{"market": "m1", "outcome": "Yes", "size": "5", "notional": "1.25"}]


class PendingClient:
    def get_balances(self):
        return {"status": "not_implemented"}

    def get_orders(self):
        return []

    def get_trades(self):
        return []

    def get_positions(self):
        return []


class PartialClient:
    def get_balances(self):
        return {"usdc": "10"}

    def get_orders(self):
        return []

    def get_trades(self):
        return []

    def get_positions(self):
        raise NotImplementedError("position reconciliation adapter pending")


class ErrorClient:
    def get_balances(self):
        raise ValueError("missing credentials")

    def get_orders(self):
        return []

    def get_trades(self):
        return []

    def get_positions(self):
        return []


class ProductionShapeClient:
    def get_balances(self):
        return {"balance": 64351839}

    def get_orders(self):
        return [
            {
                "id": "open-1",
                "market": "0xcondition",
                "token_id": "yes-token",
                "side": "BUY",
                "price": "0.12",
                "size": "5",
                "status": "live",
            }
        ]

    def get_trades(self):
        return [
            {
                "id": "trade-real-1",
                "market": "0xcondition",
                "token_id": "yes-token",
                "taker_order_id": "order-real-1",
                "transaction_hash": "0xtx",
                "side": "BUY",
                "price": "0.13",
                "size": "5",
                "matched_at": "2026-07-10T10:00:00Z",
            }
        ]

    def get_positions(self):
        return [
            {
                "condition_id": "0xcondition",
                "token_id": "yes-token",
                "outcome": "Yes",
                "size": "5",
                "avg_price": "0.13",
                "current_value": "0.60",
            }
        ]


class MakerPerspectiveClient:
    def get_balances(self):
        return {"balance": "1000000"}

    def get_orders(self):
        return [
            {
                "id": "our-order",
                "market": "0xcondition",
                "asset_id": "yes-token",
                "side": "BUY",
                "price": "0.01",
                "original_size": "100",
                "size_matched": "40",
                "status": "LIVE",
            }
        ]

    def get_trades(self):
        return [
            {
                "id": "maker-trade",
                "market": "0xcondition",
                "side": "BUY",
                "price": "0.99",
                "size": "5",
                "outcome": "No",
                "token_id": "no-token",
                "taker_order_id": "counterparty-order",
                "trader_side": "MAKER",
                "maker_orders": [
                    {
                        "order_id": "our-order",
                        "side": "BUY",
                        "price": "0.01",
                        "matched_amount": "5",
                        "outcome": "Yes",
                        "token_id": "yes-token",
                    }
                ],
                "matched_at": "2026-07-11T08:19:50Z",
            }
        ]

    def get_positions(self):
        return [
            {
                "condition_id": "0xcondition",
                "token_id": "yes-token",
                "outcome": "Yes",
                "size": "5",
                "current_value": "0.05",
            }
        ]


class MissingHistoricalMarketClient:
    def get_balances(self):
        return {"balance": "1000000"}

    def get_orders(self):
        return []

    def get_trades(self):
        return [
            {
                "id": "historical-fill",
                "market": "0xmissing-condition",
                "token_id": "historical-yes",
                "taker_order_id": "historical-order",
                "side": "BUY",
                "price": "0.13",
                "size": "5",
            }
        ]

    def get_positions(self):
        return []

    def find_markets_by_condition_ids(self, condition_ids):
        assert condition_ids == ["0xmissing-condition"]
        market = Market(
            id="historical-market",
            title="Historical weather market",
            slug="historical-weather-market",
            description="Historical settlement description",
            yes_token_id="historical-yes",
            no_token_id="historical-no",
            is_weather=True,
        )
        return [(market, {"id": market.id, "conditionId": "0xmissing-condition"})]


class PendingBroadcastTradeClient:
    def get_balances(self):
        return {"balance": "1000000"}

    def get_orders(self):
        return []

    def get_trades(self):
        return [
            {
                "id": "pending-broadcast-trade",
                "market": "m1",
                "token_id": "yes-token",
                "order_id": "pending-order",
                "side": "BUY",
                "price": "0.18",
                "size": "5.56",
                "status": "MATCHED_NOT_BROADCASTED",
                "transaction_hash": None,
            }
        ]

    def get_positions(self):
        return []


def test_reconcile_persists_successful_status(tmp_path):
    repo, connection = _repo(tmp_path)
    try:
        _seed_market(repo)
        result = ReconciliationService(ReconciledClient(), repo).reconcile()
        connection.commit()

        assert result["status"] == "ok"
        assert result["positions_stored"] == 1
        assert result["orders_stored"] == 1
        assert result["fills_stored"] == 1
        assert repo.market_exposure("m1") == 1.25
        assert repo.latest_successful_reconciliation() is not None
        assert repo.list_open_orders()[0]["exchange_order_id"] == "order-1"
        assert repo.list_fills()[0]["exchange_fill_id"] == "trade-1"
        assert repo.list_positions(nonzero_only=True)[0]["market_id"] == "m1"
        assert repo.nonzero_positions_count() == 1
    finally:
        connection.close()


def test_reconcile_pending_status_does_not_count_as_success(tmp_path):
    repo, connection = _repo(tmp_path)
    try:
        result = ReconciliationService(PendingClient(), repo).reconcile()
        connection.commit()

        assert result["status"] == "adapter-pending"
        assert repo.latest_successful_reconciliation() is None
    finally:
        connection.close()


def test_reconcile_partial_adapter_does_not_count_as_success(tmp_path):
    repo, connection = _repo(tmp_path)
    try:
        result = ReconciliationService(PartialClient(), repo).reconcile()
        connection.commit()

        assert result["status"] == "adapter-pending"
        assert "position reconciliation adapter pending" in result["error"]
        assert result["failed_stage"] == "positions"
        assert repo.latest_successful_reconciliation() is None
    finally:
        connection.close()


def test_reconcile_persists_adapter_errors(tmp_path):
    repo, connection = _repo(tmp_path)
    try:
        result = ReconciliationService(ErrorClient(), repo).reconcile()
        connection.commit()

        assert result["status"] == "adapter-error"
        assert "missing credentials" in str(result["error"])
        assert result["failed_stage"] == "balances"
        assert result["error_type"] == "ValueError"
        assert repo.latest_successful_reconciliation() is None
    finally:
        connection.close()


def test_reconcile_maps_data_api_condition_and_token_ids_to_gamma_market(tmp_path):
    repo, connection = _repo(tmp_path)
    try:
        _seed_market(
            repo,
            raw_payload={"id": "m1", "conditionId": "0xcondition"},
        )

        result = ReconciliationService(ProductionShapeClient(), repo).reconcile()
        connection.commit()

        assert result["status"] == "ok"
        assert result["positions_stored"] == 1
        assert result["orders_stored"] == 1
        assert result["fills_stored"] == 1
        assert repo.list_positions(nonzero_only=True)[0]["market_id"] == "m1"
        assert repo.list_open_orders()[0]["market_id"] == "m1"
        fill = repo.list_fills()[0]
        assert fill["market_id"] == "m1"
        assert fill["order_id"] == "order-real-1"
        assert fill["filled_at"] == "2026-07-10T10:00:00Z"
    finally:
        connection.close()


def test_reconcile_uses_our_maker_leg_and_remaining_open_size(tmp_path):
    repo, connection = _repo(tmp_path)
    try:
        _seed_market(repo, raw_payload={"id": "m1", "conditionId": "0xcondition"})
        intent_id = repo.save_order_intent(
            SimpleNamespace(
                market_id="m1",
                side="buy_yes",
                token_id="yes-token",
                limit_price=0.01,
                size=100,
                notional=1,
                rationale="incident regression",
                dry_run=False,
                status="submitted",
                idempotency_key="maker-regression",
                created_at=datetime.now(timezone.utc),
            )
        )
        repo.save_order_attempt(
            SimpleNamespace(
                intent_id=intent_id,
                request_payload={},
                response_payload={"order_id": "our-order"},
                status="submitted",
                error=None,
                created_at=datetime.now(timezone.utc),
            )
        )

        result = ReconciliationService(MakerPerspectiveClient(), repo).reconcile()
        connection.commit()

        fill = repo.list_fills()[0]
        assert fill["order_id"] == "our-order"
        assert fill["side"] == "BUY"
        assert fill["price"] == 0.01
        assert fill["size"] == 5
        order = repo.list_open_orders()[0]
        assert order["size"] == 60
        assert order["notional"] == 0.6
        assert repo.get_order_intent(intent_id)["status"] == "partially_filled"
        assert result["order_intents_updated"] == 1
    finally:
        connection.close()


def test_reconcile_ignores_and_self_heals_failed_maker_trade(tmp_path):
    repo, connection = _repo(tmp_path)
    try:
        _seed_market(repo, raw_payload={"id": "m1", "conditionId": "0xcondition"})
        intent_id = repo.save_order_intent(
            SimpleNamespace(
                market_id="m1",
                side="buy_yes",
                token_id="yes-token",
                limit_price=0.04,
                size=12.9,
                notional=0.516,
                rationale="failed trade regression",
                dry_run=False,
                status="filled",
                idempotency_key="failed-trade-regression",
                created_at=datetime.now(timezone.utc),
            )
        )
        repo.save_order_attempt(
            SimpleNamespace(
                intent_id=intent_id,
                request_payload={},
                response_payload={"order_id": "our-order"},
                status="submitted",
                error=None,
                created_at=datetime.now(timezone.utc),
            )
        )
        failed_trade = {
            "id": "failed-maker-trade",
            "market": "0xcondition",
            "side": "SELL",
            "price": "0.04",
            "size": "12.9",
            "status": "FAILED",
            "maker_orders": [
                {
                    "order_id": "our-order",
                    "side": "BUY",
                    "price": "0.04",
                    "matched_amount": "12.9",
                    "token_id": "yes-token",
                }
            ],
        }
        connection.execute(
            """
            INSERT INTO fills (
                exchange_fill_id, order_id, market_id, side, price, size, fee,
                raw_payload, filled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "failed-maker-trade",
                "our-order",
                "m1",
                "BUY",
                0.04,
                12.9,
                0,
                '{"status":"FAILED"}',
                "2026-07-15T16:06:41Z",
            ),
        )

        touched, newly_inserted = repo.save_reconciled_fills([failed_trade])

        assert touched == 0
        assert newly_inserted == []
        assert repo.list_fills(market_id="m1") == []
    finally:
        connection.close()


def test_reconcile_pending_broadcast_trade_is_not_a_confirmed_fill_or_cancelled(
    tmp_path,
):
    repo, connection = _repo(tmp_path)
    try:
        _seed_market(repo)
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        intent_id = repo.save_order_intent(
            SimpleNamespace(
                market_id="m1",
                side="buy_yes",
                token_id="yes-token",
                limit_price=0.18,
                size=5.56,
                notional=1.0008,
                rationale="pending transaction hash regression",
                dry_run=False,
                status="submitted",
                idempotency_key="pending-broadcast",
                created_at=old,
            )
        )
        repo.save_order_attempt(
            SimpleNamespace(
                intent_id=intent_id,
                request_payload={},
                response_payload={
                    "order_id": "pending-order",
                    "status": "matched",
                    "making_amount": "1.0008",
                    "taking_amount": "5.56",
                },
                status="submitted",
                error=None,
                created_at=old,
            )
        )

        result = ReconciliationService(PendingBroadcastTradeClient(), repo).reconcile()

        assert result["status"] == "ok"
        assert result["trades_count"] == 1
        assert result["fills_stored"] == 0
        assert result["new_fills"] == []
        assert repo.list_fills(market_id="m1") == []
        assert repo.get_order_intent(intent_id)["status"] == "submitted"
    finally:
        connection.close()


def test_reconcile_terminalizes_old_missing_unfilled_order(tmp_path):
    repo, connection = _repo(tmp_path)
    try:
        _seed_market(repo)
        intent_id = repo.save_order_intent(
            SimpleNamespace(
                market_id="m1",
                side="buy_yes",
                token_id="yes-token",
                limit_price=0.1,
                size=20,
                notional=2,
                rationale="accepted but later absent",
                dry_run=False,
                status="open",
                idempotency_key="missing-order",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            )
        )
        repo.save_order_attempt(
            SimpleNamespace(
                intent_id=intent_id,
                request_payload={},
                response_payload={"order_id": "missing-order-id"},
                status="submitted",
                error=None,
                created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            )
        )

        result = ReconciliationService(PendingClient(), repo).reconcile()

        assert result["order_intents_updated"] == 1
        assert repo.get_order_intent(intent_id)["status"] == "cancelled"
    finally:
        connection.close()


def test_reconcile_closes_old_missing_partially_filled_order(tmp_path):
    repo, connection = _repo(tmp_path)
    try:
        _seed_market(repo)
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        intent_id = repo.save_order_intent(
            SimpleNamespace(
                market_id="m1",
                side="sell_yes",
                token_id="yes-token",
                limit_price=0.2,
                size=10,
                notional=2,
                rationale="partial then remainder disappeared",
                dry_run=False,
                status="partially_filled",
                idempotency_key="partial-closed",
                created_at=old,
            )
        )
        repo.save_order_attempt(
            SimpleNamespace(
                intent_id=intent_id,
                request_payload={},
                response_payload={"order_id": "partial-order-id"},
                status="submitted",
                error=None,
                created_at=old,
            )
        )
        repo.save_reconciled_fills(
            [
                {
                    "id": "partial-fill-id",
                    "order_id": "partial-order-id",
                    "market": "m1",
                    "side": "SELL",
                    "price": "0.2",
                    "size": "4",
                    "timestamp": old.isoformat(),
                }
            ]
        )

        result = ReconciliationService(PendingClient(), repo).reconcile()

        assert result["order_intents_updated"] == 1
        assert repo.get_order_intent(intent_id)["status"] == "partially_filled_closed"
        assert repo.active_live_order_intent("m1", "sell_yes") is None
    finally:
        connection.close()


def test_reconcile_marks_exchange_quantized_matched_order_filled(tmp_path):
    repo, connection = _repo(tmp_path)
    try:
        _seed_market(repo)
        intent_id = repo.save_order_intent(
            SimpleNamespace(
                market_id="m1",
                side="buy_yes",
                token_id="yes-token",
                limit_price=0.26,
                size=7.4178,
                notional=1.928628,
                rationale="exchange size quantization regression",
                dry_run=False,
                status="partially_filled_closed",
                idempotency_key="matched-quantized-size",
                created_at=datetime.now(timezone.utc),
            )
        )
        repo.save_order_attempt(
            SimpleNamespace(
                intent_id=intent_id,
                request_payload={},
                response_payload={
                    "order_id": "matched-order-id",
                    "status": "matched",
                    "making_amount": "1.9266",
                    "taking_amount": "7.41",
                },
                status="submitted",
                error=None,
                created_at=datetime.now(timezone.utc),
            )
        )
        repo.save_reconciled_fills(
            [
                {
                    "id": "matched-fill-id",
                    "order_id": "matched-order-id",
                    "market": "m1",
                    "side": "BUY",
                    "price": "0.26",
                    "size": "7.41",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ]
        )

        result = ReconciliationService(PendingClient(), repo).reconcile()

        assert result["order_intents_updated"] == 1
        assert repo.get_order_intent(intent_id)["status"] == "filled"
        assert repo.active_live_order_intent("m1", "buy_yes") is None
    finally:
        connection.close()


def test_reconcile_keeps_large_terminal_matched_remainder_partial(tmp_path):
    repo, connection = _repo(tmp_path)
    try:
        _seed_market(repo)
        intent_id = repo.save_order_intent(
            SimpleNamespace(
                market_id="m1",
                side="buy_yes",
                token_id="yes-token",
                limit_price=0.17,
                size=22.5918,
                notional=3.840606,
                rationale="terminal partial fill regression",
                dry_run=False,
                # Simulate the old self-heal incorrectly labeling this as full.
                status="filled",
                idempotency_key="matched-partial-size",
                created_at=datetime.now(timezone.utc),
            )
        )
        repo.save_order_attempt(
            SimpleNamespace(
                intent_id=intent_id,
                request_payload={},
                response_payload={
                    "order_id": "partial-matched-order-id",
                    "status": "matched",
                    "making_amount": "1.7748",
                    "taking_amount": "10.44",
                },
                status="submitted",
                error=None,
                created_at=datetime.now(timezone.utc),
            )
        )
        repo.save_reconciled_fills(
            [
                {
                    "id": "partial-matched-fill-id",
                    "order_id": "partial-matched-order-id",
                    "market": "m1",
                    "side": "BUY",
                    "price": "0.17",
                    "size": "10.44",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ]
        )

        result = ReconciliationService(PendingClient(), repo).reconcile()

        assert result["order_intents_updated"] == 1
        assert repo.get_order_intent(intent_id)["status"] == "partially_filled_closed"
        progress = repo.list_recent_order_intents(limit=1)[0]
        assert progress["filled_size"] == pytest.approx(10.44)
        assert progress["size"] == pytest.approx(22.5918)
        assert repo.active_live_order_intent("m1", "buy_yes") is None
    finally:
        connection.close()


def test_reconcile_recovers_missing_historical_market_before_saving_fill(tmp_path):
    repo, connection = _repo(tmp_path)
    try:
        result = ReconciliationService(MissingHistoricalMarketClient(), repo).reconcile()
        connection.commit()

        assert result["markets_recovered"] == 1
        assert result["fills_stored"] == 1
        assert repo.get_market("historical-market") is not None
        fill = repo.list_fills()[0]
        assert fill["market_id"] == "historical-market"
        assert fill["exchange_fill_id"] == "historical-fill"
    finally:
        connection.close()


def test_strategy_override_effective_lookup_prefers_exact_match(tmp_path):
    repo, connection = _repo(tmp_path)
    try:
        repo.upsert_strategy_override(market_id="*", profile="*", min_edge="0.06")
        repo.upsert_strategy_override(market_id="m1", profile="*", min_edge="0.07")
        repo.upsert_strategy_override(market_id="*", profile="micro-live", min_edge="0.08")
        repo.upsert_strategy_override(
            market_id="m1", profile="micro-live", min_edge="0.09", live_auto_enabled=True
        )

        override = repo.effective_strategy_override("m1", "micro-live")

        assert override["market_id"] == "m1"
        assert override["profile"] == "micro-live"
        assert override["min_edge"] == 0.09
        assert override["live_auto_enabled"] == 1
        assert len(repo.list_strategy_overrides()) == 4
        assert repo.delete_strategy_override("m1", "micro-live") is True
        assert repo.effective_strategy_override("m1", "micro-live")["market_id"] == "m1"
    finally:
        connection.close()


def _repo(tmp_path):
    database = Database(tmp_path / "test.db")
    database.init_schema()
    connection = database.connect()
    return Repository(connection), connection


def _seed_market(repo, raw_payload=None):
    repo.upsert_market(
        SimpleNamespace(
            id="m1",
            slug="m1",
            title="Test market",
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
        raw_payload or {"id": "m1"},
    )
