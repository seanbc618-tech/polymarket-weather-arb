from __future__ import annotations

import logging
from typing import Any, Callable

from polymarket_weather_arb.adapters.polymarket.base import PolymarketClient
from polymarket_weather_arb.storage.repositories import Repository

logger = logging.getLogger(__name__)


class ReconciliationService:
    def __init__(self, client: PolymarketClient, repository: Repository) -> None:
        self.client = client
        self.repository = repository

    def reconcile(self) -> dict[str, object]:
        try:
            balances = self._read_stage("balances", self.client.get_balances)
            orders = self._read_stage("orders", self.client.get_orders)
            if hasattr(self.client, "get_trades"):
                trades = self._read_stage("trades", self.client.get_trades)
            else:
                trades = []
            positions = self._read_stage("positions", self.client.get_positions)
        except _StageFailure as exc:
            result = {
                "status": exc.status,
                "error": exc.message,
                "error_type": exc.error_type,
                "failed_stage": exc.stage,
            }
            logger.warning(
                "reconciliation %s stage=%s error_type=%s error=%s",
                exc.status,
                exc.stage,
                exc.error_type,
                exc.message,
            )
            self.repository.save_reconciliation(str(result["status"]), result)
            return result

        recovered_markets, recovery_error = self._recover_missing_trade_markets(trades)
        fills_stored, new_fills = self.repository.save_reconciled_fills(trades)
        positions_stored = self.repository.replace_positions(positions)
        orders_stored = self.repository.replace_open_orders(orders)
        intents_updated = self.repository.reconcile_live_order_intent_statuses(trades)
        result = {
            "balances": balances,
            "positions_count": len(positions),
            "positions_stored": positions_stored,
            "orders_count": len(orders),
            "orders_stored": orders_stored,
            "trades_count": len(trades),
            "markets_recovered": recovered_markets,
            "fills_stored": fills_stored,
            "order_intents_updated": intents_updated,
            # Newly inserted fills only (durable de-dupe via exchange_fill_id).
            "new_fills": new_fills,
            "status": "adapter-pending" if balances.get("status") == "not_implemented" else "ok",
            "failed_stage": None,
        }
        if recovery_error:
            result["market_recovery_warning"] = recovery_error
        self.repository.save_reconciliation(str(result["status"]), result)
        return result

    def _read_stage(self, stage: str, reader: Callable[[], Any]) -> Any:
        try:
            return reader()
        except NotImplementedError as exc:
            raise _StageFailure(
                stage=stage,
                status="adapter-pending",
                error_type=type(exc).__name__,
                message=f"stage={stage}: {_redact_error(exc)}",
            ) from exc
        except Exception as exc:
            raise _StageFailure(
                stage=stage,
                status="adapter-error",
                error_type=type(exc).__name__,
                message=f"stage={stage}: {_redact_error(exc)}",
            ) from exc

    def _recover_missing_trade_markets(
        self, trades: list[dict[str, object]]
    ) -> tuple[int, str | None]:
        finder = getattr(self.client, "find_markets_by_condition_ids", None)
        if not callable(finder):
            return 0, None
        condition_ids: set[str] = set()
        for trade in trades:
            condition_id = str(
                trade.get("market") or trade.get("market_id") or trade.get("condition_id") or ""
            )
            token_id = str(
                trade.get("token_id") or trade.get("asset_id") or trade.get("assetId") or ""
            )
            if not condition_id:
                continue
            if self.repository.resolve_local_market_id(condition_id, token_id or None) is None:
                condition_ids.add(condition_id)
        if not condition_ids:
            return 0, None
        try:
            recovered = finder(sorted(condition_ids))
        except Exception as exc:
            return 0, f"historical market recovery failed: {exc}"
        count = 0
        for market, raw_payload in recovered:
            self.repository.upsert_market(market, raw_payload)
            count += 1
        return count, None


class _StageFailure(Exception):
    def __init__(
        self,
        *,
        stage: str,
        status: str,
        error_type: str,
        message: str,
    ) -> None:
        self.stage = stage
        self.status = status
        self.error_type = error_type
        self.message = message
        super().__init__(message)


def _redact_error(exc: BaseException) -> str:
    text = str(exc)
    lowered = text.lower()
    for token in ("private_key", "api_key", "secret", "password", "bearer "):
        if token in lowered:
            return f"{type(exc).__name__}: [redacted]"
    if len(text) > 300:
        return f"{text[:300]}..."
    return text
