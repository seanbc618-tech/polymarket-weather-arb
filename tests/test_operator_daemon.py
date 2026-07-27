from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.profiles import get_profile
from polymarket_weather_arb.services.operator_daemon import OperatorDaemon
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


class FakeClient:
    def __init__(self):
        self.markets = [
            Market(
                id="daemon-m1",
                slug="daemon-m1",
                title="Will the high temperature in New York exceed 80°F on May 8, 2026?",
                description="NOAA station KNYC",
                yes_token_id="yes-token",
                no_token_id="no-token",
                status="active",
                is_weather=True,
            )
        ]
        self.orders = []
        self.positions = []
        self.trades = []
        self.sell_calls = []

    def list_markets(self, limit=100, offset=0):
        return [(market, {"id": market.id}) for market in self.markets]

    def get_order_book(self, market):
        return (
            MarketSnapshot(
                market_id=market.id,
                best_bid=Decimal("0.45"),
                best_ask=Decimal("0.50"),
                midpoint=Decimal("0.475"),
                spread=Decimal("0.05"),
                liquidity=Decimal("100"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {"market": market.id},
        )

    def place_limit_order(self, *, token_id, side, price, size):
        raise AssertionError("daemon tests should not place live orders")

    def place_sell_limit_order(self, *, token_id, price, size):
        self.sell_calls.append({"token_id": token_id, "price": price, "size": size})
        raise AssertionError("default daemon tests must not place auto SELL")

    def get_token_order_book(self, token_id):
        return (
            MarketSnapshot(
                market_id="token_book",
                best_bid=Decimal("0.45"),
                best_ask=Decimal("0.50"),
                midpoint=Decimal("0.475"),
                spread=Decimal("0.05"),
                liquidity=Decimal("100"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {},
        )

    def get_balances(self):
        return {}

    def get_positions(self):
        return self.positions

    def get_orders(self):
        return self.orders

    def get_trades(self):
        return self.trades


class RecordingRunner:
    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def __call__(self, argv, *, cwd, timeout, capture_output, text):
        self.calls.append(argv)
        return SimpleNamespace(
            returncode=self.returncode, stdout="dry-run order recorded", stderr=""
        )


class RecordingWorkflow:
    def __init__(self):
        self.calls = []

    def research_market(self, market_id):
        self.calls.append(("research_market", market_id))
        return SimpleNamespace(market_id=market_id, summary="researched", details=[])

    def dry_run_trade(self, market_id):
        self.calls.append(("dry_run_trade", market_id))
        return SimpleNamespace(market_id=market_id, summary="dry run recorded", details=["ok"])


class RecordingWorkflowFactory:
    def __init__(self):
        self.workflows = []

    def __call__(self, repo):
        workflow = RecordingWorkflow()
        self.workflows.append(workflow)
        return workflow


def use_recording_workflow(daemon: OperatorDaemon) -> RecordingWorkflowFactory:
    workflow_factory = RecordingWorkflowFactory()
    daemon.automation.workflow_factory = workflow_factory
    return workflow_factory


def open_repo(tmp_path):
    database = Database(tmp_path / "daemon.db")
    database.init_schema()
    connection = database.connect()
    return database, connection, Repository(connection)


def test_daemon_discovers_proposes_and_auto_executes_dry_run(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    runner = RecordingRunner()
    try:
        daemon = OperatorDaemon(
            repository=repo,
            client=FakeClient(),
            profile=get_profile("dry-run-demo"),
            dry_run_only=True,
        )
        daemon.automation.runner = runner
        workflow_factory = use_recording_workflow(daemon)

        result = daemon.tick(risk_guard=False)
        connection.commit()

        assert result.discovered == 1
        assert result.proposed_kind == "dry_run"
        assert result.auto_executed_action_ids == [result.proposed_action_id]
        assert runner.calls == []
        assert len(workflow_factory.workflows) == 1
        assert workflow_factory.workflows[0].calls == [
            ("research_market", "daemon-m1"),
            ("dry_run_trade", "daemon-m1"),
        ]
        action = repo.get_automation_action(result.proposed_action_id)
        assert action["status"] == "executed"
        assert action["execution_duration_ms"] is not None
    finally:
        connection.close()


def test_daemon_does_not_auto_approve_live_actions(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    try:
        client = FakeClient()
        # Seed the market so a non-dry-run pending action exists without discovery.
        repo.upsert_market(client.markets[0], {"id": "daemon-m1"})
        action = repo.create_automation_action(
            SimpleNamespace(
                id="act_1234567890abcdef",
                kind="trade_live",
                market_id="daemon-m1",
                reason="manual live review",
                command_preview="trade --market daemon-m1",
                idempotency_key=None,
                requested_by="test",
                created_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc).replace(year=2099),
            )
        )
        daemon = OperatorDaemon(
            repository=repo,
            client=client,
            profile=get_profile("balanced"),
            dry_run_only=False,
        )

        result = daemon.tick(discover=False, propose=False, auto_dry_run=True, risk_guard=False)
        connection.commit()

        assert result.auto_executed_action_ids == []
        assert result.skipped_live_action_ids == [action["id"]]
        assert repo.get_automation_action(action["id"])["status"] == "pending"
    finally:
        connection.close()


def test_daemon_run_respects_max_ticks(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    try:
        daemon = OperatorDaemon(
            repository=repo,
            client=FakeClient(),
            profile=get_profile("dry-run-demo"),
            dry_run_only=True,
        )

        results = daemon.run(tick_seconds=5, max_ticks=1, sleep=lambda seconds: None)

        assert len(results) == 1
    finally:
        connection.close()


def test_daemon_risk_guard_reports_missing_reconciliation(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    notifications = []
    try:
        daemon = OperatorDaemon(
            repository=repo,
            client=FakeClient(),
            profile=get_profile("dry-run-demo"),
            dry_run_only=True,
            notifier=notifications.append,
        )

        result = daemon.tick(discover=False, propose=False, auto_dry_run=False)

        assert result.risk_status == "warn"
        assert "no successful reconciliation" in result.risk_anomalies
        assert result.notifications_sent == ["daemon_risk", "daemon_tick"]
        assert notifications[0]["role"] == "risk"
        assert notifications[1]["role"] == "reviewer"
    finally:
        connection.close()


def test_daemon_live_auto_requires_micro_live_profile_and_whitelist(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    try:
        client = FakeClient()
        repo.upsert_market(client.markets[0], {"id": "daemon-m1"})
        action = repo.create_automation_action(
            SimpleNamespace(
                id="act_abcdefabcdef1234",
                kind="trade_live",
                market_id="daemon-m1",
                reason="micro live review",
                command_preview="trade --market daemon-m1",
                idempotency_key=None,
                requested_by="test",
                created_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc).replace(year=2099),
            )
        )
        daemon = OperatorDaemon(
            repository=repo,
            client=client,
            profile=get_profile("micro-live"),
            settings=Settings(TRADING_DISABLED=False, COMPLIANCE_CHECK_ENABLED=False),
            dry_run_only=False,
            allow_live_auto=True,
            live_market_ids={"other-market"},
        )

        result = daemon.tick(
            discover=False, propose=False, auto_dry_run=False, risk_guard=False, auto_live=True
        )

        assert result.auto_live_executed_action_ids == []
        assert result.skipped_live_action_ids == [action["id"]]
        assert repo.get_automation_action(action["id"])["status"] == "pending"
    finally:
        connection.close()


def test_daemon_auto_live_requires_enabled_risk_guard_even_if_reconciliation_gate_is_disabled(
    tmp_path,
):
    _, connection, repo = open_repo(tmp_path)
    try:
        client = FakeClient()
        repo.upsert_market(client.markets[0], {"id": "daemon-m1"})
        action = repo.create_automation_action(
            SimpleNamespace(
                id="act_bbbbbbbbbbbbbbbb",
                kind="trade_live",
                market_id="daemon-m1",
                reason="micro live review",
                command_preview="trade --market daemon-m1",
                idempotency_key=None,
                requested_by="test",
                created_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc).replace(year=2099),
            )
        )
        daemon = OperatorDaemon(
            repository=repo,
            client=client,
            profile=get_profile("micro-live"),
            settings=Settings(TRADING_DISABLED=False, COMPLIANCE_CHECK_ENABLED=False),
            dry_run_only=False,
            allow_live_auto=True,
            live_market_ids={"daemon-m1"},
            require_fresh_reconciliation=False,
        )
        daemon.automation.runner = RecordingRunner()

        result = daemon.tick(
            discover=False, propose=False, auto_dry_run=False, risk_guard=False, auto_live=True
        )

        assert result.auto_live_executed_action_ids == []
        assert result.skipped_live_action_ids == [action["id"]]
        assert repo.get_automation_action(action["id"])["status"] == "pending"
    finally:
        connection.close()


def test_daemon_auto_live_executes_only_with_micro_live_whitelist_and_fresh_reconciliation(
    tmp_path,
):
    _, connection, repo = open_repo(tmp_path)
    try:
        client = FakeClient()
        repo.upsert_market(client.markets[0], {"id": "daemon-m1"})
        action = repo.create_automation_action(
            SimpleNamespace(
                id="act_cccccccccccccccc",
                kind="trade_live",
                market_id="daemon-m1",
                reason="micro live review",
                command_preview="trade --market daemon-m1",
                idempotency_key=None,
                requested_by="test",
                created_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc).replace(year=2099),
            )
        )
        repo.save_reconciliation("ok", {"test": True})
        repo.upsert_strategy_override(
            market_id="daemon-m1", profile="micro-live", live_auto_enabled=True
        )
        daemon = OperatorDaemon(
            repository=repo,
            client=client,
            profile=get_profile("micro-live"),
            settings=Settings(TRADING_DISABLED=False, COMPLIANCE_CHECK_ENABLED=False),
            dry_run_only=False,
            allow_live_auto=True,
            live_market_ids={"daemon-m1"},
        )
        runner = RecordingRunner()
        daemon.automation.runner = runner

        result = daemon.tick(discover=False, propose=False, auto_dry_run=False, auto_live=True)

        assert result.auto_live_executed_action_ids == [action["id"]]
        assert result.skipped_live_action_ids == []
        assert runner.calls == [
            [
                "uv",
                "run",
                "polymarket-weather",
                "trade",
                "--market",
                "daemon-m1",
            ]
        ]
        assert repo.get_automation_action(action["id"])["status"] == "executed"
    finally:
        connection.close()


def test_daemon_auto_live_is_blocked_by_compliance_gate(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    try:
        client = FakeClient()
        repo.upsert_market(client.markets[0], {"id": "daemon-m1"})
        action = repo.create_automation_action(
            SimpleNamespace(
                id="act_fededededededede",
                kind="trade_live",
                market_id="daemon-m1",
                reason="micro live review",
                command_preview="trade --market daemon-m1",
                idempotency_key=None,
                requested_by="test",
                created_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc).replace(year=2099),
            )
        )
        repo.save_reconciliation("ok", {"test": True})
        repo.upsert_strategy_override(
            market_id="daemon-m1", profile="micro-live", live_auto_enabled=True
        )
        daemon = OperatorDaemon(
            repository=repo,
            client=client,
            profile=get_profile("micro-live"),
            settings=Settings(TRADING_DISABLED=True),
            dry_run_only=False,
            allow_live_auto=True,
            live_market_ids={"daemon-m1"},
        )
        runner = RecordingRunner()
        daemon.automation.runner = runner

        result = daemon.tick(discover=False, propose=False, auto_dry_run=False, auto_live=True)

        assert result.auto_live_executed_action_ids == []
        assert result.skipped_live_action_ids == [action["id"]]
        assert runner.calls == []
        assert repo.get_automation_action(action["id"])["status"] == "pending"
    finally:
        connection.close()


def test_daemon_auto_live_requires_strategy_override_enablement(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    try:
        client = FakeClient()
        repo.upsert_market(client.markets[0], {"id": "daemon-m1"})
        action = repo.create_automation_action(
            SimpleNamespace(
                id="act_dddddddddddddddd",
                kind="trade_live",
                market_id="daemon-m1",
                reason="micro live review",
                command_preview="trade --market daemon-m1",
                idempotency_key=None,
                requested_by="test",
                created_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc).replace(year=2099),
            )
        )
        repo.save_reconciliation("ok", {"test": True})
        daemon = OperatorDaemon(
            repository=repo,
            client=client,
            profile=get_profile("micro-live"),
            settings=Settings(TRADING_DISABLED=False, COMPLIANCE_CHECK_ENABLED=False),
            dry_run_only=False,
            allow_live_auto=True,
            live_market_ids={"daemon-m1"},
        )
        daemon.automation.runner = RecordingRunner()

        result = daemon.tick(discover=False, propose=False, auto_dry_run=False, auto_live=True)

        assert result.auto_live_executed_action_ids == []
        assert result.skipped_live_action_ids == [action["id"]]
        assert repo.get_automation_action(action["id"])["status"] == "pending"
    finally:
        connection.close()


def test_daemon_order_monitor_counts_reconciliation_state(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    try:
        client = FakeClient()
        repo.upsert_market(client.markets[0], {"id": "daemon-m1"})
        client.orders = [
            {
                "id": "order-1",
                "market": "daemon-m1",
                "asset_id": "yes-token",
                "side": "BUY",
                "price": "0.25",
                "size": "10",
                "status": "live",
            }
        ]
        client.trades = [
            {
                "id": "trade-1",
                "order_id": "order-1",
                "market": "daemon-m1",
                "side": "BUY",
                "price": "0.20",
                "size": "5",
                "fee": "0",
                "timestamp": "2026-05-06T00:00:00+00:00",
            }
        ]
        client.positions = [
            {"market": "daemon-m1", "outcome": "Yes", "size": "5", "notional": "1.25"}
        ]
        daemon = OperatorDaemon(
            repository=repo,
            client=client,
            profile=get_profile("dry-run-demo"),
            dry_run_only=True,
        )

        result = daemon.tick(
            discover=False, propose=False, auto_dry_run=False, include_reconciliation=True
        )

        assert result.open_orders_count == 1
        assert result.positions_count == 1
        assert result.nonzero_positions_count == 1
        assert result.fills_count == 1
        assert "nonzero positions present: 1" in result.risk_anomalies
    finally:
        connection.close()


def test_daemon_position_blocker_can_be_explicitly_disabled(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    try:
        client = FakeClient()
        repo.upsert_market(client.markets[0], {"id": "daemon-m1"})
        client.positions = [
            {"market": "daemon-m1", "outcome": "Yes", "size": "5", "notional": "1.25"}
        ]
        action = repo.create_automation_action(
            SimpleNamespace(
                id="act_eeeeeeeeeeeeeeee",
                kind="trade_live",
                market_id="daemon-m1",
                reason="micro live review",
                command_preview="trade --market daemon-m1",
                idempotency_key=None,
                requested_by="test",
                created_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc).replace(year=2099),
            )
        )
        repo.upsert_strategy_override(
            market_id="daemon-m1", profile="micro-live", live_auto_enabled=True
        )
        daemon = OperatorDaemon(
            repository=repo,
            client=client,
            profile=get_profile("micro-live"),
            settings=Settings(
                _env_file=None,
                COMPLIANCE_CHECK_ENABLED=False,
                TRADING_DISABLED=False,
                MAX_ORDER_USDC=Decimal("2"),
                MAX_DAILY_USDC=Decimal("5"),
                MAX_MARKET_USDC=Decimal("2"),
            ),
            dry_run_only=False,
            allow_live_auto=True,
            live_market_ids={"daemon-m1"},
            block_live_on_positions=False,
        )
        runner = RecordingRunner()
        daemon.automation.runner = runner

        result = daemon.tick(
            discover=False,
            propose=False,
            auto_dry_run=False,
            include_reconciliation=True,
            auto_live=True,
        )

        assert result.auto_live_executed_action_ids == [action["id"]]
        assert runner.calls
    finally:
        connection.close()


def test_daemon_notifications_route_each_phase_role(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    notifications = []
    runner = RecordingRunner()
    try:
        daemon = OperatorDaemon(
            repository=repo,
            client=FakeClient(),
            profile=get_profile("dry-run-demo"),
            dry_run_only=True,
            notifier=notifications.append,
        )
        daemon.automation.runner = runner
        workflow_factory = use_recording_workflow(daemon)

        result = daemon.tick(risk_guard=False)

        roles = {payload["daemon_event"]: payload["role"] for payload in notifications}
        assert roles["daemon_discovery"] == "scanner"
        assert roles["daemon_proposal"] == "captain"
        assert roles["daemon_dry_run"] == "trader"
        assert roles["daemon_tick"] == "reviewer"
        assert runner.calls == []
        assert workflow_factory.workflows[0].calls == [
            ("research_market", "daemon-m1"),
            ("dry_run_trade", "daemon-m1"),
        ]
        assert result.notifications_sent == [
            "daemon_discovery",
            "daemon_proposal",
            "daemon_dry_run",
            "daemon_tick",
        ]
    finally:
        connection.close()


def test_daemon_skips_live_actions_when_breaker_tripped(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    try:
        from polymarket_weather_arb.services.circuit_breaker_service import CircuitBreakerService

        CircuitBreakerService(repo).trip("Resolution mismatch testing")

        client = FakeClient()
        repo.upsert_market(client.markets[0], {"id": "daemon-m1"})
        action = repo.create_automation_action(
            SimpleNamespace(
                id="act_ffffffffffffffff",
                kind="trade_live",
                market_id="daemon-m1",
                reason="micro live review",
                command_preview="trade --market daemon-m1",
                idempotency_key=None,
                requested_by="test",
                created_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc).replace(year=2099),
            )
        )
        repo.upsert_strategy_override(
            market_id="daemon-m1", profile="micro-live", live_auto_enabled=True
        )
        daemon = OperatorDaemon(
            repository=repo,
            client=client,
            profile=get_profile("micro-live"),
            settings=Settings(TRADING_DISABLED=False, COMPLIANCE_CHECK_ENABLED=False),
            dry_run_only=False,
            allow_live_auto=True,
            live_market_ids={"daemon-m1"},
            block_live_on_positions=False,
        )
        runner = RecordingRunner()
        daemon.automation.runner = runner

        result = daemon.tick(
            discover=False,
            propose=False,
            auto_dry_run=False,
            include_reconciliation=True,
            auto_live=True,
        )

        assert result.auto_live_executed_action_ids == []
        assert result.skipped_live_action_ids == [action["id"]]
        assert runner.calls == []
    finally:
        connection.close()


def test_daemon_default_does_not_auto_exit_sell(tmp_path):
    """Default daemon tick must never call place_sell_limit_order."""
    _, connection, repo = open_repo(tmp_path)
    client = FakeClient()
    try:
        repo.upsert_market(
            client.markets[0],
            {
                "id": "daemon-m1",
                "outcomes": ["Yes", "No"],
                "clobTokenIds": ["yes-token", "no-token"],
            },
        )
        repo.replace_positions(
            [
                {
                    "market": "daemon-m1",
                    "outcome": "Yes",
                    "size": "5",
                    "avgPrice": "0.13",
                }
            ]
        )
        repo.save_reconciliation("ok", {"status": "ok"})
        from polymarket_weather_arb.domain.pricing import Analysis

        repo.save_analysis(
            Analysis(
                market_id="daemon-m1",
                model_version="t",
                fair_lower=Decimal("0.2"),
                fair_upper=Decimal("0.3"),
                reference_price=Decimal("0.5"),
                edge=Decimal("0.01"),
                side=None,
                decision="skip",
                reasons=["edge gone"],
            )
        )
        daemon = OperatorDaemon(
            repository=repo,
            client=client,
            profile=get_profile("micro-live"),
            settings=Settings(
                COMPLIANCE_CHECK_ENABLED=False,
                AUTO_EXIT_ENABLED=False,
                POLYMARKET_PRIVATE_KEY="k",
                POLYMARKET_FUNDER="0xf",
            ),
            dry_run_only=True,
            allow_auto_exit=False,
        )
        use_recording_workflow(daemon)
        result = daemon.tick(
            discover=False,
            propose=False,
            auto_dry_run=False,
            risk_guard=False,
            audit=False,
        )
        assert result.auto_exit_executed == 0
        assert result.auto_exit_armed is False
        assert client.sell_calls == []
    finally:
        connection.close()
