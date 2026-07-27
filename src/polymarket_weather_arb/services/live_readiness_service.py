from __future__ import annotations

import importlib.util
from dataclasses import dataclass

from polymarket_weather_arb.adapters.polymarket.base import PolymarketClient
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.services.circuit_breaker_service import live_execution_blocked
from polymarket_weather_arb.services.compliance_service import ComplianceService
from polymarket_weather_arb.services.live_monitor_service import is_fresh_reconciliation
from polymarket_weather_arb.services.reconciliation_service import ReconciliationService
from polymarket_weather_arb.storage.repositories import Repository


@dataclass(frozen=True)
class LiveReadinessCheck:
    name: str
    ok: bool
    status: str
    detail: str


class LiveReadinessService:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        *,
        client: PolymarketClient | None = None,
        compliance_service: ComplianceService | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.client = client
        self.compliance_service = compliance_service or ComplianceService(settings)

    def check(self, *, check_exchange: bool) -> list[LiveReadinessCheck]:
        checks = [
            self._credentials_check(),
            self._compliance_check(),
            self._circuit_breaker_check(),
            self._sdk_check(),
        ]
        if check_exchange:
            checks.append(self._balance_auth_path_check())
            checks.append(self._order_signing_auth_path_check())
            checks.append(self._exchange_check())
        checks.append(self._reconciliation_check())
        return checks

    def _circuit_breaker_check(self) -> LiveReadinessCheck:
        blocker = live_execution_blocked(self.repository)
        if blocker:
            return LiveReadinessCheck("resolution_circuit_breaker", False, "tripped", blocker)
        return LiveReadinessCheck(
            "resolution_circuit_breaker", True, "ok", "circuit breaker is not tripped"
        )

    def _credentials_check(self) -> LiveReadinessCheck:
        try:
            self.settings.ensure_live_trading_ready()
        except ValueError as exc:
            return LiveReadinessCheck("credentials", False, "missing", str(exc))
        return LiveReadinessCheck(
            "credentials", True, "configured", "live credentials are configured"
        )

    def _compliance_check(self) -> LiveReadinessCheck:
        decision = self.compliance_service.check_live_allowed()
        return LiveReadinessCheck("compliance", decision.ok, decision.status, decision.reason)

    def _sdk_check(self) -> LiveReadinessCheck:
        if importlib.util.find_spec("polymarket") is None:
            return LiveReadinessCheck(
                "sdk",
                False,
                "missing",
                "polymarket-client is not importable",
            )
        return LiveReadinessCheck("sdk", True, "installed", "polymarket-client is importable")

    def _balance_auth_path_check(self) -> LiveReadinessCheck:
        if self.client is None:
            return LiveReadinessCheck(
                "balance_auth_path",
                False,
                "skipped",
                "no authenticated client was provided",
            )
        try:
            balances = self.client.get_balances()
        except Exception as exc:
            return LiveReadinessCheck(
                "balance_auth_path",
                False,
                "failed",
                f"balance read failed: {exc}",
            )
        balance = balances.get("balance") if isinstance(balances, dict) else None
        detail = (
            f"balance endpoint authenticated; balance={balance}"
            if balance is not None
            else "balance endpoint authenticated"
        )
        return LiveReadinessCheck("balance_auth_path", True, "ok", detail)

    def _order_signing_auth_path_check(self) -> LiveReadinessCheck:
        if self.client is None:
            return LiveReadinessCheck(
                "order_signing_auth_path",
                False,
                "skipped",
                "no authenticated client was provided",
            )
        validator = getattr(self.client, "validate_order_signing", None)
        if validator is None:
            return LiveReadinessCheck(
                "order_signing_auth_path",
                False,
                "missing",
                "authenticated client cannot validate order signing",
            )
        try:
            result = validator()
        except Exception as exc:
            return LiveReadinessCheck(
                "order_signing_auth_path",
                False,
                "failed",
                f"order signing validation failed: {exc}",
            )
        if not isinstance(result, dict):
            return LiveReadinessCheck(
                "order_signing_auth_path",
                False,
                "invalid",
                "order signing validation returned an unexpected result",
            )
        return LiveReadinessCheck(
            "order_signing_auth_path",
            bool(result.get("ok")),
            str(result.get("status", "unknown")),
            str(result.get("detail", "order signing validation completed")),
        )

    def _exchange_check(self) -> LiveReadinessCheck:
        if self.client is None:
            return LiveReadinessCheck(
                "exchange_reads",
                False,
                "skipped",
                "no authenticated client was provided",
            )
        result = ReconciliationService(self.client, self.repository).reconcile()
        status = str(result.get("status", "unknown"))
        return LiveReadinessCheck(
            "exchange_reads",
            status == "ok",
            status,
            f"reconciliation status={status}",
        )

    def _reconciliation_check(self) -> LiveReadinessCheck:
        latest = self.repository.latest_successful_reconciliation()
        if latest is None:
            return LiveReadinessCheck(
                "reconciliation",
                False,
                "missing",
                "no successful reconciliation is stored",
            )
        if not is_fresh_reconciliation(latest):
            return LiveReadinessCheck(
                "reconciliation",
                False,
                "stale",
                "latest successful reconciliation is stale",
            )
        return LiveReadinessCheck(
            "reconciliation",
            True,
            "fresh",
            "latest successful reconciliation is fresh",
        )


def readiness_is_ok(checks: list[LiveReadinessCheck]) -> bool:
    return all(check.ok for check in checks)
