from __future__ import annotations

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.services.compliance_service import ComplianceDecision
from polymarket_weather_arb.services.live_readiness_service import (
    LiveReadinessService,
    readiness_is_ok,
)
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


class FakeComplianceService:
    def __init__(self, decision: ComplianceDecision) -> None:
        self.decision = decision

    def check_live_allowed(self) -> ComplianceDecision:
        return self.decision


class FakeClient:
    def get_balances(self):
        return {"status": "ok"}

    def get_orders(self):
        return []

    def get_trades(self):
        return []

    def get_positions(self):
        return []


class SigningReadyFakeClient(FakeClient):
    def validate_order_signing(self):
        return {
            "ok": True,
            "status": "wallet-path-configured",
            "detail": "polymarket-client will sign orders with wallet=POLYMARKET_FUNDER",
        }


class SigningBlockedFakeClient(FakeClient):
    def validate_order_signing(self):
        return {
            "ok": False,
            "status": "missing-sdk",
            "detail": "polymarket-client is not importable",
        }


def test_live_readiness_reports_blocked_compliance_and_missing_credentials(tmp_path):
    _, connection, repo = _repo(tmp_path)
    try:
        checks = LiveReadinessService(
            Settings(
                DATABASE_PATH=tmp_path / "ready.db",
                POLYMARKET_PRIVATE_KEY="",
                POLYMARKET_FUNDER="",
            ),
            repo,
            compliance_service=FakeComplianceService(
                ComplianceDecision(False, "blocked", "country=US is blocked", country="US")
            ),
        ).check(check_exchange=False)

        by_name = {check.name: check for check in checks}
        assert by_name["credentials"].ok is False
        assert by_name["compliance"].ok is False
        assert by_name["compliance"].status == "blocked"
        assert readiness_is_ok(checks) is False
    finally:
        connection.close()


def test_live_readiness_can_run_exchange_reconciliation(tmp_path):
    _, connection, repo = _repo(tmp_path)
    try:
        checks = LiveReadinessService(
            Settings(
                DATABASE_PATH=tmp_path / "ready.db",
                POLYMARKET_PRIVATE_KEY="test-key",
                POLYMARKET_FUNDER="test-funder",
            ),
            repo,
            client=SigningReadyFakeClient(),
            compliance_service=FakeComplianceService(
                ComplianceDecision(True, "allowed", "country=HK is allowed", country="HK")
            ),
        ).check(check_exchange=True)

        by_name = {check.name: check for check in checks}
        assert by_name["credentials"].ok is True
        assert by_name["compliance"].ok is True
        assert by_name["balance_auth_path"].ok is True
        assert by_name["order_signing_auth_path"].ok is True
        assert by_name["exchange_reads"].ok is True
        assert by_name["reconciliation"].status == "fresh"
        assert readiness_is_ok(checks) is True
    finally:
        connection.close()


def test_live_readiness_splits_balance_and_order_signing_paths(tmp_path):
    _, connection, repo = _repo(tmp_path)
    try:
        checks = LiveReadinessService(
            Settings(
                DATABASE_PATH=tmp_path / "ready.db",
                POLYMARKET_PRIVATE_KEY="test-key",
                POLYMARKET_FUNDER="test-funder",
            ),
            repo,
            client=SigningBlockedFakeClient(),
            compliance_service=FakeComplianceService(
                ComplianceDecision(True, "allowed", "country=VN is allowed", country="VN")
            ),
        ).check(check_exchange=True)

        by_name = {check.name: check for check in checks}
        assert by_name["balance_auth_path"].ok is True
        assert by_name["order_signing_auth_path"].ok is False
        assert by_name["order_signing_auth_path"].status == "missing-sdk"
        assert readiness_is_ok(checks) is False
    finally:
        connection.close()


def _repo(tmp_path):
    database = Database(tmp_path / "ready.db")
    database.init_schema()
    connection = database.connect()
    return database, connection, Repository(connection)
