"""Guarded automatic position exit (default OFF).

Automatic SELL only reduces existing reconciled positions via limit orders.
It never creates new exposure, never uses market orders, and never runs unless
every hard gate is open (config + daemon flag + live profile + ExitGuardian).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any

from polymarket_weather_arb.adapters.polymarket.base import PolymarketClient
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.market_eligibility import is_market_orderable
from polymarket_weather_arb.domain.markets import MarketSnapshot
from polymarket_weather_arb.domain.order_constraints import (
    extract_order_constraints,
    residual_is_dust,
)
from polymarket_weather_arb.domain.position_inventory import best_bid_depth_from_book
from polymarket_weather_arb.services.automation_service import AutomationAction
from polymarket_weather_arb.services.compliance_service import ComplianceService
from polymarket_weather_arb.services.exit_guardian_service import (
    ExitGuardianService,
    ExitRecommendation,
)
from polymarket_weather_arb.services.position_exit_service import PositionExitService
from polymarket_weather_arb.storage.repositories import Repository

_PRICE_TICK = Decimal("0.001")
_AUTO_EXIT_PROFILES = frozenset({"micro-live", "full-live"})
# Align with AutopilotService market analysis freshness window.
_ANALYSIS_MAX_AGE = timedelta(minutes=30)
_DUST_SKIP_STATUS = "skipped"
_ACTIVE_ACTION_STATUSES = frozenset({"pending", "approved", "running"})


def _exit_priority(rec: ExitRecommendation) -> tuple[int, Decimal, str]:
    """Prioritize evidence-backed full exits deterministically."""
    action_priority = 0 if rec.action in {"exit_full", "position_at_risk"} else 1
    hold_edge = rec.position_hold_edge
    return (
        action_priority,
        hold_edge if hold_edge is not None else Decimal("0"),
        rec.market_id,
    )


@dataclass
class AutoExitTickResult:
    enabled_gates_ok: bool
    attempted: int = 0
    executed: int = 0
    skipped: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    intent_ids: list[int] = field(default_factory=list)
    action_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Material SELL submissions for caller notifications (ok exchange-accept only).
    submissions: list[dict[str, Any]] = field(default_factory=list)


class AutoExitService:
    def __init__(
        self,
        repository: Repository,
        client: PolymarketClient,
        *,
        exit_service: PositionExitService | None = None,
        guardian: ExitGuardianService | None = None,
    ) -> None:
        self.repository = repository
        self.client = client
        self.exit_service = exit_service or PositionExitService(repository, client)
        self.guardian = guardian or ExitGuardianService(repository)

    @staticmethod
    def status_snapshot(
        *,
        settings: Settings,
        profile_name: str,
        allow_auto_exit: bool,
    ) -> dict[str, Any]:
        """Read-only AUTO EXIT status for UI/CLI (never enables execution)."""
        armed, _ = AutoExitService._gates_open(
            settings=settings,
            profile_name=profile_name,
            allow_auto_exit=allow_auto_exit,
        )
        effective_enabled = profile_name == "full-live" or bool(settings.auto_exit_enabled)
        return {
            "auto_exit_enabled": effective_enabled,
            "allow_auto_exit_flag": bool(allow_auto_exit),
            "profile": profile_name,
            "profile_is_micro_live": profile_name == "micro-live",
            "profile_allows_auto_exit": profile_name in _AUTO_EXIT_PROFILES,
            "max_auto_exits_per_tick": int(settings.max_auto_exits_per_tick),
            "auto_exit_max_position_usdc": str(settings.auto_exit_max_position_usdc),
            "auto_exit_max_slippage": str(settings.auto_exit_max_slippage),
            "armed": armed,
        }

    def run_tick(
        self,
        *,
        settings: Settings,
        profile_name: str,
        allow_auto_exit: bool,
        on_submitted: Callable[[int], None] | None = None,
        compliance_service: ComplianceService | None = None,
        min_edge: Decimal | None = None,
    ) -> AutoExitTickResult:
        ok, blockers = self._gates_open(
            settings=settings,
            profile_name=profile_name,
            allow_auto_exit=allow_auto_exit,
        )
        result = AutoExitTickResult(enabled_gates_ok=ok, notes=list(blockers))
        if not ok:
            return result

        max_exits = int(settings.max_auto_exits_per_tick)
        if max_exits <= 0:
            result.skipped.append("MAX_AUTO_EXITS_PER_TICK=0")
            return result

        edge = min_edge if min_edge is not None else settings.min_edge
        book = self._collect_books(persist=True)
        best_bids = {k: v["bid"] for k, v in book.items()}
        bid_depths = {k: v["depth"] for k, v in book.items() if v.get("depth") is not None}
        recommendations = self.guardian.evaluate(
            min_edge=edge, best_bids=best_bids, bid_depths=bid_depths
        )
        # Settlement core: only evidence/value-backed full exits may SELL.
        # Profit recovery, dust cleanup, hold, settlement, and review never sell.
        executable = [
            rec
            for rec in recommendations
            if rec.kind == "position"
            and rec.action in {"exit_full", "position_at_risk"}
        ]
        if not executable:
            result.notes.append("no executable exit recommendations")
            return result

        no_bid = [
            rec for rec in executable if rec.best_bid is None or rec.best_bid <= 0
        ]
        for rec in no_bid:
            note = f"{rec.market_id}: no best bid; auto-exit deferred without SELL attempt"
            result.skipped.append(note)
            try:
                self._record_no_bid_skip_once(
                    market_id=rec.market_id,
                    outcome=_normalize_outcome(rec.outcome),
                    size=Decimal(str(rec.actual_position_size or 0)),
                )
            except (TypeError, ValueError):
                result.notes.append(
                    f"{rec.market_id}: no-bid recommendation has incomplete position metadata"
                )

        executable = [
            rec for rec in executable if rec.best_bid is not None and rec.best_bid > 0
        ]
        executable.sort(key=_exit_priority)
        if not executable:
            result.notes.append("no executable exit recommendations with a live best bid")
            return result

        for rec in executable:
            if result.executed >= max_exits:
                result.skipped.append(
                    f"max_auto_exits_per_tick={max_exits} reached; skip {rec.market_id}"
                )
                continue
            try:
                outcome = self._execute_one(
                    rec=rec,
                    settings=settings,
                    profile_name=profile_name,
                    min_edge=edge,
                    on_submitted=on_submitted,
                    compliance_service=compliance_service,
                    result=result,
                )
                if outcome:
                    result.executed += 1
            except Exception as exc:
                failure = f"{rec.market_id}: {exc}"
                result.failures.append(failure)
                result.skipped.append(failure)
        return result

    def _collect_books(self, *, persist: bool = True) -> dict[tuple[str, str], dict[str, Any]]:
        """Fresh token books for nonzero positions; optionally persist snapshots for /app."""
        from polymarket_weather_arb.adapters.polymarket.translator import translate_market

        books: dict[tuple[str, str], dict[str, Any]] = {}
        for position in self.repository.list_positions(limit=1000, nonzero_only=True):
            market_id = str(position["market_id"])
            try:
                outcome = _normalize_outcome(position["outcome"])
            except ValueError:
                continue
            market_row = self.repository.get_market(market_id)
            if market_row is None:
                continue
            try:
                payload = json.loads(market_row["raw_payload"])
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            if not is_market_orderable(
                raw_payload=payload,
                title=market_row["title"],
                close_time=market_row["close_time"],
                check_target_date=True,
            ):
                continue
            try:
                market = translate_market(payload)
            except Exception:
                continue
            token_id = market.yes_token_id if outcome == "YES" else market.no_token_id
            if not token_id:
                continue
            try:
                snapshot, raw = self.client.get_token_order_book(token_id)
            except Exception:
                continue
            if snapshot.best_bid is None:
                continue
            price = snapshot.best_bid.quantize(_PRICE_TICK, rounding=ROUND_DOWN)
            if price <= 0:
                continue
            depth = best_bid_depth_from_book(raw if isinstance(raw, dict) else None)
            books[(market_id, outcome)] = {"bid": price, "depth": depth, "raw": raw}
            if persist:
                # Persist under market_id + token_id so YES/NO quotes never mix.
                self.repository.save_market_snapshot(
                    MarketSnapshot(
                        market_id=market_id,
                        best_bid=snapshot.best_bid,
                        best_ask=snapshot.best_ask,
                        midpoint=snapshot.midpoint,
                        spread=snapshot.spread,
                        liquidity=snapshot.liquidity,
                        fetched_at=snapshot.fetched_at or datetime.now(timezone.utc),
                        token_id=str(token_id),
                    ),
                    raw if isinstance(raw, dict) else {"token_id": token_id},
                    token_id=str(token_id),
                )
        return books

    def _execute_one(
        self,
        *,
        rec: ExitRecommendation,
        settings: Settings,
        profile_name: str,
        min_edge: Decimal,
        on_submitted: Callable[[int], None] | None,
        compliance_service: ComplianceService | None,
        result: AutoExitTickResult,
    ) -> bool:
        market_id = rec.market_id
        # Never auto-exit on missing/stale analysis (refresh failure or aged snapshot).
        analysis = self.repository.latest_analysis(market_id)
        if analysis is None:
            raise ValueError("analysis missing; auto-exit blocked")
        if not _analysis_is_fresh(analysis):
            raise ValueError("analysis stale; auto-exit blocked")
        outcome_raw = _normalize_outcome(rec.outcome)

        positions = self.repository.list_positions(market_id=market_id, nonzero_only=True)
        position = next(
            (p for p in positions if _normalize_outcome(p["outcome"]) == outcome_raw),
            None,
        )
        if position is None:
            raise ValueError(f"no nonzero position for {market_id} {outcome_raw}")

        position_size = Decimal(str(position["size"]))
        if position_size <= 0:
            raise ValueError("position size not positive; naked SELL forbidden")

        notional = Decimal(str(position["notional"] or 0))
        if notional <= 0:
            notional = position_size
        enforce_max_position = profile_name != "full-live"
        if enforce_max_position and notional > settings.auto_exit_max_position_usdc:
            raise ValueError(
                f"position notional {notional} exceeds AUTO_EXIT_MAX_POSITION_USDC "
                f"{settings.auto_exit_max_position_usdc}"
            )

        market_row = self.repository.get_market(market_id)
        if market_row is None:
            raise ValueError(f"market {market_id} not found")
        from polymarket_weather_arb.adapters.polymarket.translator import translate_market

        try:
            market_payload = json.loads(market_row["raw_payload"])
        except Exception:
            market_payload = {}
        if not isinstance(market_payload, dict):
            market_payload = {}
        market = translate_market(market_payload)
        token_id = market.yes_token_id if outcome_raw == "YES" else market.no_token_id
        if not token_id:
            raise ValueError(f"missing token for {outcome_raw}")

        # Second (pre-submit) book refresh — recompute policy with the same guardian.
        snapshot, raw_book = self.client.get_token_order_book(token_id)
        if snapshot.best_bid is None:
            note = f"{market_id}: no best bid; auto-exit deferred without SELL attempt"
            result.skipped.append(note)
            result.notes.append(note)
            self._record_no_bid_skip_once(
                market_id=market_id,
                outcome=outcome_raw,
                size=position_size,
            )
            return False
        price = snapshot.best_bid.quantize(_PRICE_TICK, rounding=ROUND_DOWN)
        if price <= 0 or price >= 1:
            raise ValueError(f"invalid planned sell price {price}")
        depth = best_bid_depth_from_book(raw_book if isinstance(raw_book, dict) else None)
        self.repository.save_market_snapshot(
            MarketSnapshot(
                market_id=market_id,
                best_bid=snapshot.best_bid,
                best_ask=snapshot.best_ask,
                midpoint=snapshot.midpoint,
                spread=snapshot.spread,
                liquidity=snapshot.liquidity,
                fetched_at=snapshot.fetched_at or datetime.now(timezone.utc),
                token_id=str(token_id),
            ),
            raw_book if isinstance(raw_book, dict) else {"token_id": token_id},
            token_id=str(token_id),
        )

        fresh_recs = self.guardian.evaluate(
            min_edge=min_edge,
            best_bids={(market_id, outcome_raw): price},
            bid_depths={(market_id, outcome_raw): depth} if depth is not None else None,
        )
        fresh = next(
            (
                r
                for r in fresh_recs
                if r.kind == "position"
                and r.market_id == market_id
                and _normalize_outcome(r.outcome) == outcome_raw
            ),
            None,
        )
        if fresh is None or fresh.action not in {
            "exit_full",
            "position_at_risk",
        }:
            note = (
                f"{market_id}: post-refresh policy={getattr(fresh, 'action', None)} "
                f"at bid={price}; no SELL"
            )
            result.skipped.append(note)
            result.notes.append(note)
            return False
        rec = fresh

        # V5 automatic exits are all-or-none. A partial size would recreate the
        # profit-recovery/dust path that settlement core explicitly supersedes.
        size = position_size

        constraints = extract_order_constraints(market_payload)
        is_dust, dust_reason = residual_is_dust(
            residual_size=size,
            price=price,
            constraints=constraints,
        )
        if is_dust:
            note = f"{market_id}: dust residual — {dust_reason}"
            result.skipped.append(note)
            result.notes.append(note)
            self._record_dust_skip_once(
                market_id=market_id,
                outcome=outcome_raw,
                size=size,
                price=price,
                reason=dust_reason,
            )
            return False

        planned_notional = price * size
        if enforce_max_position and planned_notional > settings.auto_exit_max_position_usdc:
            raise ValueError(
                f"planned notional {planned_notional} exceeds AUTO_EXIT_MAX_POSITION_USDC "
                f"{settings.auto_exit_max_position_usdc}"
            )

        if self._has_blocking_auto_exit_action(market_id=market_id, outcome=outcome_raw, size=size):
            result.skipped.append(
                f"{market_id}: active or identical residual auto-exit already recorded"
            )
            return False

        residual_key = _residual_key(market_id, outcome_raw, size, price)
        action = self._create_auto_exit_action(
            market_id=market_id,
            reason=rec.reason,
            preview=(
                f"auto-exit {rec.action} SELL {market_id} {outcome_raw} size={size} price={price}"
            ),
            residual_key=f"{residual_key}:{uuid.uuid4().hex[:8]}",
        )
        action_id = str(action["id"])
        result.action_ids.append(action_id)
        self.repository.append_automation_audit_event(
            action_id,
            "auto_exit_candidate",
            "auto-exit-service",
            {
                "market_id": market_id,
                "outcome": outcome_raw,
                "size": str(size),
                "price": str(price),
                "depth": str(depth) if depth is not None else None,
                "guardian_action": rec.action,
                "guardian_reason": rec.reason,
                "policy_stage": rec.policy_stage,
                "policy_version": rec.policy_version,
                "hold_value_upper": str(rec.hold_value_upper)
                if rec.hold_value_upper is not None
                else None,
                "net_sell_per_share": str(rec.net_sell_per_share)
                if rec.net_sell_per_share is not None
                else None,
                "recommended_size": str(rec.recommended_size)
                if rec.recommended_size is not None
                else None,
                "unrecovered_cash": str(rec.unrecovered_cash)
                if rec.unrecovered_cash is not None
                else None,
                "verified_buy_cost": str(rec.verified_buy_cost)
                if rec.verified_buy_cost is not None
                else None,
                "verified_sell_proceeds": str(rec.verified_sell_proceeds)
                if rec.verified_sell_proceeds is not None
                else None,
                "accounting_verified": rec.accounting_verified,
                "post_refresh_recompute": True,
            },
        )

        result.attempted += 1
        try:
            sell_result = self.exit_service.close_live(
                settings=settings,
                market_id=market_id,
                outcome=outcome_raw,
                price=price,
                size=size,
                size_text=format(size, "f"),
                max_slippage=settings.auto_exit_max_slippage,
                confirm=None,
                compliance_service=compliance_service,
                on_submitted=on_submitted,
                auto_exit=True,
                auto_exit_profile_name=profile_name,
                auto_rationale=(
                    f"guardian={rec.action}; exit_policy={rec.policy_version}; {rec.reason}"
                ),
            )
        except Exception as exc:
            # Any exception after action creation must terminalize the action.
            self._finalize_action(
                action_id,
                status="failed",
                summary=f"auto-exit exception after create: {exc}",
                failure_reason=str(exc),
            )
            self.repository.append_automation_audit_event(
                action_id,
                "auto_exit_failed",
                "auto-exit-service",
                {"error": str(exc), "phase": "close_live_exception"},
            )
            raise

        intent_id = sell_result.get("intent_id")
        if intent_id is not None:
            result.intent_ids.append(int(intent_id))

        if sell_result.get("ok"):
            result.submissions.append(
                {
                    "ok": True,
                    "status": sell_result.get("status") or "submitted",
                    "verified": sell_result.get("verified"),
                    "intent_id": intent_id,
                    "order_id": sell_result.get("order_id"),
                    "market_id": market_id,
                    "side": "SELL",
                    "outcome": outcome_raw,
                    "price": sell_result.get("price")
                    if sell_result.get("price") is not None
                    else price,
                    "size": sell_result.get("size")
                    if sell_result.get("size") is not None
                    else size,
                    "warning": sell_result.get("warning"),
                    "guardian_action": rec.action,
                    "policy_stage": rec.policy_stage,
                    "policy_version": rec.policy_version,
                }
            )

        event = "auto_exit_submitted" if sell_result.get("ok") else "auto_exit_failed"
        self.repository.append_automation_audit_event(
            action_id,
            event,
            "auto-exit-service",
            {
                "ok": sell_result.get("ok"),
                "verified": sell_result.get("verified"),
                "status": sell_result.get("status"),
                "intent_id": intent_id,
                "order_id": sell_result.get("order_id"),
                "warning": sell_result.get("warning"),
                "error": sell_result.get("error"),
            },
        )
        status = "executed" if sell_result.get("ok") else "failed"
        summary = f"auto-exit intent={intent_id} status={sell_result.get('status')}"
        self._finalize_action(
            action_id,
            status=status,
            summary=summary,
            failure_reason=str(sell_result.get("error") or sell_result.get("status") or "")
            if not sell_result.get("ok")
            else None,
            return_code=0 if sell_result.get("ok") else 1,
        )
        if not sell_result.get("ok"):
            failure = (
                f"{market_id}: "
                f"{sell_result.get('error') or sell_result.get('status') or 'SELL failed'}"
            )
            result.failures.append(failure)
            result.skipped.append(failure)
        return bool(sell_result.get("ok"))

    def _create_auto_exit_action(
        self,
        *,
        market_id: str,
        reason: str,
        preview: str,
        residual_key: str | None = None,
    ) -> Any:
        now = datetime.now(timezone.utc)
        action = AutomationAction(
            id=f"auto_exit_{uuid.uuid4().hex[:16]}",
            kind="auto_exit",
            market_id=market_id,
            reason=reason,
            command_preview=preview,
            idempotency_key=residual_key or f"auto-exit:{market_id}:{now.isoformat()}",
            requested_by="auto-exit-service",
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )
        return self.repository.create_automation_action(action)

    def _record_dust_skip_once(
        self,
        *,
        market_id: str,
        outcome: str,
        size: Decimal,
        price: Decimal,
        reason: str,
    ) -> None:
        """Persist a terminal skipped dust action once; subsequent ticks no-op."""
        residual_key = f"auto-exit-dust:{market_id}:{outcome}:{format(size, 'f')}"
        existing = self.repository.connection.execute(
            """
            SELECT id, status FROM automation_actions
            WHERE kind = 'auto_exit' AND market_id = ? AND idempotency_key = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (market_id, residual_key),
        ).fetchone()
        if existing is not None:
            return
        now = datetime.now(timezone.utc)
        action = AutomationAction(
            id=f"auto_exit_dust_{uuid.uuid4().hex[:12]}",
            kind="auto_exit",
            market_id=market_id,
            reason=f"dust residual: {reason}",
            command_preview=f"skip dust SELL {market_id} {outcome} size={size} price={price}",
            idempotency_key=residual_key,
            requested_by="auto-exit-service",
            created_at=now,
            expires_at=now + timedelta(days=7),
        )
        created = self.repository.create_automation_action(action)
        action_id = str(created["id"])
        self.repository.append_automation_audit_event(
            action_id,
            "auto_exit_dust_residual",
            "auto-exit-service",
            {
                "market_id": market_id,
                "outcome": outcome,
                "size": str(size),
                "price": str(price),
                "reason": reason,
            },
        )
        self._finalize_action(
            action_id,
            status=_DUST_SKIP_STATUS,
            summary=f"dust residual not sellable: {reason}",
            failure_reason=reason,
            return_code=0,
        )

    def _has_blocking_auto_exit_action(
        self, *, market_id: str, outcome: str, size: Decimal
    ) -> bool:
        # Any still-active auto-exit for this market blocks another create.
        active = self.repository.connection.execute(
            """
            SELECT id, status FROM automation_actions
            WHERE kind = 'auto_exit' AND market_id = ?
              AND status IN ('pending', 'approved', 'running')
            ORDER BY created_at DESC LIMIT 1
            """,
            (market_id,),
        ).fetchone()
        if active is not None:
            return True
        # Same residual size already attempted this analysis window: suppress spam.
        # Terminal failed/executed with identical residual_key within recent window.
        residual_prefix = f"auto-exit-residual:{market_id}:{outcome}:{format(size, 'f')}:"
        recent = self.repository.connection.execute(
            """
            SELECT id, status, created_at FROM automation_actions
            WHERE kind = 'auto_exit' AND market_id = ?
              AND idempotency_key LIKE ?
              AND created_at >= datetime('now', '-30 minutes')
            ORDER BY created_at DESC LIMIT 1
            """,
            (market_id, residual_prefix + "%"),
        ).fetchone()
        return recent is not None

    def _record_no_bid_skip_once(
        self,
        *,
        market_id: str,
        outcome: str,
        size: Decimal,
    ) -> None:
        """Persist illiquidity once without classifying it as execution failure."""
        key = f"auto-exit-no-bid:{market_id}:{outcome}"
        existing = self.repository.connection.execute(
            """
            SELECT id FROM automation_actions
            WHERE kind = 'auto_exit' AND market_id = ? AND idempotency_key = ?
            LIMIT 1
            """,
            (market_id, key),
        ).fetchone()
        if existing is not None:
            return
        now = datetime.now(timezone.utc)
        action = AutomationAction(
            id=f"auto_exit_no_bid_{uuid.uuid4().hex[:12]}",
            kind="auto_exit",
            market_id=market_id,
            reason="no best bid; auto-exit deferred until executable liquidity returns",
            command_preview=f"skip SELL {market_id} {outcome} size={size}: no best bid",
            idempotency_key=key,
            requested_by="auto-exit-service",
            created_at=now,
            expires_at=now + timedelta(days=7),
        )
        created = self.repository.create_automation_action(action)
        action_id = str(created["id"])
        self.repository.append_automation_audit_event(
            action_id,
            "auto_exit_no_bid",
            "auto-exit-service",
            {
                "market_id": market_id,
                "outcome": outcome,
                "size": str(size),
                "reason": "no best bid",
            },
        )
        self._finalize_action(
            action_id,
            status="skipped",
            summary="auto-exit deferred: no executable best bid",
            failure_reason="no best bid",
            return_code=0,
        )

    def _finalize_action(
        self,
        action_id: str,
        *,
        status: str,
        summary: str,
        failure_reason: str | None = None,
        return_code: int = 1,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.repository.connection.execute(
            """
            UPDATE automation_actions
            SET status = ?, result_summary = ?, return_code = ?, updated_at = ?,
                executed_at = CASE WHEN ? = 'executed' THEN ? ELSE executed_at END,
                failed_at = CASE
                    WHEN ? IN ('failed', 'skipped') THEN ?
                    ELSE failed_at
                END,
                failure_reason = CASE
                    WHEN ? IN ('failed', 'skipped') THEN COALESCE(?, failure_reason)
                    ELSE failure_reason
                END
            WHERE id = ?
            """,
            (
                status,
                summary,
                return_code,
                now,
                status,
                now,
                status,
                now,
                status,
                failure_reason,
                action_id,
            ),
        )

    @staticmethod
    def _gates_open(
        *,
        settings: Settings,
        profile_name: str,
        allow_auto_exit: bool,
    ) -> tuple[bool, list[str]]:
        blockers: list[str] = []
        if profile_name != "full-live" and not settings.auto_exit_enabled:
            blockers.append("AUTO_EXIT_ENABLED=false")
        if not allow_auto_exit:
            blockers.append("daemon allow_auto_exit=false")
        if profile_name not in _AUTO_EXIT_PROFILES:
            blockers.append(
                f"profile={profile_name} is not an auto-exit live profile "
                f"(allowed: {', '.join(sorted(_AUTO_EXIT_PROFILES))})"
            )
        if settings.trading_disabled:
            blockers.append("TRADING_DISABLED=true")
        return (not blockers), blockers


def _normalize_outcome(value: object) -> str:
    text = str(value or "").strip().upper()
    if text in {"YES", "Y"}:
        return "YES"
    if text in {"NO", "N"}:
        return "NO"
    raise ValueError(f"unsupported outcome for auto-exit: {value!r}")


def _analysis_is_fresh(analysis: Any, *, max_age: timedelta = _ANALYSIS_MAX_AGE) -> bool:
    try:
        created_at = analysis["created_at"]
    except (KeyError, IndexError, TypeError):
        return False
    if not created_at:
        return False
    try:
        parsed = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - parsed <= max_age


def _residual_key(market_id: str, outcome: str, size: Decimal, price: Decimal | None = None) -> str:
    price_part = format(price, "f") if price is not None else "na"
    return f"auto-exit-residual:{market_id}:{outcome}:{format(size, 'f')}:{price_part}"
