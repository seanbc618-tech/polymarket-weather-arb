from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Protocol

from polymarket_weather_arb.adapters.polymarket.base import PolymarketClient
from polymarket_weather_arb.adapters.weather.open_meteo import OpenMeteoProvider
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.profiles import StrategyProfile, settings_for_profile
from polymarket_weather_arb.services.automation_service import AutomationService
from polymarket_weather_arb.services.circuit_breaker_service import (
    CircuitBreakerService,
    live_execution_blocked,
)
from polymarket_weather_arb.services.compliance_service import ComplianceService
from polymarket_weather_arb.services.discovery_service import DiscoveryService
from polymarket_weather_arb.services.live_monitor_service import live_action_gates
from polymarket_weather_arb.services.market_workflow_service import MarketWorkflowService
from polymarket_weather_arb.services.reconciliation_service import ReconciliationService
from polymarket_weather_arb.services.resolution_audit_service import ResolutionAuditService
from polymarket_weather_arb.storage.repositories import Repository


class SleepFn(Protocol):
    def __call__(self, seconds: float) -> None: ...


NotifyFn = Callable[[dict[str, object]], None]


@dataclass(frozen=True)
class RiskGuardResult:
    status: str
    reconciliation_fresh: bool
    latest_reconciliation_status: str | None
    daily_live_notional: Decimal
    market_exposures: dict[str, Decimal]
    anomalies: list[str]


@dataclass(frozen=True)
class OrderMonitorResult:
    reconciliation_status: str | None
    open_orders_count: int
    positions_count: int
    nonzero_positions_count: int
    fills_count: int
    notes: list[str]


@dataclass(frozen=True)
class DaemonTickResult:
    discovered: int
    proposed_action_id: str | None
    proposed_kind: str | None
    auto_executed_action_ids: list[str]
    auto_live_executed_action_ids: list[str]
    skipped_live_action_ids: list[str]
    audited_markets: list[str]
    risk_status: str
    risk_anomalies: list[str]
    reconciliation_status: str | None
    reconciliation_fresh: bool
    open_orders_count: int
    positions_count: int
    nonzero_positions_count: int
    fills_count: int
    notifications_sent: list[str]
    notes: list[str]
    auto_exit_executed: int = 0
    auto_exit_attempted: int = 0
    auto_exit_skipped: list[str] = field(default_factory=list)
    auto_exit_armed: bool = False


class OperatorDaemon:
    def __init__(
        self,
        *,
        repository: Repository,
        client: PolymarketClient,
        profile: StrategyProfile,
        settings: Settings | None = None,
        dry_run_only: bool = True,
        notifier: NotifyFn | None = None,
        notify_force: bool = False,
        allow_live_auto: bool = False,
        live_market_ids: set[str] | None = None,
        max_live_actions_per_tick: int = 1,
        require_fresh_reconciliation: bool = True,
        block_live_on_positions: bool = True,
        allow_auto_exit: bool = False,
        live_whitelist_open: bool = False,
    ) -> None:
        self.repository = repository
        self.client = client
        self.profile = profile
        self.settings = settings_for_profile(settings or Settings(), profile)
        self.dry_run_only = dry_run_only
        self.notifier = notifier
        self.notify_force = notify_force
        self.allow_live_auto = allow_live_auto
        self.live_market_ids = live_market_ids or set()
        self.max_live_actions_per_tick = max_live_actions_per_tick
        self.require_fresh_reconciliation = require_fresh_reconciliation
        self.block_live_on_positions = block_live_on_positions
        # Separate from live entry auto: exit is default-off and dual-gated.
        self.allow_auto_exit = allow_auto_exit
        # full-auto without LIVE_MARKET_IDS: open whitelist for small weather universe
        self.live_whitelist_open = live_whitelist_open
        self.automation = AutomationService(
            repository,
            workflow_factory=lambda repo: MarketWorkflowService(
                self.settings,
                repo,
                weather_provider_factory=OpenMeteoProvider,
                polymarket_client_factory=lambda settings: self.client,
            ),
            settings=self.settings,
        )

    def tick(
        self,
        *,
        discover: bool = True,
        propose: bool = True,
        auto_dry_run: bool = True,
        risk_guard: bool = True,
        include_reconciliation: bool = False,
        auto_live: bool = False,
        audit: bool = True,
    ) -> DaemonTickResult:
        notes: list[str] = []
        notifications_sent: list[str] = []
        monitor = self.order_monitor(include_reconciliation=include_reconciliation)
        reconciliation_status = monitor.reconciliation_status
        notes.extend(monitor.notes)

        risk = self.risk_guard() if risk_guard else self._risk_guard_disabled()
        self._notify_risk_if_needed(risk, notifications_sent)

        discovered = 0
        if discover:
            discovered = DiscoveryService(self.client, self.repository).discover(
                limit=self.profile.discovery_limit,
                pages=self.profile.discovery_pages,
            )
            notes.append(f"discovered={discovered}")
            if self.notifier and discovered:
                self._notify(
                    "daemon_discovery",
                    build_discovery_notification(discovered, self.profile.name),
                    notifications_sent,
                )

        audited_markets: list[str] = []
        if audit:
            audit_service = ResolutionAuditService(
                self.repository, self.client, CircuitBreakerService(self.repository)
            )
            for m_id in self.repository.get_markets_needing_resolution_audit(limit=100):
                audit_service.audit_cached_market(m_id)
                audited_markets.append(m_id)
            for event_slug in self.repository.get_event_slugs_needing_resolution_audit(limit=2):
                results = audit_service.audit_event(event_slug)
                audited_markets.extend(result.market_id for result in results)
            if audited_markets:
                notes.append(f"audited={len(audited_markets)}")

        proposed_action_id = None
        proposed_kind = None
        # Expire stale queue items first — otherwise ancient pending demo actions
        # permanently block propose (expires_at in the past but status still pending).
        expired_n = self.repository.expire_automation_actions(
            datetime.now(timezone.utc).isoformat()
        )
        if expired_n:
            notes.append(f"expired_stale_actions={expired_n}")
        if (
            propose
            and self.repository.latest_action_by_status("pending", "approved", "executing") is None
        ):
            candidate = self.repository.next_dry_run_ready_candidate()
            if candidate is None:
                notes.append("no dry_run_ready candidate")
            else:
                proposed_kind = (
                    "dry_run" if self.dry_run_only else self.profile.normalized_action_kind()
                )
                action = self.automation.propose_next(
                    kind=proposed_kind,
                    reason=f"operator daemon {self.profile.name} tick",
                    ttl_minutes=self.profile.action_ttl_minutes,
                    requested_by=f"operator-daemon:{self.profile.name}",
                )
                self.repository.append_automation_audit_event(
                    action["id"],
                    "daemon_proposed",
                    "operator-daemon",
                    {"profile": self.profile.name, "dry_run_only": self.dry_run_only},
                )
                proposed_action_id = action["id"]
                self._notify(
                    "daemon_proposal",
                    build_proposal_notification(action, self.profile.name),
                    notifications_sent,
                )
        elif propose:
            blocker = self.repository.latest_action_by_status("pending", "approved", "executing")
            if blocker is not None:
                notes.append(
                    f"active action already exists id={blocker['id']} "
                    f"kind={blocker['kind']} market={blocker['market_id']} "
                    f"status={blocker['status']}"
                )
            else:
                notes.append("active action already exists")

        auto_executed_action_ids: list[str] = []
        skipped_live_action_ids: list[str] = []
        if auto_dry_run:
            for action in self.repository.list_automation_actions(
                limit=20, status="pending", kind="dry_run"
            ):
                approved = self.automation.approve(
                    action["id"], f"operator-daemon:{self.profile.name}"
                )
                if approved["status"] == "approved":
                    result = self.automation.execute(approved["id"])
                    auto_executed_action_ids.append(result["id"])
            if auto_executed_action_ids:
                self._notify(
                    "daemon_dry_run",
                    build_execution_notification(
                        auto_executed_action_ids, "dry_run", self.profile.name
                    ),
                    notifications_sent,
                )

        auto_live_executed_action_ids = self._execute_live_actions(
            enabled=auto_live,
            risk=risk,
            skipped_live_action_ids=skipped_live_action_ids,
        )
        for action in self.repository.list_automation_actions(limit=20, status="pending"):
            if action["kind"] != "dry_run" and action["id"] not in skipped_live_action_ids:
                skipped_live_action_ids.append(action["id"])
        if auto_live_executed_action_ids:
            self._notify(
                "daemon_live",
                build_execution_notification(
                    auto_live_executed_action_ids, "trade_live", self.profile.name
                ),
                notifications_sent,
            )

        auto_exit_result = self._run_auto_exit()
        if auto_exit_result.notes:
            notes.extend(f"auto_exit:{n}" for n in auto_exit_result.notes)
        if auto_exit_result.skipped:
            notes.extend(f"auto_exit_skip:{s}" for s in auto_exit_result.skipped)
        if auto_exit_result.executed:
            notes.append(f"auto_exit_executed={auto_exit_result.executed}")
            self._notify(
                "daemon_auto_exit",
                {
                    "kind": "review",
                    "role": "trader",
                    "project": "polymarket-weather-arb",
                    "status": "executed",
                    "summary": (
                        f"自动平仓已提交 {auto_exit_result.executed} 笔卖出 "
                        f"尝试={auto_exit_result.attempted}"
                    ),
                    "items": [
                        f"策略={self.profile.name}",
                        f"成功={auto_exit_result.executed}",
                        f"尝试={auto_exit_result.attempted}",
                        f"意图ID={auto_exit_result.intent_ids}",
                        f"动作ID={auto_exit_result.action_ids}",
                        *auto_exit_result.skipped[:5],
                    ],
                },
                notifications_sent,
            )

        self._notify(
            "daemon_tick",
            build_tick_notification(
                discovered=discovered,
                proposed_action_id=proposed_action_id,
                proposed_kind=proposed_kind,
                auto_executed_action_ids=auto_executed_action_ids,
                auto_live_executed_action_ids=auto_live_executed_action_ids,
                skipped_live_action_ids=skipped_live_action_ids,
                audited_markets=audited_markets,
                risk=risk,
                monitor=monitor,
                profile=self.profile.name,
                auto_exit_executed=auto_exit_result.executed,
                auto_exit_attempted=auto_exit_result.attempted,
                auto_exit_armed=auto_exit_result.enabled_gates_ok,
            ),
            notifications_sent,
        )

        return DaemonTickResult(
            discovered=discovered,
            proposed_action_id=proposed_action_id,
            proposed_kind=proposed_kind,
            auto_executed_action_ids=auto_executed_action_ids,
            auto_live_executed_action_ids=auto_live_executed_action_ids,
            skipped_live_action_ids=skipped_live_action_ids,
            audited_markets=audited_markets,
            risk_status=risk.status,
            risk_anomalies=risk.anomalies,
            reconciliation_status=reconciliation_status or risk.latest_reconciliation_status,
            reconciliation_fresh=risk.reconciliation_fresh,
            open_orders_count=monitor.open_orders_count,
            positions_count=monitor.positions_count,
            nonzero_positions_count=monitor.nonzero_positions_count,
            fills_count=monitor.fills_count,
            notifications_sent=notifications_sent,
            notes=notes,
            auto_exit_executed=auto_exit_result.executed,
            auto_exit_attempted=auto_exit_result.attempted,
            auto_exit_skipped=list(auto_exit_result.skipped),
            auto_exit_armed=auto_exit_result.enabled_gates_ok,
        )

    def _run_auto_exit(self):
        from polymarket_weather_arb.services.auto_exit_service import AutoExitService

        service = AutoExitService(self.repository, self.client)
        return service.run_tick(
            settings=self.settings,
            profile_name=self.profile.name,
            allow_auto_exit=self.allow_auto_exit,
            on_submitted=lambda _intent_id: self.repository.connection.commit(),
        )

    def run(
        self,
        *,
        tick_seconds: int,
        max_ticks: int | None = None,
        sleep: SleepFn = time.sleep,
    ) -> list[DaemonTickResult]:
        from polymarket_weather_arb.services.autopilot_service import _now_iso

        self.repository.update_autopilot_state(process_started_at=_now_iso())

        results = []
        tick_count = 0
        while max_ticks is None or tick_count < max_ticks:
            results.append(self.tick())
            tick_count += 1
            self.repository.connection.commit()
            if max_ticks is not None and tick_count >= max_ticks:
                break
            sleep(tick_seconds)
        return results

    def order_monitor(self, *, include_reconciliation: bool) -> OrderMonitorResult:
        notes = []
        reconciliation_status = None
        if include_reconciliation:
            reconciliation = ReconciliationService(self.client, self.repository).reconcile()
            reconciliation_status = str(reconciliation.get("status", "unknown"))
            notes.append(f"reconciliation={reconciliation_status}")
        open_orders_count = len(self.repository.list_open_orders(limit=100))
        positions_count = len(self.repository.list_positions(limit=100))
        nonzero_positions_count = self.repository.nonzero_positions_count()
        fills_count = len(self.repository.list_fills(limit=100))
        notes.append(
            f"open_orders={open_orders_count} positions={positions_count} "
            f"nonzero_positions={nonzero_positions_count} fills={fills_count}"
        )
        return OrderMonitorResult(
            reconciliation_status=reconciliation_status,
            open_orders_count=open_orders_count,
            positions_count=positions_count,
            nonzero_positions_count=nonzero_positions_count,
            fills_count=fills_count,
            notes=notes,
        )

    def risk_guard(self) -> RiskGuardResult:
        today = datetime.now(timezone.utc).date().isoformat()
        daily = self.repository.daily_order_notional(today)
        markets = self.repository.list_weather_markets()
        exposures = {row["id"]: self.repository.market_exposure(row["id"]) for row in markets}
        latest_reconciliation = self.repository.latest_reconciliation()
        latest_success = self.repository.latest_successful_reconciliation()
        latest_failed = self.repository.latest_failed_action()
        anomalies = []
        latest_status = latest_reconciliation["status"] if latest_reconciliation else None
        reconciliation_fresh = _fresh_reconciliation(latest_success)
        if latest_success is None:
            anomalies.append("no successful reconciliation")
        elif not reconciliation_fresh:
            anomalies.append("successful reconciliation is stale")
        if latest_status and latest_status != "ok":
            anomalies.append(f"latest reconciliation status is {latest_status}")
        # Only recent failures block live auto. Ancient demo/dashboard failures
        # (weeks old) must not permanently freeze trade_live execution.
        if latest_failed is not None and _is_recent_timestamp(
            latest_failed["failed_at"]
            or latest_failed["updated_at"]
            or latest_failed["created_at"],
            max_age_seconds=24 * 3600,
        ):
            anomalies.append(f"failed action waiting for inspection: {latest_failed['id']}")
        nonzero_positions = self.repository.nonzero_positions_count()
        if self.block_live_on_positions and nonzero_positions:
            anomalies.append(f"nonzero positions present: {nonzero_positions}")
        if daily > self.settings.max_daily_usdc:
            anomalies.append(f"daily live notional exceeds {self.settings.max_daily_usdc}")
        for market_id, exposure in exposures.items():
            if exposure > self.settings.max_market_usdc:
                anomalies.append(
                    f"market {market_id} exposure exceeds {self.settings.max_market_usdc}"
                )
        return RiskGuardResult(
            status="warn" if anomalies else "ok",
            reconciliation_fresh=reconciliation_fresh,
            latest_reconciliation_status=latest_status,
            daily_live_notional=daily,
            market_exposures=exposures,
            anomalies=anomalies,
        )

    def _risk_guard_disabled(self) -> RiskGuardResult:
        return RiskGuardResult(
            status="disabled",
            reconciliation_fresh=False,
            latest_reconciliation_status=None,
            daily_live_notional=Decimal("0"),
            market_exposures={},
            anomalies=[],
        )

    def _execute_live_actions(
        self,
        *,
        enabled: bool,
        risk: RiskGuardResult,
        skipped_live_action_ids: list[str],
    ) -> list[str]:
        executed = []
        if not enabled:
            return executed
        pending_live_actions = self.repository.list_automation_actions(
            limit=20, status="pending", kind="trade_live"
        )
        compliance = ComplianceService(self.settings).check_live_allowed()
        blocker = live_execution_blocked(self.repository)
        if not compliance.ok or blocker:
            skipped_live_action_ids.extend(action["id"] for action in pending_live_actions)
            return executed
        for action in pending_live_actions:
            if len(executed) >= self.max_live_actions_per_tick:
                skipped_live_action_ids.append(action["id"])
                continue
            can_live, gate_notes = self._can_auto_live(action, risk)
            if not can_live:
                skipped_live_action_ids.append(action["id"])
                # Surface first failed gate for Telegram/tick notes.
                self.repository.append_automation_audit_event(
                    action["id"],
                    "live_auto_skipped",
                    "operator-daemon",
                    {"gates": gate_notes, "risk_status": risk.status},
                )
                continue
            approved = self.automation.approve(action["id"], f"operator-daemon:{self.profile.name}")
            if approved["status"] == "approved":
                result = self.automation.execute(approved["id"])
                executed.append(result["id"])
        return executed

    def _can_auto_live(self, action, risk: RiskGuardResult) -> tuple[bool, list[str]]:
        gates = live_action_gates(
            self.repository,
            action,
            profile=self.profile,
            allow_live_auto=self.allow_live_auto,
            live_market_ids=self.live_market_ids,
            require_fresh_reconciliation=self.require_fresh_reconciliation,
            reconciliation_fresh=risk.reconciliation_fresh,
            risk_status=risk.status,
            dry_run_only=self.dry_run_only,
            live_whitelist_open=self.live_whitelist_open,
        )
        failed = [
            f"{g.name}: {g.reason}" + (f" ({g.detail})" if g.detail else "")
            for g in gates
            if not g.ok
        ]
        return (not failed), failed

    def _notify_risk_if_needed(self, risk: RiskGuardResult, notifications_sent: list[str]) -> None:
        if risk.anomalies:
            self._notify(
                "daemon_risk", build_risk_notification(risk, self.profile.name), notifications_sent
            )

    def _notify(self, name: str, payload: dict[str, object], notifications_sent: list[str]) -> None:
        if self.notifier is None:
            return
        payload = {**payload, "daemon_event": name, "notify_force": self.notify_force}
        self.notifier(payload)
        notifications_sent.append(name)


def build_discovery_notification(discovered: int, profile: str) -> dict[str, object]:
    return {
        "kind": "discovery",
        "role": "scanner",
        "project": "polymarket-weather-arb",
        "status": "ok",
        "summary": f"发现 {discovered} 个天气市场",
        "items": [f"策略={profile}", f"发现数={discovered}"],
    }


def build_proposal_notification(action, profile: str) -> dict[str, object]:
    return {
        "kind": "proposal",
        "role": "captain",
        "project": "polymarket-weather-arb",
        "status": "needs_human_approval" if action["kind"] != "dry_run" else "auto_dry_run_pending",
        "summary": f"提案 {action['kind']} 市场={action['market_id']}",
        "action_id": action["id"],
        "market": action["market_id"],
        "items": [
            f"策略={profile}",
            f"动作ID={action['id']}",
            "模式=仅审批；人工确认后执行",
        ],
    }


def build_execution_notification(
    action_ids: list[str], kind: str, profile: str
) -> dict[str, object]:
    live = kind != "dry_run"
    kind_zh = "实盘" if live else "模拟"
    return {
        "kind": "review" if live else "dry_run",
        "role": "captain" if live else "trader",
        "project": "polymarket-weather-arb",
        "status": "executed",
        "summary": f"已执行 {len(action_ids)} 个{kind_zh}动作 ({kind})",
        "items": [f"策略={profile}", *[f"动作ID={action_id}" for action_id in action_ids]],
    }


def build_risk_notification(risk: RiskGuardResult, profile: str) -> dict[str, object]:
    return {
        "kind": "risk_report",
        "role": "risk",
        "project": "polymarket-weather-arb",
        "status": risk.status,
        "summary": "; ".join(risk.anomalies[:3]) or "风险检查正常",
        "items": [
            f"策略={profile}",
            f"对账新鲜={risk.reconciliation_fresh}",
            *risk.anomalies,
        ],
    }


def build_tick_notification(
    *,
    discovered: int,
    proposed_action_id: str | None,
    proposed_kind: str | None,
    auto_executed_action_ids: list[str],
    auto_live_executed_action_ids: list[str],
    skipped_live_action_ids: list[str],
    audited_markets: list[str],
    risk: RiskGuardResult,
    monitor: OrderMonitorResult,
    profile: str,
    auto_exit_executed: int = 0,
    auto_exit_attempted: int = 0,
    auto_exit_armed: bool = False,
) -> dict[str, object]:
    return {
        "kind": "review",
        "role": "reviewer",
        "project": "polymarket-weather-arb",
        "status": risk.status,
        "summary": f"周期摘要 策略={profile} 发现={discovered} 风险={risk.status}",
        "items": [
            f"策略={profile}",
            f"发现={discovered}",
            f"提案动作={proposed_action_id}",
            f"提案类型={proposed_kind}",
            f"自动模拟执行={len(auto_executed_action_ids)}",
            f"自动实盘执行={len(auto_live_executed_action_ids)}",
            f"跳过实盘={len(skipped_live_action_ids)}",
            f"自动平仓已武装={auto_exit_armed}",
            f"自动平仓成功={auto_exit_executed}",
            f"自动平仓尝试={auto_exit_attempted}",
            f"审计市场={len(audited_markets)}",
            f"对账新鲜={risk.reconciliation_fresh}",
            f"挂单={monitor.open_orders_count}",
            f"持仓={monitor.positions_count}",
            f"非零持仓={monitor.nonzero_positions_count}",
            f"成交={monitor.fills_count}",
        ],
    }


def _fresh_reconciliation(row) -> bool:
    if row is None:
        return False
    created_at = datetime.fromisoformat(row["created_at"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created_at).total_seconds() <= 300


def _is_recent_timestamp(value: str | None, *, max_age_seconds: int) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds() <= max_age_seconds
