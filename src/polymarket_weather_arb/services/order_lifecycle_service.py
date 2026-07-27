from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from polymarket_weather_arb.adapters.polymarket.base import PolymarketClient
from polymarket_weather_arb.services.exit_guardian_service import (
    ExitGuardianService,
    ExitRecommendation,
)
from polymarket_weather_arb.storage.repositories import Repository


@dataclass
class CancelStaleResult:
    cancelled: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class LifecycleRecommendation:
    """Standardized, serializable dry-run lifecycle recommendation.

    Phase 1 is advisory only: execute is always False and no cancel/close path
    is invoked from review_lifecycle.
    """

    kind: str
    action: str
    market_id: str
    reason: str
    execute: bool = False
    dry_run: bool = True
    exchange_order_id: str | None = None
    outcome: str | None = None
    token_id: str | None = None
    side: str | None = None
    notional: Decimal | None = None
    age_seconds: int | None = None
    latest_decision: str | None = None
    latest_edge: Decimal | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.notional is not None:
            payload["notional"] = str(self.notional)
        if self.latest_edge is not None:
            payload["latest_edge"] = str(self.latest_edge)
        payload["execute"] = False
        payload["dry_run"] = True
        return payload

    @classmethod
    def from_exit_recommendation(cls, item: ExitRecommendation) -> LifecycleRecommendation:
        return cls(
            kind=item.kind,
            action=item.action,
            market_id=item.market_id,
            reason=item.reason,
            execute=False,
            dry_run=True,
            exchange_order_id=item.exchange_order_id,
            outcome=item.outcome,
            token_id=item.token_id,
            side=item.side,
            notional=item.notional,
            age_seconds=item.age_seconds,
            latest_decision=item.latest_decision,
            latest_edge=item.latest_edge,
        )


class OrderLifecycleService:
    def __init__(self, client: PolymarketClient | None, repository: Repository) -> None:
        self.client = client
        self.repository = repository

    def review_lifecycle(
        self,
        *,
        stale_threshold_seconds: int = 300,
        min_edge: Decimal = Decimal("0.03"),
    ) -> list[LifecycleRecommendation]:
        """Dry-run review of open orders and positions.

        Reuses ExitGuardianService so cancel/hold logic stays single-sourced.
        Never calls cancel_order, cancel_stale_orders, or any close path.
        """
        recommendations = ExitGuardianService(self.repository).evaluate(
            stale_threshold_seconds=stale_threshold_seconds,
            min_edge=min_edge,
        )
        return [LifecycleRecommendation.from_exit_recommendation(item) for item in recommendations]

    def refresh_open_orders(self) -> int:
        return self.repository.replace_open_orders(self._require_client().get_orders())

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._require_client().get_order(order_id)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        response = self._require_client().cancel_order(order_id)
        status = str(response.get("status") or response.get("state") or "cancelled")
        self.repository.mark_open_order_status(order_id, status, response)
        return response

    def cancel_stale_orders(self, stale_threshold_seconds: int = 300) -> CancelStaleResult:
        result = CancelStaleResult()
        for order in self.detect_stale_orders(stale_threshold_seconds):
            order_id = order.get("exchange_order_id")
            if not order_id:
                continue
            try:
                result.cancelled.append(self.cancel_order(str(order_id)))
            except Exception as exc:
                result.failures.append(
                    {
                        "exchange_order_id": str(order_id),
                        "market_id": order.get("market_id"),
                        "error": str(exc),
                    }
                )
        return result

    def cancel_all_open_orders(self) -> list[dict[str, Any]]:
        cancelled = []
        for order in self.repository.list_open_orders(limit=1000):
            try:
                order_id = (
                    order["exchange_order_id"] if "exchange_order_id" in order.keys() else None
                )
            except (KeyError, IndexError):
                order_id = None
            if not order_id:
                continue
            cancelled.append(self.cancel_order(str(order_id)))
        return cancelled

    def _require_client(self) -> PolymarketClient:
        if self.client is None:
            raise RuntimeError("Polymarket client is required for exchange lifecycle actions")
        return self.client

    def detect_stale_orders(self, stale_threshold_seconds: int = 300) -> list[dict[str, Any]]:
        """Detect open orders older than threshold using durable first-seen age."""
        open_orders = self.repository.list_open_orders(limit=1000)
        stale_orders: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)

        for order in open_orders:
            order_time = _order_age_anchor(order)
            if order_time is None:
                continue
            age_seconds = (now - order_time).total_seconds()
            if age_seconds > stale_threshold_seconds:
                order_dict = dict(order)
                order_dict["age_seconds"] = age_seconds
                order_dict["is_stale"] = True
                stale_orders.append(order_dict)

        return stale_orders

    def get_position_exposure(self) -> dict[str, Any]:
        """获取持仓敞口摘要"""
        positions = self.repository.list_positions(limit=100)
        nonzero_positions = []
        for p in positions:
            try:
                size = p["size"] if "size" in p.keys() else None
                if size and float(size) > 0:
                    nonzero_positions.append(dict(p))
            except (KeyError, ValueError, TypeError):
                continue

        total_exposure = sum(float(p.get("notional", 0) or 0) for p in nonzero_positions)
        market_exposures = {}
        for pos in nonzero_positions:
            market_id = pos.get("market_id", "unknown")
            market_exposures[market_id] = market_exposures.get(market_id, 0) + float(
                pos.get("notional", 0) or 0
            )

        return {
            "total_positions": len(positions),
            "nonzero_positions": len(nonzero_positions),
            "total_exposure": total_exposure,
            "market_exposures": market_exposures,
            "positions": nonzero_positions,
        }

    def get_fill_summary(self, days: int = 7) -> dict[str, Any]:
        """获取成交摘要"""
        fills = self.repository.list_fills(limit=1000)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)

        recent_fills = []
        total_volume = 0.0
        total_fees = 0.0

        for fill in fills:
            try:
                filled_at = fill["filled_at"] if "filled_at" in fill.keys() else None
            except (KeyError, IndexError):
                filled_at = None

            if not filled_at:
                continue

            try:
                if isinstance(filled_at, str):
                    fill_time = datetime.fromisoformat(filled_at.replace("Z", "+00:00"))
                else:
                    fill_time = filled_at

                if fill_time >= cutoff:
                    fill_dict = dict(fill)
                    recent_fills.append(fill_dict)
                    total_volume += float(fill_dict.get("size", 0) or 0)
                    total_fees += float(fill_dict.get("fee", 0) or 0)
            except (ValueError, AttributeError):
                continue

        return {
            "period_days": days,
            "total_fills": len(recent_fills),
            "total_volume": total_volume,
            "total_fees": total_fees,
            "fills": recent_fills,
        }

    def get_order_statistics(self) -> dict[str, Any]:
        """获取订单统计"""
        open_orders = self.repository.list_open_orders(limit=1000)
        now = datetime.now(timezone.utc)

        total_orders = len(open_orders)
        stale_orders = 0
        total_notional = 0.0

        for order in open_orders:
            try:
                notional = order["notional"] if "notional" in order.keys() else None
                if notional:
                    total_notional += float(notional)
            except (KeyError, ValueError, TypeError):
                pass

            order_time = _order_age_anchor(order)
            if order_time is not None:
                age_seconds = (now - order_time).total_seconds()
                if age_seconds > 300:  # 5 minutes
                    stale_orders += 1

        return {
            "total_orders": total_orders,
            "stale_orders": stale_orders,
            "total_notional": total_notional,
        }

    def get_position_risk_summary(self) -> dict[str, Any]:
        """获取持仓风险摘要"""
        positions = self.repository.list_positions(limit=100)
        nonzero_positions = []
        for p in positions:
            try:
                size = p["size"] if "size" in p.keys() else None
                if size and abs(float(size)) > 0:
                    nonzero_positions.append(dict(p))
            except (KeyError, ValueError, TypeError):
                continue

        total_exposure = sum(abs(float(p.get("notional", 0) or 0)) for p in nonzero_positions)
        market_exposures = {}
        for pos in nonzero_positions:
            market_id = pos.get("market_id", "unknown")
            market_exposures[market_id] = market_exposures.get(market_id, 0) + abs(
                float(pos.get("notional", 0) or 0)
            )

        # 计算风险指标
        max_market_exposure = max(market_exposures.values()) if market_exposures else 0
        concentration_risk = max_market_exposure / total_exposure if total_exposure > 0 else 0

        return {
            "total_positions": len(positions),
            "nonzero_positions": len(nonzero_positions),
            "total_exposure": total_exposure,
            "max_market_exposure": max_market_exposure,
            "concentration_risk": concentration_risk,
            "market_exposures": market_exposures,
        }


def _order_age_anchor(order: Any) -> datetime | None:
    """Earliest durable age anchor among first_seen_at and updated_at (UTC-aware)."""
    keys = order.keys() if hasattr(order, "keys") else []
    candidates: list[object] = []
    if "first_seen_at" in keys:
        candidates.append(order["first_seen_at"])
    if "updated_at" in keys:
        candidates.append(order["updated_at"])
    best: datetime | None = None
    for value in candidates:
        parsed = _parse_utc_datetime(value)
        if parsed is None:
            continue
        if best is None or parsed < best:
            best = parsed
    return best


def _parse_utc_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
