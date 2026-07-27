from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from typer.testing import CliRunner

from helpers import strip_ansi

from polymarket_weather_arb.cli import app
from polymarket_weather_arb.domain.markets import MarketSnapshot
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.weather import ForecastSnapshot
from polymarket_weather_arb.services.automation_service import AutomationService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository

runner = CliRunner()


def test_cli_registers_command_group_modules():
    from polymarket_weather_arb import cli
    from polymarket_weather_arb.cli_commands import automation, fixtures, operator, profiles

    assert cli.automation_app is automation.automation_app
    assert cli.fixtures_app is fixtures.fixtures_app
    assert cli.operator_app is operator.operator_app
    assert cli.profiles_app is profiles.profiles_app


def test_cli_command_groups_keep_existing_help_names():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "automation" in result.stdout
    assert "fixtures" in result.stdout
    assert "operator" in result.stdout
    assert "profiles" in result.stdout


class FakeChinaWeatherProvider:
    def fetch_forecast(self, market_id, rule):
        now = datetime.now(timezone.utc)
        return (
            ForecastSnapshot(
                provider="fake-china-official",
                variable=rule.variable,
                value=Decimal("18"),
                unit="C",
                issue_time=now,
                valid_time=now,
                market_id=market_id,
                location=rule.city,
                station=rule.station_id,
                lower_value=Decimal("17.8"),
                upper_value=Decimal("18.2"),
                fetched_at=now,
            ),
            {"fake": True},
        )


def fake_china_provider_factory():
    return FakeChinaWeatherProvider


class FakeSmokeLiveClient:
    def __init__(self):
        self.market = None
        self.submitted = []
        self.cancelled = []

    def get_market(self, market_id):
        return (self.market, {"id": market_id}) if self.market is not None else None

    def validate_order_signing(self):
        return {
            "ok": True,
            "status": "wallet-path-configured",
            "detail": "polymarket-client will sign orders with wallet=POLYMARKET_FUNDER",
        }

    def place_limit_order(self, *, token_id, side, price, size):
        self.submitted.append({"token_id": token_id, "side": side, "price": price, "size": size})
        return {"ok": True, "order_id": "order-1", "status": "live"}

    def get_order(self, order_id):
        return {
            "id": order_id,
            "status": "LIVE",
            "size_matched": "0",
            "associate_trades": [],
        }

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {"canceled": [order_id], "not_canceled": {}}

    def get_balances(self):
        return {"balance": 65030109}

    def get_orders(self):
        return []

    def get_trades(self):
        return []

    def get_positions(self):
        return []


def test_doctor_live_reports_only_active_auth_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "operator.db"))
    monkeypatch.setenv("COMPLIANCE_CHECK_ENABLED", "false")

    result = runner.invoke(app, ["doctor", "--live"])

    assert result.exit_code == 0
    assert "Live credentials readiness:" in result.stdout
    assert "Compliance readiness:" in result.stdout
    assert "Relayer" not in result.stdout


def test_live_trade_is_blocked_by_trading_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "operator.db"))
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "test-key")
    monkeypatch.setenv("POLYMARKET_FUNDER", "test-funder")
    monkeypatch.setenv("TRADING_DISABLED", "true")

    result = runner.invoke(app, ["trade", "--market", "missing-market"])

    assert result.exit_code != 0
    assert "TRADING_DISABLED=true blocks live trading" in result.output


def test_live_readiness_command_renders_checks(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "operator.db"))
    monkeypatch.setenv("TRADING_DISABLED", "true")

    result = runner.invoke(app, ["live-readiness", "--no-check-exchange"])

    assert result.exit_code == 0
    assert "credentials" in result.stdout
    assert "compliance" in result.stdout
    assert "TRADING_DISABLED" in result.stdout


def test_operator_smoke_live_records_submit_check_cancel_and_reconcile(tmp_path, monkeypatch):
    from polymarket_weather_arb.cli_commands import operator
    from polymarket_weather_arb.domain.markets import Market

    db_path = tmp_path / "operator.db"
    fake_client = FakeSmokeLiveClient()
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "test-key")
    monkeypatch.setenv("POLYMARKET_FUNDER", "0xfunder")
    monkeypatch.setenv("TRADING_DISABLED", "false")
    monkeypatch.setattr(operator, "GammaPolymarketClient", lambda settings: fake_client)

    fake_client.market = Market(
        id="2853383",
        slug="chicago-90f",
        title="Will Chicago high be 90F?",
        description="Smoke market",
        yes_token_id="yes-token",
        no_token_id="no-token",
        status="active",
        is_weather=True,
    )

    result = runner.invoke(
        app,
        [
            "operator",
            "smoke-live",
            "--market",
            "2853383",
            "--side",
            "buy_yes",
            "--price",
            "0.001",
            "--size",
            "1000",
            "--cancel-immediately",
        ],
    )

    assert result.exit_code == 0
    assert "Smoke live intent" in result.stdout
    assert "cancelled" in result.stdout
    assert fake_client.submitted == [
        {"token_id": "yes-token", "side": "buy_yes", "price": "0.001", "size": "1000"}
    ]
    assert fake_client.cancelled == ["order-1"]

    database = Database(db_path)
    connection = database.connect()
    try:
        repository = Repository(connection)
        intent = repository.list_recent_order_intents(limit=1, market_id="2853383")[0]
        attempts = connection.execute(
            "SELECT status, request_payload, response_payload FROM order_attempts WHERE intent_id = ? ORDER BY id",
            (intent["id"],),
        ).fetchall()
        assert intent["dry_run"] == 0
        assert intent["status"] == "cancelled"
        assert [row["status"] for row in attempts] == [
            "submitted",
            "checked",
            "cancelled",
            "reconciled",
        ]
    finally:
        connection.close()


def test_operator_smoke_live_blocks_notional_above_one_usdc(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "operator.db"))
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "test-key")
    monkeypatch.setenv("POLYMARKET_FUNDER", "0xfunder")
    monkeypatch.setenv("TRADING_DISABLED", "false")

    result = runner.invoke(
        app,
        [
            "operator",
            "smoke-live",
            "--market",
            "2853383",
            "--side",
            "buy_yes",
            "--price",
            "0.002",
            "--size",
            "1000",
        ],
    )

    assert result.exit_code != 0
    assert "notional 2.000 exceeds max loss 1" in result.output


class FakeCloseLiveClient:
    def __init__(self):
        self.sell_calls = []
        self.buy_calls = []

    def place_limit_order(self, *, token_id, side, price, size):
        self.buy_calls.append({"token_id": token_id, "side": side, "price": price, "size": size})
        raise RuntimeError("BUY path must not be used by close-live")

    def place_sell_limit_order(self, *, token_id, price, size):
        self.sell_calls.append({"token_id": token_id, "price": price, "size": size})
        return {"ok": True, "order_id": "sell-order-1", "status": "live"}

    def get_token_order_book(self, token_id):
        return (
            MarketSnapshot(
                market_id="token_book",
                best_bid=Decimal("0.50"),
                best_ask=Decimal("0.55"),
                midpoint=Decimal("0.525"),
                spread=Decimal("0.05"),
                liquidity=Decimal("100"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {},
        )

    def get_order(self, order_id):
        return {"id": order_id, "status": "LIVE"}

    def get_balances(self):
        return {"balance": 1}

    def get_orders(self):
        return []

    def get_trades(self):
        return []

    def get_positions(self):
        return []


def test_operator_close_live_wrong_confirm_zero_mutation(tmp_path, monkeypatch):
    from polymarket_weather_arb.cli_commands import operator
    from polymarket_weather_arb.domain.markets import Market

    db_path = tmp_path / "close-live.db"
    fake_client = FakeCloseLiveClient()
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "test-key")
    monkeypatch.setenv("POLYMARKET_FUNDER", "0xfunder")
    monkeypatch.setenv("TRADING_DISABLED", "false")
    monkeypatch.setenv("COMPLIANCE_CHECK_ENABLED", "false")
    monkeypatch.setattr(operator, "GammaPolymarketClient", lambda settings: fake_client)

    database = Database(db_path)
    database.init_schema()
    connection = database.connect()
    try:
        repository = Repository(connection)
        market = Market(
            id="m-close",
            slug="m-close",
            title="Close market",
            description="test",
            yes_token_id="yes-token",
            no_token_id="no-token",
            status="active",
            is_weather=True,
        )
        repository.upsert_market(
            market,
            {
                "id": "m-close",
                "outcomes": ["Yes", "No"],
                "clobTokenIds": ["yes-token", "no-token"],
            },
        )
        repository.replace_positions(
            [
                {
                    "market": "m-close",
                    "outcome": "Yes",
                    "size": "40",
                    "avgPrice": "0.4",
                }
            ]
        )
        repository.save_reconciliation("ok", {"status": "ok"})
        connection.commit()
    finally:
        connection.close()

    result = runner.invoke(
        app,
        [
            "operator",
            "close-live",
            "--market",
            "m-close",
            "--outcome",
            "YES",
            "--price",
            "0.49",
            "--size",
            "10",
            "--max-slippage",
            "0.05",
            "--confirm",
            "SELL m-close YES 999",
        ],
    )

    assert result.exit_code != 0
    assert "Confirm phrase mismatch" in result.output
    assert fake_client.sell_calls == []
    assert fake_client.buy_calls == []


def _seed_close_live_market(db_path, market_id="m-close"):
    from polymarket_weather_arb.domain.markets import Market

    database = Database(db_path)
    database.init_schema()
    connection = database.connect()
    try:
        repository = Repository(connection)
        market = Market(
            id=market_id,
            slug=market_id,
            title="Close market",
            description="test",
            yes_token_id="yes-token",
            no_token_id="no-token",
            status="active",
            is_weather=True,
        )
        repository.upsert_market(
            market,
            {
                "id": market_id,
                "outcomes": ["Yes", "No"],
                "clobTokenIds": ["yes-token", "no-token"],
            },
        )
        repository.replace_positions(
            [
                {
                    "market": market_id,
                    "outcome": "Yes",
                    "size": "40",
                    "avgPrice": "0.4",
                }
            ]
        )
        repository.save_reconciliation("ok", {"status": "ok"})
        connection.commit()
    finally:
        connection.close()


def test_operator_close_live_success_path(tmp_path, monkeypatch):
    from polymarket_weather_arb.cli_commands import operator

    db_path = tmp_path / "close-live-ok.db"
    fake_client = FakeCloseLiveClient()
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "test-key")
    monkeypatch.setenv("POLYMARKET_FUNDER", "0xfunder")
    monkeypatch.setenv("TRADING_DISABLED", "false")
    monkeypatch.setenv("COMPLIANCE_CHECK_ENABLED", "false")
    monkeypatch.setattr(operator, "GammaPolymarketClient", lambda settings: fake_client)
    _seed_close_live_market(db_path)

    result = runner.invoke(
        app,
        [
            "operator",
            "close-live",
            "--market",
            "m-close",
            "--outcome",
            "YES",
            "--price",
            "0.49",
            "--size",
            "10",
            "--max-slippage",
            "0.05",
            "--confirm",
            "SELL m-close YES 10",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "SELL submitted" in result.stdout
    assert fake_client.sell_calls == [{"token_id": "yes-token", "price": "0.49", "size": "10"}]
    assert fake_client.buy_calls == []

    database = Database(db_path)
    connection = database.connect()
    try:
        repository = Repository(connection)
        intent = repository.list_recent_order_intents(limit=1, market_id="m-close")[0]
        attempts = connection.execute(
            "SELECT status FROM order_attempts WHERE intent_id = ? ORDER BY id",
            (intent["id"],),
        ).fetchall()
        assert intent["side"] == "sell_yes"
        assert intent["dry_run"] == 0
        assert [row["status"] for row in attempts] == [
            "submitted",
            "checked",
            "reconciled",
        ]
    finally:
        connection.close()


class FakeCloseLiveClientGetOrderFails(FakeCloseLiveClient):
    def get_order(self, order_id):
        raise RuntimeError("get_order network down")


def test_operator_close_live_get_order_failure_preserves_audit(tmp_path, monkeypatch):
    """P0: get_order failure must not roll back submitted SELL audit rows."""
    from polymarket_weather_arb.cli_commands import operator

    db_path = tmp_path / "close-live-unverif.db"
    fake_client = FakeCloseLiveClientGetOrderFails()
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "test-key")
    monkeypatch.setenv("POLYMARKET_FUNDER", "0xfunder")
    monkeypatch.setenv("TRADING_DISABLED", "false")
    monkeypatch.setenv("COMPLIANCE_CHECK_ENABLED", "false")
    monkeypatch.setattr(operator, "GammaPolymarketClient", lambda settings: fake_client)
    _seed_close_live_market(db_path)

    result = runner.invoke(
        app,
        [
            "operator",
            "close-live",
            "--market",
            "m-close",
            "--outcome",
            "YES",
            "--price",
            "0.49",
            "--size",
            "10",
            "--max-slippage",
            "0.05",
            "--confirm",
            "SELL m-close YES 10",
        ],
    )

    assert result.exit_code == 2, result.output
    assert "not fully verified" in result.output
    assert "Do not re-submit" in result.output
    assert fake_client.sell_calls == [{"token_id": "yes-token", "price": "0.49", "size": "10"}]

    database = Database(db_path)
    connection = database.connect()
    try:
        repository = Repository(connection)
        intent = repository.list_recent_order_intents(limit=1, market_id="m-close")[0]
        attempts = connection.execute(
            "SELECT status FROM order_attempts WHERE intent_id = ? ORDER BY id",
            (intent["id"],),
        ).fetchall()
        assert intent["status"] == "submitted_unverified"
        assert intent["side"] == "sell_yes"
        assert [row["status"] for row in attempts] == ["submitted", "check_failed"]
        # Active idempotency must still block a second SELL.
        assert repository.active_live_order_intent("m-close", "sell_yes") is not None
    finally:
        connection.close()


class FakeCloseLiveClientReconcileFails(FakeCloseLiveClient):
    def get_balances(self):
        raise RuntimeError("balances api down")


def test_operator_close_live_reconcile_failure_not_success(tmp_path, monkeypatch):
    """P1: reconcile adapter-error must not print full success."""
    from polymarket_weather_arb.cli_commands import operator

    db_path = tmp_path / "close-live-recon-fail.db"
    fake_client = FakeCloseLiveClientReconcileFails()
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "test-key")
    monkeypatch.setenv("POLYMARKET_FUNDER", "0xfunder")
    monkeypatch.setenv("TRADING_DISABLED", "false")
    monkeypatch.setenv("COMPLIANCE_CHECK_ENABLED", "false")
    monkeypatch.setattr(operator, "GammaPolymarketClient", lambda settings: fake_client)
    _seed_close_live_market(db_path)

    result = runner.invoke(
        app,
        [
            "operator",
            "close-live",
            "--market",
            "m-close",
            "--outcome",
            "YES",
            "--price",
            "0.49",
            "--size",
            "10",
            "--max-slippage",
            "0.05",
            "--confirm",
            "SELL m-close YES 10",
        ],
    )

    assert result.exit_code == 2, result.output
    assert "not fully verified" in result.output
    assert "Do not re-submit" in result.output
    assert "[green]SELL submitted[/green]" not in result.output

    database = Database(db_path)
    connection = database.connect()
    try:
        repository = Repository(connection)
        intent = repository.list_recent_order_intents(limit=1, market_id="m-close")[0]
        attempts = connection.execute(
            "SELECT status FROM order_attempts WHERE intent_id = ? ORDER BY id",
            (intent["id"],),
        ).fetchall()
        assert intent["status"] == "reconcile_failed"
        assert [row["status"] for row in attempts] == [
            "submitted",
            "checked",
            "reconcile_failed",
        ]
        assert repository.active_live_order_intent("m-close", "sell_yes") is not None
    finally:
        connection.close()


def test_calibration_report_and_settle_commands(tmp_path, monkeypatch):
    db_path = tmp_path / "calibration.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    _seed_calibration_signal(db_path)

    settle = runner.invoke(
        app,
        [
            "calibration-settle",
            "--market",
            "m1",
            "--outcome",
            "yes",
            "--settlement-value",
            "83",
            "--settlement-source",
            "nws-observation",
        ],
    )
    report = runner.invoke(app, ["calibration-report"])

    settle_out = strip_ansi(settle.stdout)
    report_out = strip_ansi(report.stdout)
    assert settle.exit_code == 0
    assert "Updated 1 signal" in settle_out
    assert report.exit_code == 0
    assert "weather-threshold-v1" in report_out
    assert "noaa-nws" in report_out
    assert "Brier" in report_out


def test_settlement_backfill_command_prints_result(tmp_path, monkeypatch):
    from polymarket_weather_arb import cli

    class FakeSettlementService:
        def __init__(self, repository, provider):
            self.repository = repository
            self.provider = provider

        def backfill_market(self, market_id):
            return SimpleNamespace(
                market_id=market_id,
                resolved_outcome="yes",
                observation_value=Decimal("83"),
                observation_unit="F",
                settlement_source="nws-observation",
                updated_signals=1,
                warnings=[],
            )

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "settlement.db"))
    monkeypatch.setattr(cli, "SettlementService", FakeSettlementService)

    result = runner.invoke(app, ["settlement-backfill", "--market", "m1"])

    output = strip_ansi(result.stdout)
    assert result.exit_code == 0
    assert "Backfilled settlement" in output
    assert "market=m1" in output
    assert "outcome=yes" in output
    assert "value=83F" in output
    assert "source=nws-observation" in output
    assert "updated=1" in output


def test_settlement_backfill_preview_prints_preview_and_does_not_mutate(tmp_path, monkeypatch):
    from polymarket_weather_arb import cli

    preview_called = []
    backfill_called = []

    class FakeSettlementService:
        def __init__(self, repository, provider):
            self.repository = repository
            self.provider = provider

        def preview_market(self, market_id):
            preview_called.append(market_id)
            return SimpleNamespace(
                market_id=market_id,
                station="KNYC",
                variable="temperature_high",
                observed_value=Decimal("83"),
                unit="F",
                observed_at=None,
                quality_status="V",
                would_resolve_outcome="yes",
                settlement_source="nws-observation",
                rule_operator=">=",
                rule_threshold=Decimal("80"),
                warnings=[],
            )

        def backfill_market(self, market_id):
            backfill_called.append(market_id)
            return SimpleNamespace(
                market_id=market_id,
                resolved_outcome="yes",
                observation_value=Decimal("83"),
                observation_unit="F",
                settlement_source="nws-observation",
                updated_signals=1,
                warnings=[],
            )

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "settlement.db"))
    monkeypatch.setattr(cli, "SettlementService", FakeSettlementService)

    result = runner.invoke(app, ["settlement-backfill", "--market", "m1", "--preview"])

    assert result.exit_code == 0
    assert "Preview settlement" in result.stdout
    assert "market=m1" in result.stdout
    assert "station=KNYC" in result.stdout
    assert "value=83F" in result.stdout
    assert "quality=V" in result.stdout
    assert "would_resolve=yes" in result.stdout
    assert "source=nws-observation" in result.stdout
    assert preview_called == ["m1"]
    assert backfill_called == []


def test_settlement_backfill_preview_prints_warning_when_present(tmp_path, monkeypatch):
    from polymarket_weather_arb import cli

    class FakeSettlementService:
        def __init__(self, repository, provider):
            pass

        def preview_market(self, market_id):
            return SimpleNamespace(
                market_id=market_id,
                station="KNYC",
                variable="temperature_high",
                observed_value=Decimal("83"),
                unit="F",
                observed_at=None,
                quality_status="X",
                would_resolve_outcome="yes",
                settlement_source="nws-observation",
                rule_operator=">=",
                rule_threshold=Decimal("80"),
                warnings=[
                    "low observation coverage: 1 usable records",
                    "selected observation quality is X",
                ],
            )

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "settlement.db"))
    monkeypatch.setattr(cli, "SettlementService", FakeSettlementService)

    result = runner.invoke(app, ["settlement-backfill", "--market", "m1", "--preview"])

    assert result.exit_code == 0
    assert "WARNING: low observation coverage" in result.stdout
    assert "WARNING: selected observation quality is X" in result.stdout


def test_operator_start_prints_next_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "operator.db"))

    result = runner.invoke(app, ["operator", "start"])

    assert result.exit_code == 0
    assert "operator demo --kind dry-run" in result.stdout
    assert "<MARKET_ID>" not in result.stdout


def test_profiles_list_and_show(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "operator.db"))

    listed = runner.invoke(app, ["profiles", "list"])
    shown = runner.invoke(app, ["profiles", "show", "conservative"])

    assert listed.exit_code == 0
    assert "dry-run-demo" in listed.stdout
    assert shown.exit_code == 0
    assert "conservative" in shown.stdout
    assert "0.08" in shown.stdout


def test_operator_demo_creates_real_action(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "operator.db"))

    result = runner.invoke(app, ["operator", "demo", "--kind", "analyze"])

    output = strip_ansi(result.stdout)
    assert result.exit_code == 0
    assert "Profile:" in output
    assert "act_" in output
    assert "demo-weather-nyc-high-2026-05-08" in output
    assert "/wufu action-approve action-id:act_" in output


def test_operator_queue_renders_actions_and_filters(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "operator.db"))
    runner.invoke(app, ["operator", "demo", "--kind", "analyze"])

    result = runner.invoke(app, ["operator", "queue", "--status", "pending", "--kind", "analyze"])

    assert result.exit_code == 0
    assert "pending" in result.stdout
    assert "analyze" in result.stdout


def test_operator_queue_detail_and_timeline_render_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "operator.db"))
    created = runner.invoke(app, ["operator", "demo", "--kind", "dry-run"])
    action_id = _first_action_id(created.stdout)

    detail = runner.invoke(app, ["operator", "queue-detail", action_id])
    timeline = runner.invoke(app, ["operator", "queue-timeline", action_id])

    assert detail.exit_code == 0
    assert "profile_selected" in detail.stdout
    assert "execution_duration_ms" in detail.stdout
    assert timeline.exit_code == 0
    assert "created" in timeline.stdout


def test_operator_queue_summary_renders_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "operator.db"))
    runner.invoke(app, ["operator", "demo", "--kind", "dry-run"])

    result = runner.invoke(app, ["operator", "queue-summary"])

    assert result.exit_code == 0
    assert "Automation actions" in result.stdout
    assert "pending" in result.stdout


def test_operator_approve_latest_avoids_copying_action_id(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "operator.db"))
    runner.invoke(app, ["operator", "demo", "--kind", "dry-run"])

    result = runner.invoke(app, ["operator", "approve-latest", "--actor", "tester"])

    assert result.exit_code == 0
    assert "approved" in result.stdout
    assert "operator run-approved" in result.stdout


def test_operator_next_prints_real_action_id(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "operator.db"))
    runner.invoke(app, ["operator", "demo", "--kind", "analyze"])

    result = runner.invoke(app, ["operator", "next"])

    assert result.exit_code == 0
    assert "Profile: balanced" in result.stdout
    assert "act_" in result.stdout
    assert "<" not in result.stdout


def test_operator_go_can_propose_with_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "operator.db"))
    runner.invoke(app, ["operator", "demo", "--kind", "dry-run"])
    runner.invoke(
        app,
        [
            "automation",
            "reject",
            "--action-id",
            _first_action_id(runner.invoke(app, ["operator", "queue"]).stdout),
            "--actor",
            "tester",
        ],
    )

    result = runner.invoke(app, ["operator", "go", "--profile", "dry-run-demo", "--propose-only"])

    assert result.exit_code == 0
    assert "operator-go:dry-run-demo" in result.stdout
    assert "dry_run" in result.stdout


def test_operator_daemon_once_can_skip_network_discovery(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "operator.db"))
    runner.invoke(app, ["operator", "demo", "--kind", "dry-run"])

    result = runner.invoke(
        app,
        [
            "operator",
            "daemon",
            "--once",
            "--no-discover",
            "--no-propose",
            "--profile",
            "dry-run-demo",
        ],
    )

    assert result.exit_code == 0
    assert "auto_executed_action_ids" in result.stdout
    assert "open_orders_count" in result.stdout
    assert "act_" in result.stdout


def test_operator_live_monitor_renders_gate_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "operator.db"))
    runner.invoke(app, ["operator", "demo", "--profile", "micro-live", "--kind", "trade-live"])

    result = runner.invoke(app, ["operator", "live-monitor", "--profile", "micro-live"])

    output = strip_ansi(result.stdout)
    assert result.exit_code == 0
    assert "Live monitor:" in output
    assert "allow_live_auto=False" in output
    assert "pending_live_actions=1" in output
    assert "market is not whitelisted" in output


def test_cli_china_bucket_analyze_and_dry_run_use_module_workflow(tmp_path, monkeypatch):
    database_path = tmp_path / "operator.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("MAX_ORDER_USDC", "1")
    monkeypatch.setattr(
        "polymarket_weather_arb.services.market_workflow_service.ChinaOfficialWeatherProvider",
        fake_china_provider_factory(),
    )
    _seed_china_bucket_market(database_path)

    analyze = runner.invoke(app, ["analyze", "--market", "shanghai-18c"])
    trade = runner.invoke(app, ["trade", "--market", "shanghai-18c", "--dry-run"])

    assert analyze.exit_code == 0
    assert "Decision:" in analyze.stdout
    assert "fake-china-official" not in analyze.stdout
    assert trade.exit_code == 0
    assert "dry-run order recorded" in trade.stdout
    assert _latest_action_status(database_path, "shanghai-18c") is None
    assert _latest_order_status(database_path, "shanghai-18c") == "dry_run"


def test_operator_run_approved_executes_china_dry_run_via_workflow(tmp_path, monkeypatch):
    database_path = tmp_path / "operator.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("MAX_ORDER_USDC", "1")
    monkeypatch.setattr(
        "polymarket_weather_arb.services.market_workflow_service.ChinaOfficialWeatherProvider",
        fake_china_provider_factory(),
    )
    action_id = _seed_approved_china_dry_run_action(database_path)

    result = runner.invoke(app, ["operator", "run-approved", "--limit", "1"])

    assert result.exit_code == 0
    assert action_id
    assert _latest_action_status(database_path, "shanghai-18c") == "executed"
    assert _latest_order_status(database_path, "shanghai-18c") == "dry_run"


def test_operator_override_commands_render_strategy_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "operator.db"))

    created = runner.invoke(
        app,
        [
            "operator",
            "override-set",
            "--market",
            "m1",
            "--profile",
            "micro-live",
            "--min-edge",
            "0.12",
            "--max-order-usdc",
            "3",
            "--live-auto",
            "--notes",
            "tiny live test",
        ],
    )
    listed = runner.invoke(app, ["operator", "overrides"])

    assert created.exit_code == 0
    assert "m1" in created.stdout
    assert "micro-live" in created.stdout
    assert "yes" in created.stdout
    assert listed.exit_code == 0
    assert "tiny live test" in listed.stdout


def test_operator_refresh_open_orders_uses_order_lifecycle_service(tmp_path, monkeypatch):
    from polymarket_weather_arb.cli_commands import operator

    class FakeClient:
        def __init__(self, settings):
            self.settings = settings

        def get_orders(self):
            return []

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "operator.db"))
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "test-key")
    monkeypatch.setenv("POLYMARKET_FUNDER", "test-funder")
    monkeypatch.setattr(operator, "GammaPolymarketClient", FakeClient)

    result = runner.invoke(app, ["operator", "refresh-open-orders"])

    output = strip_ansi(result.stdout)
    assert result.exit_code == 0
    assert "Refreshed open orders: 0" in output


def test_operator_cancel_order_uses_order_lifecycle_service(tmp_path, monkeypatch):
    from polymarket_weather_arb.cli_commands import operator

    class FakeClient:
        def __init__(self, settings):
            self.settings = settings

        def cancel_order(self, order_id):
            return {"id": order_id, "status": "cancelled"}

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "operator.db"))
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "test-key")
    monkeypatch.setenv("POLYMARKET_FUNDER", "test-funder")
    monkeypatch.setattr(operator, "GammaPolymarketClient", FakeClient)

    result = runner.invoke(app, ["operator", "cancel-order", "order-1"])

    output = strip_ansi(result.stdout)
    assert result.exit_code == 0
    assert "cancelled" in output
    assert "order-1" in output


def test_operator_exchange_state_commands_render_empty_tables(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "operator.db"))

    open_orders = runner.invoke(app, ["operator", "open-orders"])
    positions = runner.invoke(app, ["operator", "positions"])
    fills = runner.invoke(app, ["operator", "fills"])

    assert open_orders.exit_code == 0
    assert "Order" in open_orders.stdout
    assert positions.exit_code == 0
    assert "Outcome" in positions.stdout
    assert fills.exit_code == 0
    assert "Fill" in fills.stdout


def test_operator_exit_guardian_renders_dry_run_recommendations(tmp_path, monkeypatch):
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

    result = runner.invoke(app, ["operator", "exit-guardian"])

    output = strip_ansi(result.stdout)
    assert result.exit_code == 0
    assert "cancel_edge_gone" in output
    assert "order-1" in output
    assert "Dry-run only" in output


def _first_action_id(output: str) -> str:
    start = output.index("act_")
    end = start
    while end < len(output) and output[end] in "act_0123456789abcdef":
        end += 1
    return output[start:end]


def _seed_china_bucket_market(database_path):
    database = Database(database_path)
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
        repo.save_market_snapshot(
            MarketSnapshot(
                market_id=market.id,
                best_bid=Decimal("0.03"),
                best_ask=Decimal("0.04"),
                midpoint=Decimal("0.035"),
                spread=Decimal("0.01"),
                liquidity=Decimal("100"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {"market": market.id},
        )
        connection.commit()
    finally:
        connection.close()


def _seed_approved_china_dry_run_action(database_path):
    database = Database(database_path)
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        _seed_china_bucket_market(database_path)
        action = AutomationService(repo).propose(kind="dry_run", market_id="shanghai-18c")
        AutomationService(repo).approve(action["id"], "tester")
        connection.commit()
        return action["id"]
    finally:
        connection.close()


def _latest_action_status(database_path, market_id):
    database = Database(database_path)
    connection = database.connect()
    try:
        actions = Repository(connection).list_automation_actions(limit=1, market_id=market_id)
        return actions[0]["status"] if actions else None
    finally:
        connection.close()


def _latest_order_status(database_path, market_id):
    database = Database(database_path)
    connection = database.connect()
    try:
        orders = Repository(connection).list_recent_order_intents(limit=1, market_id=market_id)
        return orders[0]["status"] if orders else None
    finally:
        connection.close()


def _seed_calibration_signal(database_path) -> None:
    from polymarket_weather_arb.domain.markets import Market
    from polymarket_weather_arb.domain.pricing import Analysis

    database = Database(database_path)
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
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
        now = datetime.now(timezone.utc)
        repo.save_forecast(
            ForecastSnapshot(
                provider="noaa-nws",
                variable="temperature_high",
                value=Decimal("82"),
                unit="F",
                issue_time=now,
                valid_time=now,
                market_id="m1",
                location="New York",
                station="KNYC",
                fetched_at=now,
            ),
            {"source_grade": "official_forecast", "provider": "noaa-nws"},
        )
        repo.save_analysis(
            Analysis(
                market_id="m1",
                model_version="weather-threshold-v1",
                fair_lower=Decimal("0.60"),
                fair_upper=Decimal("0.70"),
                reference_price=Decimal("0.50"),
                edge=Decimal("0.10"),
                side="buy_yes",
                decision="trade",
                reasons=["edge"],
            )
        )
        connection.commit()
    finally:
        connection.close()


def test_operator_launch_initializes_database_and_starts_dashboard(tmp_path, monkeypatch):
    from polymarket_weather_arb.cli_commands import operator

    calls = []

    def fake_serve(settings, *, host, port):
        calls.append((settings.database_path, host, port))

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "operator.db"))
    monkeypatch.setattr(operator, "serve_dashboard", fake_serve)

    result = runner.invoke(app, ["operator", "launch", "--port", "9876"])

    assert result.exit_code == 0
    assert "http://127.0.0.1:9876/beginner" in result.stdout
    assert calls == [(tmp_path / "operator.db", "127.0.0.1", 9876)]
    assert (tmp_path / "operator.db").exists()
