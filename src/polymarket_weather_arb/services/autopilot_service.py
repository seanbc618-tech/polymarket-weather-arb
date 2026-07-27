from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Protocol

from polymarket_weather_arb.adapters.http_reader import open_meteo_cooldown_remaining
from polymarket_weather_arb.adapters.polymarket.client import GammaPolymarketClient
from polymarket_weather_arb.adapters.weather.noaa import NoaaProvider
from polymarket_weather_arb.adapters.weather.open_meteo import OpenMeteoProvider
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.execution import (
    live_order_opportunity_key,
    minimum_buy_cash_required,
    preflight_buy_rejection_reason,
)
from polymarket_weather_arb.domain.market_eligibility import evaluate_market_orderability
from polymarket_weather_arb.domain.rules import (
    enrich_rule_from_market_title,
    event_date_from_market_title,
    parse_resolution_rule,
)
from polymarket_weather_arb.domain.strategy_versions import (
    GLOBAL_BUCKET_MODEL_VERSION,
    WEATHER_ENTRY_POLICY_VERSION,
    WEATHER_V5_LIVE_MIN_EDGE,
    WEATHER_V5_LIVE_MIN_PRICE,
)
from polymarket_weather_arb.domain.global_temperature_bucket import (
    parse_global_temperature_bucket_rule,
    with_settlement_timezone,
)
from polymarket_weather_arb.profiles import (
    get_profile,
    live_auto_enabled_by_override,
    settings_for_override,
    settings_for_profile,
)
from polymarket_weather_arb.services.circuit_breaker_service import live_execution_blocked
from polymarket_weather_arb.services.calibration_service import CalibrationService
from polymarket_weather_arb.services.compliance_service import ComplianceService
from polymarket_weather_arb.services.discovery_service import DiscoveryService
from polymarket_weather_arb.services.market_workflow_service import (
    MarketWorkflowService,
    analysis_from_row,
    risk_context,
)
from polymarket_weather_arb.adapters.llm.factory import llm_runtime_label
from polymarket_weather_arb.domain.llm_decision import LlmTradeDecision
from polymarket_weather_arb.logging_config import redact_text
from polymarket_weather_arb.services.llm_advisor_service import LlmAdvisorService
from polymarket_weather_arb.services.live_launchpad_service import live_market_ids_from_settings
from polymarket_weather_arb.services.reconciliation_service import ReconciliationService
from polymarket_weather_arb.services.trading_service import TradingService, age_seconds
from polymarket_weather_arb.storage.repositories import Repository

logger = logging.getLogger(__name__)

SleepFn = Callable[[float], None]
MonotonicFn = Callable[[], float]
NotifyFn = Callable[[dict[str, object]], None]
APP_MODES = {"observe", "paper", "micro_live", "full_live"}
DEFAULT_APP_MODE = "paper"


def remaining_cycle_delay(
    *, tick_seconds: int, cycle_started: float, monotonic: MonotonicFn
) -> float:
    elapsed = max(0.0, monotonic() - cycle_started)
    return max(0.0, tick_seconds - elapsed)


# Multi-cadence pulse (Phase 2). Strategy cruise remains on autopilot_state.tick_seconds
# for slow work; the background wake is always this short interval.
PULSE_SECONDS = 2
# Without an exchange WebSocket, periodic reconciliation remains the account-state
# backstop. Three minutes keeps it comfortably inside the freshness window while
# avoiding the prior full account read every 20-60 seconds. Any mutation still
# schedules an immediate reconciliation.
CAPITAL_INTERVAL_SECONDS = 180
EXIT_INTERVAL_SECONDS = 180
DISCOVERY_INTERVAL_SECONDS = 420  # ~7m mid of 5–10m
# This cadence refreshes D0 observations. Forecast providers use horizon-aware
# 2-hour (D0) / 6-hour (later) caches, so a 5-minute wake does not imply an
# upstream ensemble/Google request.
WEATHER_REFRESH_INTERVAL_SECONDS = 300
GLOBAL_BUCKET_CANDIDATE_SCAN_LIMIT = 5000
# REST-backed reprice interval when exchange stream quotes are unavailable.
# With a live Market Channel, meaningful BBO changes schedule an immediate
# pending group reprice instead of waiting for this timer.
# One complete bucket group can contain 8-12 sibling markets. Repricing more
# often than once a minute turns harmless BBO churn into a REST/CPU storm while
# adding little strategy value. The 2s stream pulse still persists UI quotes;
# final live submission still performs its unconditional REST verification.
REPRICE_INTERVAL_SECONDS = 60
PORTFOLIO_DIGEST_INTERVAL_SECONDS = 4 * 60 * 60
# How often capital/slow pulses recompute the derived Market Channel token set.
STREAM_SUBSCRIPTION_SYNC_SECONDS = 60
# Periodic REST re-check for stream-subscribed tokens (even when BBO looks fresh).
# A full fair rotation spans roughly 10-15 minutes at the current group count.
# Stream BBOs remain freshness-gated per token, and final live submission still
# performs an unconditional REST read, so this only removes redundant research
# reads between rotations.
STREAM_REST_VERIFY_SECONDS = 900
D0_LIVE_REVALIDATION_SECONDS = 120
HISTORY_MAINTENANCE_INTERVAL_SECONDS = 3600


def _jittered_delay(base_seconds: float, *, spread: float = 0.15) -> float:
    """Avoid synchronized request bursts at fixed wall-clock boundaries."""
    base = max(0.0, float(base_seconds))
    if base <= 0:
        return 0.0
    delta = base * spread
    return max(0.0, base + random.uniform(-delta, delta))


@dataclass
class AutopilotPulseState:
    """Process-memory multi-cadence state for the single dashboard scheduler.

    Not persisted: restart rebuilds the queue from candidates/analyses.
    """

    next_capital_at: float = 0.0
    next_exit_at: float = 0.0
    next_discovery_at: float = 0.0
    next_weather_refresh_at: float = 0.0
    next_reprice_at: float = 0.0
    next_stream_sync_at: float = 0.0
    next_history_maintenance_at: float = 0.0
    capital_due_after_mutation: bool = False
    # Groups needing slow upstream refresh: (city, target_date) -> reason
    slow_refresh_reasons: dict[tuple[str, str], str] = field(default_factory=dict)
    # Last coherent forecast revision applied per group (city, target_date)
    group_forecast_revision: dict[tuple[str, str], str] = field(default_factory=dict)
    # Groups with a pending stream-driven reprice (at most one pending per group).
    pending_reprice_groups: dict[tuple[str, str], float] = field(default_factory=dict)
    # Quotes waiting for a successful capital path (or non-capital pulse) to apply.
    pending_stream_quotes: dict[str, Any] = field(default_factory=dict)
    rotation_cursor: int = 0
    live_rotation_cursor: int = 0
    last_path: str = "init"
    # Serialize: only one work class runs per pulse; background never overlaps.
    in_flight: bool = False
    # REST book reads skipped because a fresh stream quote covered the token.
    stream_rest_skips: int = 0
    stream_rest_reads: int = 0
    # Monotonic time of last REST verification per token (periodic + backfill).
    stream_rest_verified_at: dict[str, float] = field(default_factory=dict)
    stream_subscription_generation: int = -1
    reconciliation_failure_count: int = 0


_MATERIAL_UNVERIFIED_STATUSES = frozenset({"submitted_unverified", "reconcile_failed"})
# Persisted in autopilot_state.last_error (existing field; no schema change).
RECON_ALERT_PREFIX = "recon_alert|"


class SleepProtocol(Protocol):
    def __call__(self, seconds: float) -> None: ...


@dataclass(frozen=True)
class AutopilotBlockers:
    items: list[str]

    @property
    def blocked(self) -> bool:
        return bool(self.items)


@dataclass(frozen=True)
class AutopilotTickResult:
    status: str
    action: str
    market_id: str | None
    edge: Decimal | None
    reason: str
    blockers: list[str]
    discovered: int
    intent_id: int | None = None
    auto_exit_executed: int = 0
    auto_exit_attempted: int = 0
    auto_redeem_executed: int = 0
    auto_redeem_attempted: int = 0
    duration_ms: int = 0
    deferred_count: int = 0
    rotation_backlog: int = 0
    budget_deferred: int = 0
    failures: int = 0
    is_useful: bool = False


@dataclass(frozen=True)
class FirstRunCheck:
    name: str
    ok: bool
    status: str
    detail: str


@dataclass(frozen=True)
class AutopilotSnapshot:
    enabled: bool
    mode: str
    app_mode: str
    tick_seconds: int
    last_tick_at: str | None
    last_tick_status: str | None
    last_error: str | None
    tick_count: int
    process_started_at: str | None
    latest_useful_tick_at: str | None
    last_tick_duration_ms: int | None
    deferred_candidates_count: int | None
    blockers: list[str]
    llm_status: str
    first_run_checks: list[FirstRunCheck]
    decisions: list[dict[str, object]]
    auto_exit_enabled: bool = False
    auto_exit_armed: bool = False
    live_whitelist_open: bool = False
    reconciliation_status: str = "missing"
    reconciliation_fresh: bool = False
    reconciliation_detail: str | None = None


def select_fair_analysis_groups(
    candidate_groups: list[tuple[str, str]],
    rotation_slot: int,
    now: datetime | None = None,
    city_timezones: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    """
    Fairly order candidate groups by interleaving dates (e.g. D1, D0, D2)
    and rotating cities within each date deterministically.
    """
    from polymarket_weather_arb.domain.market_eligibility import try_local_weather_day

    by_days_diff: dict[Any, list[tuple[str, str]]] = {0: [], 1: [], 2: [], "unknown": []}
    current = now or datetime.now(timezone.utc)

    for group in candidate_groups:
        city = group[0]
        date_str = group[1]
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        local_day = try_local_weather_day(
            location_hint=city.replace("-", " "),
            timezone_name=(city_timezones or {}).get(city),
            now=current,
        )
        if local_day is None:
            by_days_diff["unknown"].append(group)
            continue

        days_diff = (target_date - local_day).days
        if 0 <= days_diff <= 2:
            by_days_diff[days_diff].append(group)

    rotated_by_diff = {}
    for diff, groups_for_diff in by_days_diff.items():
        sorted_groups = sorted(list(set(groups_for_diff)))
        if sorted_groups:
            shift = rotation_slot % len(sorted_groups)
            rotated_by_diff[diff] = sorted_groups[shift:] + sorted_groups[:shift]
        else:
            rotated_by_diff[diff] = []

    order = [1, 0, 2, "unknown"]
    lists = [rotated_by_diff[d] for d in order]

    result = []
    max_len = max((len(v) for v in lists), default=0)
    for i in range(max_len):
        for lst in lists:
            if i < len(lst):
                result.append(lst[i])

    return result


class AutopilotService:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        *,
        client: GammaPolymarketClient | None = None,
        llm_advisor: LlmAdvisorService | None = None,
        notifier: NotifyFn | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        # Composition roots (dashboard background) inject a shared client; only
        # close it when this service created the adapter.
        self._owns_client = client is None
        self.client = client or GammaPolymarketClient(settings)
        self.llm_advisor = llm_advisor or LlmAdvisorService(settings, repository)
        # Optional Telegram/Fanout notifier (one instance per dashboard process).
        self.notifier = notifier
        self.workflow = MarketWorkflowService(
            settings,
            repository,
            weather_provider_factory=_weather_provider_factory(settings),
            polymarket_client_factory=lambda _: self.client,
            llm_advisor=self.llm_advisor,
        )
        self.calibration_service = CalibrationService(repository)
        self._d0_live_revalidated_at: dict[tuple[str, str], float] = {}

    def close(self) -> None:
        """Close only a client this service created (not an injected shared one)."""
        if not self._owns_client:
            return
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def ensure_state(self, *, mode: str = "dry_run", tick_seconds: int = 300) -> None:
        self.repository.ensure_autopilot_state(
            mode=mode,
            app_mode=_app_mode_for_mode(mode),
            tick_seconds=tick_seconds,
        )

    def set_enabled(self, enabled: bool) -> None:
        self.repository.update_autopilot_state(enabled=enabled)

    def set_mode(self, mode: str) -> None:
        if mode not in {"dry_run", "live"}:
            raise ValueError(f"unsupported autopilot mode: {mode}")
        self.repository.update_autopilot_state(mode=mode, app_mode=_app_mode_for_mode(mode))

    def set_app_mode(self, app_mode: str) -> None:
        if app_mode not in APP_MODES:
            allowed = ", ".join(sorted(APP_MODES))
            raise ValueError(f"unsupported app mode: {app_mode}; allowed: {allowed}")
        self.repository.update_autopilot_state(
            enabled=False,
            mode=_execution_mode_for_app_mode(app_mode),
            app_mode=app_mode,
        )
        from polymarket_weather_arb.services.full_auto_service import (
            clear_legacy_global_live_overrides,
        )

        clear_legacy_global_live_overrides(self.repository)

    def snapshot(self) -> AutopilotSnapshot:
        state = self.repository.get_autopilot_state()
        mode = state["mode"] if state is not None else "dry_run"
        app_mode = _state_app_mode(state)
        reconciliation_status, reconciliation_fresh, reconciliation_detail = _reconciliation_health(
            self.repository.latest_reconciliation()
        )
        blockers = self.collect_blockers(live_mode=mode == "live", app_mode=app_mode)
        first_run_checks = self.first_run_checks(app_mode=app_mode)
        live_ids = live_market_ids_from_settings(self.settings)
        auto_exit_enabled = _auto_exit_enabled_for_app_mode(self.settings, app_mode)
        auto_exit_armed = auto_exit_enabled and app_mode in {"micro_live", "full_live"}
        live_whitelist_open = len(live_ids) == 0
        if state is None:
            return AutopilotSnapshot(
                enabled=False,
                mode="dry_run",
                app_mode=DEFAULT_APP_MODE,
                tick_seconds=300,
                last_tick_at=None,
                last_tick_status=None,
                last_error=None,
                tick_count=0,
                process_started_at=None,
                latest_useful_tick_at=None,
                last_tick_duration_ms=None,
                deferred_candidates_count=None,
                blockers=blockers.items,
                llm_status=llm_runtime_label(self.settings),
                first_run_checks=first_run_checks,
                decisions=self._decision_rows(),
                auto_exit_enabled=auto_exit_enabled,
                auto_exit_armed=False,
                live_whitelist_open=live_whitelist_open,
                reconciliation_status=reconciliation_status,
                reconciliation_fresh=reconciliation_fresh,
                reconciliation_detail=reconciliation_detail,
            )
        return AutopilotSnapshot(
            enabled=bool(state["enabled"]),
            mode=state["mode"],
            app_mode=app_mode,
            tick_seconds=int(state["tick_seconds"]),
            last_tick_at=state["last_tick_at"],
            last_tick_status=state["last_tick_status"],
            last_error=state["last_error"],
            tick_count=int(state["tick_count"]),
            process_started_at=dict(state).get("process_started_at"),
            latest_useful_tick_at=dict(state).get("latest_useful_tick_at"),
            last_tick_duration_ms=int(dict(state)["last_tick_duration_ms"])
            if dict(state).get("last_tick_duration_ms") is not None
            else None,
            deferred_candidates_count=int(dict(state)["deferred_candidates_count"])
            if dict(state).get("deferred_candidates_count") is not None
            else None,
            blockers=blockers.items,
            llm_status=llm_runtime_label(self.settings),
            first_run_checks=first_run_checks,
            decisions=self._decision_rows(),
            auto_exit_enabled=auto_exit_enabled,
            auto_exit_armed=auto_exit_armed and bool(state["enabled"]),
            live_whitelist_open=live_whitelist_open,
            reconciliation_status=reconciliation_status,
            reconciliation_fresh=reconciliation_fresh,
            reconciliation_detail=reconciliation_detail,
        )

    def first_run_checks(self, *, app_mode: str | None = None) -> list[FirstRunCheck]:
        reconciliation_status, reconciliation_fresh, reconciliation_detail = _reconciliation_health(
            self.repository.latest_reconciliation()
        )
        live_ready = True
        live_detail = "live credentials are configured"
        try:
            self.settings.ensure_live_trading_ready()
        except ValueError as exc:
            live_ready = False
            live_detail = str(exc)
        compliance_ok = (
            not self.settings.trading_disabled
        ) and self.settings.compliance_check_enabled
        compliance_status = "configured" if compliance_ok else "blocked"
        compliance_detail = (
            f"compliance check enabled; allowed countries={self.settings.compliance_allowed_countries}"
            if compliance_ok
            else "TRADING_DISABLED=true blocks live compliance checks"
            if self.settings.trading_disabled
            else "COMPLIANCE_CHECK_ENABLED=false"
        )
        breaker_ok = live_execution_blocked(self.repository) is None
        full_live_ready = _full_live_readiness_ok(
            trading_disabled=self.settings.trading_disabled,
            live_ready=live_ready,
            compliance_ok=compliance_ok,
            reconciliation_fresh=reconciliation_fresh,
            breaker_ok=breaker_ok,
        )
        return [
            FirstRunCheck(
                "database",
                True,
                "ok",
                f"database path: {self.settings.database_path}",
            ),
            FirstRunCheck(
                "weather",
                bool(self.settings.weather_provider),
                "configured" if self.settings.weather_provider else "missing",
                f"weather provider: {self.settings.weather_provider or 'not configured'}",
            ),
            FirstRunCheck(
                "polymarket_reads",
                bool(
                    self.settings.polymarket_gamma_api_base
                    and self.settings.polymarket_clob_api_base
                ),
                "configured",
                "Polymarket Gamma and CLOB read endpoints are configured",
            ),
            FirstRunCheck(
                "compliance",
                compliance_ok,
                compliance_status,
                compliance_detail,
            ),
            FirstRunCheck(
                "reconciliation",
                reconciliation_fresh,
                reconciliation_status,
                reconciliation_detail,
            ),
            FirstRunCheck(
                "trading_disabled",
                not self.settings.trading_disabled,
                "off" if not self.settings.trading_disabled else "on",
                "TRADING_DISABLED=false allows live modes"
                if not self.settings.trading_disabled
                else "TRADING_DISABLED=true keeps live trading locked",
            ),
            FirstRunCheck(
                "live_credentials",
                live_ready,
                "configured" if live_ready else "missing",
                live_detail,
            ),
            FirstRunCheck(
                "auto_redeem",
                self.settings.builder_credentials_ready(),
                (
                    "builder_ready"
                    if self.settings.builder_credentials_ready()
                    else "wallet_check_required"
                ),
                (
                    "Builder credential triple is configured for gasless wallet redemption"
                    if self.settings.builder_credentials_ready()
                    else (
                        "Builder credential triple is not configured; Deposit Wallet "
                        "redemption will fail closed (an EOA wallet may use direct broadcast)"
                    )
                ),
            ),
            FirstRunCheck(
                "full_live",
                full_live_ready,
                "ready" if full_live_ready else "not_ready",
                (
                    "full live readiness: credentials, compliance, fresh reconciliation, "
                    "open breaker, automatic exits"
                    if full_live_ready
                    else (
                        "full live needs TRADING_DISABLED=false, live credentials, "
                        "compliance, fresh reconciliation, and an open breaker"
                    )
                ),
            ),
            FirstRunCheck(
                "resolution_circuit_breaker",
                breaker_ok,
                "ok" if breaker_ok else "tripped",
                "resolution circuit breaker is not tripped"
                if breaker_ok
                else "resolution circuit breaker is tripped",
            ),
        ]

    def collect_blockers(
        self,
        *,
        live_mode: bool,
        app_mode: str | None = None,
        require_fresh_reconciliation: bool = True,
    ) -> AutopilotBlockers:
        items: list[str] = []
        if live_mode or app_mode in {"micro_live", "full_live"}:
            blocker = live_execution_blocked(self.repository)
            if blocker:
                items.append(blocker)

        if (
            live_mode or app_mode in {"micro_live", "full_live"}
        ) and self.settings.trading_disabled:
            items.append("TRADING_DISABLED=true")
        if live_mode:
            try:
                self.settings.ensure_live_trading_ready()
            except ValueError as exc:
                items.append(str(exc))
            decision = ComplianceService(self.settings).check_live_allowed()
            if not decision.ok:
                items.append(decision.reason)
            if require_fresh_reconciliation:
                latest_reconciliation = self.repository.latest_reconciliation()
                reconciliation_status, reconciliation_fresh, reconciliation_detail = (
                    _reconciliation_health(latest_reconciliation)
                )
                if not reconciliation_fresh:
                    if reconciliation_status == "missing":
                        items.append("no successful reconciliation")
                    elif reconciliation_status == "stale":
                        items.append("reconciliation is stale")
                    else:
                        items.append(
                            f"capital path blocked: reconciliation {reconciliation_status}; "
                            f"{reconciliation_detail}"
                        )
        return AutopilotBlockers(items=items)

    def tick(self) -> AutopilotTickResult:
        """Run one full Autopilot cycle (CLI ``--once`` and compatibility tests).

        Background dashboard scheduling uses :meth:`pulse` for multi-cadence work.
        """
        try:
            return self._tick_body()
        finally:
            self._flush_notifier()

    def pulse(
        self,
        pulse_state: AutopilotPulseState,
        *,
        monotonic: MonotonicFn = time.monotonic,
        stream_bridge: Any | None = None,
    ) -> AutopilotTickResult:
        """One serial multi-cadence wake: at most one work class, no overlapping mutation.

        Priority: capital maintenance → slow input refresh → cached group reprice →
        health pulse. Does not create a second scheduler or trading owner.

        Optional ``stream_bridge`` supplies exchange Market/User Channel hints only;
        all SQL and strategy work remains in this serial pulse.
        """
        if pulse_state.in_flight:
            result = AutopilotTickResult(
                status="skipped",
                action="skip",
                market_id=None,
                edge=None,
                reason="pulse already in flight; refusing overlap",
                blockers=["pulse_in_flight"],
                discovered=0,
                is_useful=False,
            )
            self._record_tick(result, discovered=0, increment_tick_count=False)
            return result
        pulse_state.in_flight = True
        try:
            try:
                return self._pulse_body(
                    pulse_state, monotonic=monotonic, stream_bridge=stream_bridge
                )
            finally:
                self._flush_notifier()
        finally:
            pulse_state.in_flight = False

    def _pulse_body(
        self,
        pulse_state: AutopilotPulseState,
        *,
        monotonic: MonotonicFn,
        stream_bridge: Any | None = None,
    ) -> AutopilotTickResult:
        started = monotonic()
        tick_start_ms = started * 1000
        state = self.repository.get_autopilot_state()
        mode = state["mode"] if state is not None else "dry_run"
        app_mode = _state_app_mode(state)
        live_mode = mode == "live"
        now_m = started

        # 0) Drain exchange stream hints (non-blocking; no strategy/SQL in bridge).
        self._ingest_stream_signals(pulse_state, stream_bridge)
        self._persist_stream_health(stream_bridge, pulse_state=pulse_state)

        # 1) Capital maintenance always wins (incl. User Channel reconcile hints).
        capital_due = live_mode and (
            pulse_state.capital_due_after_mutation
            or now_m >= pulse_state.next_capital_at
            or now_m >= pulse_state.next_exit_at
        )
        if capital_due:
            result = self._pulse_capital_maintenance(
                pulse_state,
                app_mode=app_mode,
                live_mode=live_mode,
                tick_start_ms=tick_start_ms,
                monotonic=monotonic,
            )
            # Quotes that depend on capital truth apply only after recon success.
            if result.status not in {"failed", "blocked"}:
                self._apply_pending_stream_quotes(
                    pulse_state, stream_bridge=stream_bridge, monotonic=monotonic
                )
            self._maybe_sync_stream_subscriptions(
                pulse_state, stream_bridge=stream_bridge, monotonic=monotonic, force=True
            )
            pulse_state.last_path = "capital"
            return result

        # No capital work this pulse: persist coalesced quotes then continue.
        self._apply_pending_stream_quotes(
            pulse_state, stream_bridge=stream_bridge, monotonic=monotonic
        )

        # 2) Slow input refresh (discovery / weather / LLM via existing batch path).
        slow_due = (
            now_m >= pulse_state.next_discovery_at or now_m >= pulse_state.next_weather_refresh_at
        )
        if slow_due:
            result = self._pulse_slow_refresh(
                pulse_state,
                app_mode=app_mode,
                live_mode=live_mode,
                tick_start_ms=tick_start_ms,
                monotonic=monotonic,
            )
            self._maybe_sync_stream_subscriptions(
                pulse_state, stream_bridge=stream_bridge, monotonic=monotonic, force=True
            )
            pulse_state.last_path = "slow_refresh"
            return result

        # 3) Cached event-group reprice (no upstream weather/Gamma/LLM).
        # Stream BBOs enqueue groups but never bypass the global reprice budget.
        # This keeps one noisy event from monopolizing the serial scheduler.
        reprice_due = now_m >= pulse_state.next_reprice_at
        if reprice_due:
            result = self._pulse_cached_reprice(
                pulse_state,
                app_mode=app_mode,
                live_mode=live_mode,
                tick_start_ms=tick_start_ms,
                monotonic=monotonic,
                stream_bridge=stream_bridge,
            )
            if result is not None:
                pulse_state.last_path = "cached_reprice"
                return result

        # 4) Honest health pulse for local stream UI.
        result = AutopilotTickResult(
            status="idle",
            action="skip",
            market_id=None,
            edge=None,
            reason=(
                f"health pulse; last_path={pulse_state.last_path}; "
                f"slow_backlog={len(pulse_state.slow_refresh_reasons)}; "
                f"stream_pending_groups={len(pulse_state.pending_reprice_groups)}"
            ),
            blockers=[],
            discovered=0,
            duration_ms=int((monotonic() - started) * 1000),
            deferred_count=len(pulse_state.slow_refresh_reasons),
            is_useful=False,
        )
        self._record_health_pulse(result)
        pulse_state.last_path = "health"
        return result

    def _pulse_capital_maintenance(
        self,
        pulse_state: AutopilotPulseState,
        *,
        app_mode: str,
        live_mode: bool,
        tick_start_ms: float,
        monotonic: MonotonicFn,
    ) -> AutopilotTickResult:
        discovered = 0
        if live_mode:
            recon = ReconciliationService(self.client, self.repository).reconcile()
            self._commit_reconciliation_then_notify_fills(recon)
            recon_status = str(recon.get("status") or "")
            failed_stage = recon.get("failed_stage")
            if recon_status != "ok":
                signature = build_recon_alert_signature(recon)
                reason = (
                    f"reconciliation status={recon_status} fail-stop; "
                    f"stage={failed_stage or 'unknown'}; "
                    "cancel/exit/entry blocked this capital pulse"
                )
                result = AutopilotTickResult(
                    status="failed",
                    action="skip",
                    market_id=None,
                    edge=None,
                    reason=reason,
                    blockers=[f"reconciliation status={recon_status}"],
                    discovered=0,
                    duration_ms=int(monotonic() * 1000 - tick_start_ms),
                    is_useful=False,
                )
                prior_signature = self._load_recon_alert_signature()
                self._record_tick(result, discovered=0, error=signature)
                self._commit_recon_alert_state("failure")
                self._notify_reconciliation_failure(
                    signature=signature,
                    prior_signature=prior_signature,
                    recon_status=recon_status,
                    failed_stage=failed_stage,
                    reason=reason,
                )
                # Back off both capital and exit clocks. Leaving next_exit_at due
                # caused the 2-second pulse loop to hammer reconciliation during
                # an exchange/network outage.
                pulse_state.reconciliation_failure_count += 1
                retry_delay = min(
                    300.0,
                    15.0 * (2 ** min(pulse_state.reconciliation_failure_count - 1, 5)),
                )
                retry_at = monotonic() + _jittered_delay(retry_delay)
                pulse_state.next_capital_at = retry_at
                pulse_state.next_exit_at = retry_at
                pulse_state.capital_due_after_mutation = False
                return result
            pulse_state.reconciliation_failure_count = 0
            self._notify_reconciliation_recovery_if_needed()

            execution_blockers = self.collect_blockers(
                live_mode=True,
                app_mode=app_mode,
                require_fresh_reconciliation=True,
            )
            if execution_blockers.blocked:
                reason = "; ".join(execution_blockers.items)
                result = AutopilotTickResult(
                    status="blocked",
                    action="skip",
                    market_id=None,
                    edge=None,
                    reason=reason,
                    blockers=execution_blockers.items,
                    discovered=0,
                    duration_ms=int(monotonic() * 1000 - tick_start_ms),
                    is_useful=False,
                )
                self._record_tick(
                    result,
                    discovered=0,
                    error=f"pulse_blocker|{reason}",
                    increment_tick_count=False,
                )
                retry_at = monotonic() + _jittered_delay(CAPITAL_INTERVAL_SECONDS)
                pulse_state.next_capital_at = retry_at
                pulse_state.next_exit_at = retry_at
                return result

        lifecycle_notes = self._maybe_manage_stale_orders(app_mode=app_mode)
        lifecycle_failed = any(
            "cancel_failed" in note or "lifecycle_failed" in note or "cancel_commit_failed" in note
            for note in lifecycle_notes
        )
        exit_executed, exit_attempted = 0, 0
        redeem_executed, redeem_attempted = 0, 0
        if app_mode in {"micro_live", "full_live"}:
            self._refresh_position_analyses()
            redeem_executed, redeem_attempted = self._maybe_auto_redeem(
                app_mode=app_mode
            )
            # Keep a single high-impact position mutation per capital pulse.
            # An attempted redemption may already be on-chain even when local
            # confirmation is ambiguous, so do not follow it with a SELL.
            if not redeem_attempted:
                exit_executed, exit_attempted = self._maybe_auto_exit(app_mode=app_mode)
        now_m = monotonic()
        cancelled_orders = any(
            note.startswith("cancelled_stale_orders=") for note in lifecycle_notes
        )
        mutation_executed = bool(
            redeem_attempted or exit_executed or cancelled_orders
        )
        pulse_state.capital_due_after_mutation = mutation_executed
        pulse_state.next_capital_at = (
            now_m if mutation_executed else now_m + _jittered_delay(CAPITAL_INTERVAL_SECONDS)
        )
        pulse_state.next_exit_at = now_m + _jittered_delay(EXIT_INTERVAL_SECONDS)

        reason = "capital maintenance ok"
        if lifecycle_notes:
            reason = f"{reason}; {'; '.join(lifecycle_notes)}"
        if exit_attempted:
            reason = f"{reason}; exits attempted={exit_attempted} executed={exit_executed}"
        if redeem_attempted:
            reason = (
                f"{reason}; redeems attempted={redeem_attempted} "
                f"confirmed={redeem_executed}"
            )
        result = AutopilotTickResult(
            status="failed" if lifecycle_failed else "ok",
            action="skip",
            market_id=None,
            edge=None,
            reason=reason,
            blockers=[],
            discovered=discovered,
            auto_exit_executed=exit_executed,
            auto_exit_attempted=exit_attempted,
            auto_redeem_executed=redeem_executed,
            auto_redeem_attempted=redeem_attempted,
            duration_ms=int(now_m * 1000 - tick_start_ms),
            is_useful=True,
        )
        self._record_tick(
            result,
            discovered=discovered,
            error=reason if lifecycle_failed else None,
        )
        return result

    def _pulse_slow_refresh(
        self,
        pulse_state: AutopilotPulseState,
        *,
        app_mode: str,
        live_mode: bool,
        tick_start_ms: float,
        monotonic: MonotonicFn,
    ) -> AutopilotTickResult:
        discovered = 0
        failures = 0
        deferred = len(pulse_state.slow_refresh_reasons)
        now_m = monotonic()
        discovery_due = now_m >= pulse_state.next_discovery_at
        weather_due = now_m >= pulse_state.next_weather_refresh_at
        state = self.repository.get_autopilot_state()
        analyzed = 0
        rb = 0
        bd = 0

        if discovery_due:
            discovery = DiscoveryService(self.client, self.repository)
            discovery.reset_fallback_budget()
            try:
                discovered += discovery.discover_weather_events(
                    limit=30,
                    time_budget=45.0,
                    rotation_slot=state["tick_count"] if state else 0,
                    reset_fallback_budget=False,
                )
                discovered += discovery.discover(limit=50, pages=1, reset_fallback_budget=False)
                self.repository.connection.commit()
                pulse_state.next_discovery_at = now_m + _jittered_delay(DISCOVERY_INTERVAL_SECONDS)
            except Exception as exc:
                failures += 1
                pulse_state.next_discovery_at = now_m + _jittered_delay(60)
                logger.warning("pulse slow discovery failed: %s", exc)

        if now_m >= pulse_state.next_history_maintenance_at:
            try:
                pruned_snapshots = self.repository.prune_old_market_snapshots(
                    keep_days=7,
                    batch_size=20000,
                )
                pruned_signals = self.repository.prune_superseded_pending_weather_source_signals(
                    batch_size=10000
                )
                pruned_forecasts = self.repository.prune_old_weather_forecasts(
                    keep_days=14, batch_size=5000
                )
                pruned_observations = self.repository.prune_old_weather_observations(
                    keep_days=14, batch_size=5000
                )
                pruned_analyses = self.repository.prune_unreferenced_analyses(
                    keep_days=14, batch_size=5000
                )
                pruned_audits = self.repository.prune_superseded_unavailable_resolution_audits(
                    batch_size=5000
                )
                compacted_audits = self.repository.compact_resolution_audit_payloads(
                    batch_size=1000
                )
                self.repository.connection.commit()
                if any(
                    (
                        pruned_snapshots,
                        pruned_signals,
                        pruned_forecasts,
                        pruned_observations,
                        pruned_analyses,
                        pruned_audits,
                        compacted_audits,
                    )
                ):
                    logger.info(
                        "runtime history maintenance pruned snapshots=%s "
                        "source_signals=%s forecasts=%s observations=%s analyses=%s "
                        "audits=%s compacted_audits=%s",
                        pruned_snapshots,
                        pruned_signals,
                        pruned_forecasts,
                        pruned_observations,
                        pruned_analyses,
                        pruned_audits,
                        compacted_audits,
                    )
                pulse_state.next_history_maintenance_at = (
                    now_m + HISTORY_MAINTENANCE_INTERVAL_SECONDS
                )
            except Exception as exc:
                failures += 1
                pulse_state.next_history_maintenance_at = now_m + 300
                logger.warning("runtime history maintenance failed: %s", exc)

        if weather_due:
            try:
                self._backfill_resolved_model_signals()
            except Exception as exc:
                failures += 1
                logger.warning("pulse calibration backfill failed: %s", exc)
            analyzed, rb, bd, fail = self._prepare_global_bucket_candidates(
                max_groups=4, time_budget=90.0
            )
            failures += fail
            deferred += rb + bd
            refresh_delay = 60 if fail else WEATHER_REFRESH_INTERVAL_SECONDS
            provider_cooldown = open_meteo_cooldown_remaining()
            pulse_state.next_weather_refresh_at = now_m + (
                max(refresh_delay, provider_cooldown)
                if provider_cooldown
                else _jittered_delay(refresh_delay)
            )
            if analyzed:
                # New forecast/observation evidence makes a near-term quote pass useful.
                pulse_state.next_reprice_at = now_m

        # Optional entry after slow research (same selection rules as full tick).
        entry_result = None
        if weather_due and analyzed:
            entry_result = self._pulse_maybe_enter(
                app_mode=app_mode,
                live_mode=live_mode,
                discovered=discovered,
                tick_start_ms=tick_start_ms,
                monotonic=monotonic,
                deferred_count=deferred,
                failures=failures,
                default_reason=(
                    f"slow refresh: discovered={discovered} analyzed={analyzed} "
                    f"rotation_backlog={rb} failures={failures}"
                ),
            )
        if entry_result is not None:
            if live_mode and entry_result.status == "executed":
                pulse_state.capital_due_after_mutation = True
            return entry_result

        finished_m = monotonic()
        result = AutopilotTickResult(
            status="ok" if failures == 0 else "failed",
            action="skip",
            market_id=None,
            edge=None,
            reason=(
                f"slow refresh: discovered={discovered} analyzed={analyzed} "
                f"rotation_backlog={rb} failures={failures}"
            ),
            blockers=[],
            discovered=discovered,
            duration_ms=int(finished_m * 1000 - tick_start_ms),
            deferred_count=deferred,
            rotation_backlog=rb,
            budget_deferred=bd,
            failures=failures,
            is_useful=True,
        )
        self._record_tick(result, discovered=discovered)
        return result

    def _pulse_cached_reprice(
        self,
        pulse_state: AutopilotPulseState,
        *,
        app_mode: str,
        live_mode: bool,
        tick_start_ms: float,
        monotonic: MonotonicFn,
        stream_bridge: Any | None = None,
    ) -> AutopilotTickResult | None:
        group, market_ids = self._select_reprice_group(pulse_state)
        pulse_state.next_reprice_at = monotonic() + _jittered_delay(REPRICE_INTERVAL_SECONDS)
        if group is None or not market_ids:
            return None

        city, target_date = group
        pulse_state.pending_reprice_groups.pop(group, None)
        quote_failures = self._refresh_group_order_books(
            market_ids, pulse_state=pulse_state, stream_bridge=stream_bridge
        )
        if quote_failures:
            pulse_state.next_reprice_at = monotonic() + _jittered_delay(REPRICE_INTERVAL_SECONDS)
            pulse_state.rotation_cursor += 1
            reason = (
                f"quote refresh failed for {city}/{target_date}; cached reprice blocked: "
                + "; ".join(quote_failures)
            )
            result = AutopilotTickResult(
                status="failed",
                action="skip",
                market_id=market_ids[0],
                edge=None,
                reason=reason,
                blockers=["fresh group quotes unavailable"],
                discovered=0,
                duration_ms=int(monotonic() * 1000 - tick_start_ms),
                deferred_count=len(pulse_state.slow_refresh_reasons),
                failures=len(quote_failures),
                is_useful=True,
            )
            self._record_tick(result, discovered=0, error=reason)
            return result

        analyzed, failures, slow_reason, revision = (
            self.workflow.reprice_global_bucket_group_cached(market_ids)
        )
        pulse_state.next_reprice_at = monotonic() + _jittered_delay(REPRICE_INTERVAL_SECONDS)
        if slow_reason:
            pulse_state.slow_refresh_reasons[group] = slow_reason
            pulse_state.rotation_cursor += 1
            # Pull slow refresh forward without waiting full discovery cadence.
            provider_cooldown = open_meteo_cooldown_remaining()
            refresh_at = monotonic() + (
                provider_cooldown if provider_cooldown else _jittered_delay(60)
            )
            pulse_state.next_weather_refresh_at = (
                max(pulse_state.next_weather_refresh_at, refresh_at)
                if provider_cooldown
                else min(pulse_state.next_weather_refresh_at, refresh_at)
            )
            result = AutopilotTickResult(
                status="skipped",
                action="skip",
                market_id=market_ids[0],
                edge=None,
                reason=(
                    f"cached reprice deferred for {city}/{target_date}: {slow_reason}; "
                    "enqueued slow refresh"
                ),
                blockers=[],
                discovered=0,
                duration_ms=int(monotonic() * 1000 - tick_start_ms),
                deferred_count=len(pulse_state.slow_refresh_reasons),
                failures=len(failures),
                is_useful=True,
            )
            self._record_tick(result, discovered=0)
            return result

        if revision:
            # Newer revision supersedes older in-memory tag for this group.
            pulse_state.group_forecast_revision[group] = revision
        pulse_state.slow_refresh_reasons.pop(group, None)
        pulse_state.rotation_cursor += 1

        entry_result = self._pulse_maybe_enter(
            app_mode=app_mode,
            live_mode=live_mode,
            discovered=0,
            tick_start_ms=tick_start_ms,
            monotonic=monotonic,
            deferred_count=len(pulse_state.slow_refresh_reasons),
            failures=len(failures),
            default_reason=(
                f"cached reprice {city}/{target_date}: analyzed={analyzed} "
                f"revision={revision or 'none'} failures={len(failures)}"
            ),
            preferred_market_ids=set(market_ids),
        )
        if entry_result is not None:
            if live_mode and entry_result.status == "executed":
                pulse_state.capital_due_after_mutation = True
            return entry_result

        result = AutopilotTickResult(
            status="ok" if not failures else "failed",
            action="skip",
            market_id=market_ids[0],
            edge=None,
            reason=(
                f"cached reprice {city}/{target_date}: analyzed={analyzed} "
                f"revision={revision or 'none'} failures={len(failures)}"
            ),
            blockers=[],
            discovered=0,
            duration_ms=int(monotonic() * 1000 - tick_start_ms),
            deferred_count=len(pulse_state.slow_refresh_reasons),
            failures=len(failures),
            is_useful=True,
        )
        self._record_tick(result, discovered=0)
        return result

    def _select_reprice_group(
        self, pulse_state: AutopilotPulseState
    ) -> tuple[tuple[str, str] | None, list[str]]:
        """Pick one (city, date) group: stream-pending, live inventory, then fair rotation."""
        candidates = self.repository.list_candidates(
            limit=GLOBAL_BUCKET_CANDIDATE_SCAN_LIMIT,
            status="dry_run_ready",
            module_id="global_temp_bucket",
        )
        groups: dict[tuple[str, str], list[str]] = {}
        live_groups: set[tuple[str, str]] = set()
        city_timezones: dict[str, str] = {}
        for candidate in candidates:
            market_id = str(candidate["market_id"])
            market = self.repository.get_market(market_id)
            if market is None or self._retire_if_unorderable(
                market, location_hint=str(candidate["city"] or "")
            ):
                continue
            city = str(candidate["city"] or market_id)
            target_date = str(candidate["target_date"] or "")
            if not target_date:
                continue
            group = (city, target_date)
            timezone_name = str(candidate["settlement_timezone"] or "")
            if timezone_name:
                city_timezones[city] = timezone_name
            if candidate["best_bid"] is None and candidate["best_ask"] is None:
                # Need a persisted book for cached reprice; mark for slow path.
                pulse_state.slow_refresh_reasons.setdefault(group, "missing_order_book")
                continue
            groups.setdefault(group, []).append(market_id)
            if self.repository.market_has_live_activity(market_id):
                live_groups.add(group)

        if not groups:
            return None, []

        # Stream-driven pending groups win so BBO changes reprice promptly.
        pending = [
            group
            for group, _ts in sorted(
                pulse_state.pending_reprice_groups.items(), key=lambda item: item[1]
            )
            if group in groups
        ]
        if pending:
            group = pending[0]
            return group, groups[group]

        ordered = select_fair_analysis_groups(
            list(groups.keys()),
            pulse_state.rotation_cursor,
            city_timezones=city_timezones,
        )
        if not ordered:
            # Fall back to any known group when fair rotation filters far horizons.
            ordered = sorted(groups.keys())
        if not ordered:
            return None, []
        # Capital maintenance and auto-exit already refresh held positions every
        # three minutes. Reserve one of every three cached-reprice slots for live
        # inventory and use the other two for D0/D1/D2 opportunity rotation.
        if live_groups and pulse_state.rotation_cursor % 3 == 0:
            live_ordered = sorted(group for group in live_groups if group in groups)
            if live_ordered:
                group = live_ordered[pulse_state.live_rotation_cursor % len(live_ordered)]
                pulse_state.live_rotation_cursor += 1
                return group, groups[group]
        opportunity_ordered = [group for group in ordered if group not in live_groups]
        if opportunity_ordered:
            group = opportunity_ordered[0]
            return group, groups[group]
        group = ordered[0]
        return group, groups.get(group, [])

    def _refresh_group_order_books(
        self,
        market_ids: list[str],
        *,
        pulse_state: AutopilotPulseState | None = None,
        stream_bridge: Any | None = None,
    ) -> list[str]:
        """Refresh one complete sibling group's YES books before cached pricing.

        Prefer fresh Market Channel quotes when the bridge reports a trustworthy
        BBO for the YES token; otherwise use the existing bounded REST path.
        A partial refresh blocks the whole group so sibling prices never mix
        fresh and stale revisions. Final pre-submit REST verification is unchanged.
        """
        failures: list[str] = []
        for market_id in market_ids:
            market = self.repository.get_market(market_id)
            if market is None:
                failures.append(f"{market_id}: unknown market")
                continue
            if self._retire_if_unorderable(market):
                failures.append(f"{market_id}: market is no longer orderable")
                continue
            yes_token = market["yes_token_id"]
            token_key = str(yes_token) if yes_token else ""
            now_m = time.monotonic()
            needs_backfill = bool(
                stream_bridge is not None
                and token_key
                and getattr(stream_bridge, "needs_rest_backfill", None) is not None
                and stream_bridge.needs_rest_backfill(token_key)
            )
            last_verify = (
                pulse_state.stream_rest_verified_at.get(token_key, 0.0)
                if pulse_state is not None and token_key
                else 0.0
            )
            stored = self.repository.latest_pricing_snapshot(market_id)
            persisted_stream_fresh = bool(
                stored is not None and not needs_backfill and _is_recent_stream_snapshot(stored)
            )
            stream_fresh = bool(
                persisted_stream_fresh
                or (
                    stream_bridge is not None
                    and yes_token
                    and getattr(stream_bridge, "is_token_fresh", None) is not None
                    and stream_bridge.is_token_fresh(str(yes_token))
                    and not needs_backfill
                )
            )
            # Skip REST only when stream BBO is fresh AND a recent REST verify
            # already confirmed the book (periodic re-check still runs).
            recently_verified = bool(
                last_verify and (now_m - last_verify) < STREAM_REST_VERIFY_SECONDS
            )
            if stream_fresh and recently_verified:
                if stored is not None:
                    if pulse_state is not None:
                        pulse_state.stream_rest_skips += 1
                    continue
            if pulse_state is not None:
                pulse_state.stream_rest_reads += 1
            _snapshot, error = self._refresh_token_order_book(
                market_id=market_id,
                market_row=market,
                side="buy_yes",
            )
            if error:
                failures.append(f"{market_id}: {error}")
            elif token_key:
                if pulse_state is not None:
                    pulse_state.stream_rest_verified_at[token_key] = now_m
                if stream_bridge is not None and hasattr(stream_bridge, "mark_rest_verified"):
                    stream_bridge.mark_rest_verified(token_key)
        return failures

    def _ingest_stream_signals(
        self, pulse_state: AutopilotPulseState, stream_bridge: Any | None
    ) -> None:
        """Drain the bridge queue into pulse state without touching strategy owners."""
        if stream_bridge is None:
            return
        try:
            batch = stream_bridge.drain()
        except Exception as exc:
            logger.warning("stream drain failed; retaining REST fallback: %s", exc)
            return
        if batch.reconcile_due or batch.resolved:
            # User Channel and market_resolved are low-latency recon hints only.
            pulse_state.capital_due_after_mutation = True
        for token_id, quote in batch.quotes.items():
            pulse_state.pending_stream_quotes[token_id] = quote
        # Tick-size changes invalidate trust for that token → force REST next reprice.
        for hint in batch.tick_size:
            token_id = str(getattr(hint, "token_id", "") or "")
            if token_id:
                # Drop freshness by removing any pending quote reliance on old tick.
                pulse_state.pending_stream_quotes.pop(token_id, None)

    def _persist_stream_health(
        self,
        stream_bridge: Any | None,
        *,
        pulse_state: AutopilotPulseState | None = None,
    ) -> None:
        """Overwrite singleton exchange-stream health fields (no history table)."""
        if stream_bridge is None:
            self.repository.update_autopilot_state(
                exchange_stream_status="disabled",
                exchange_stream_updated_at=_now_iso(),
                exchange_stream_detail=json.dumps(
                    {
                        "subscribed_token_count": 0,
                        "rest_fallback_active": True,
                        "local_transport": "sqlite",
                        "rest_skips": 0,
                        "rest_reads": 0,
                    },
                    sort_keys=True,
                ),
            )
            return
        try:
            health = stream_bridge.health().public_dict()
        except Exception as exc:
            self.repository.update_autopilot_state(
                exchange_stream_status="degraded",
                exchange_stream_updated_at=_now_iso(),
                exchange_stream_detail=json.dumps(
                    {"error": "health_read_failed", "rest_fallback_active": True},
                    sort_keys=True,
                ),
            )
            logger.warning("stream health read failed: %s", exc)
            return
        detail = health.get("detail") if isinstance(health.get("detail"), dict) else {}
        detail = {
            k: v
            for k, v in detail.items()
            if not any(
                secret in str(k).lower()
                for secret in ("key", "secret", "credential", "private", "auth", "password")
            )
        }
        detail["local_transport"] = "sqlite"
        if pulse_state is not None:
            detail["rest_skips"] = int(pulse_state.stream_rest_skips)
            detail["rest_reads"] = int(pulse_state.stream_rest_reads)
        # Bound nested structure.
        bounded = json.dumps(detail, default=str, sort_keys=True)[:3500]
        self.repository.update_autopilot_state(
            exchange_stream_status=str(health.get("status") or "degraded"),
            exchange_stream_updated_at=_now_iso(),
            exchange_stream_detail=bounded,
        )

    def _apply_pending_stream_quotes(
        self,
        pulse_state: AutopilotPulseState,
        *,
        stream_bridge: Any | None,
        monotonic: MonotonicFn,
    ) -> int:
        """Persist meaningful token-aware BBOs and mark sibling groups for reprice."""
        if not pulse_state.pending_stream_quotes:
            return 0
        from polymarket_weather_arb.domain.markets import MarketSnapshot

        applied = 0
        token_map = {}
        if stream_bridge is not None and hasattr(stream_bridge, "token_to_market"):
            try:
                token_map = dict(stream_bridge.token_to_market())
            except Exception:
                token_map = {}
        pending = dict(pulse_state.pending_stream_quotes)
        pulse_state.pending_stream_quotes.clear()
        now = datetime.now(timezone.utc)
        for token_id, quote in pending.items():
            market_id = token_map.get(token_id)
            if not market_id:
                market_id = self.repository.resolve_local_market_id(None, token_id)
            if not market_id:
                continue
            best_bid = getattr(quote, "best_bid", None)
            best_ask = getattr(quote, "best_ask", None)
            prev = self.repository.latest_market_snapshot(market_id, token_id=str(token_id))
            if prev is not None:
                prev_bid = prev["best_bid"]
                prev_ask = prev["best_ask"]
                same_bid = (prev_bid is None and best_bid is None) or (
                    prev_bid is not None
                    and best_bid is not None
                    and Decimal(str(prev_bid)) == Decimal(str(best_bid))
                )
                same_ask = (prev_ask is None and best_ask is None) or (
                    prev_ask is not None
                    and best_ask is not None
                    and Decimal(str(prev_ask)) == Decimal(str(best_ask))
                )
                if same_bid and same_ask:
                    # Depth-only churn with unchanged BBO must not reprice.
                    continue
            midpoint = getattr(quote, "midpoint", None)
            spread = getattr(quote, "spread", None)
            liquidity = getattr(quote, "liquidity", None)
            snapshot = MarketSnapshot(
                market_id=str(market_id),
                best_bid=best_bid,
                best_ask=best_ask,
                midpoint=midpoint,
                spread=spread,
                liquidity=liquidity,
                fetched_at=now,
                token_id=str(token_id),
            )
            self.repository.save_market_snapshot(
                snapshot,
                {
                    "source": "polymarket_stream",
                    "source_type": getattr(quote, "source_type", None),
                    "token_id": str(token_id),
                },
                token_id=str(token_id),
            )
            applied += 1
            group = self._group_for_market(str(market_id))
            if group is not None:
                # Coalesce: at most one pending reprice entry per group.
                pulse_state.pending_reprice_groups[group] = monotonic()
        return applied

    def _group_for_market(self, market_id: str) -> tuple[str, str] | None:
        rule = self.repository.get_temperature_bucket_rule(market_id)
        if rule is None:
            return None
        city = str(rule["city"] or "").strip()
        target_date = str(rule["target_date"] or "").strip()
        if not city or not target_date:
            return None
        return city, target_date

    def _maybe_sync_stream_subscriptions(
        self,
        pulse_state: AutopilotPulseState,
        *,
        stream_bridge: Any | None,
        monotonic: MonotonicFn,
        force: bool = False,
    ) -> None:
        """Recompute Market Channel tokens on capital/slow cadences only."""
        if stream_bridge is None:
            return
        now_m = monotonic()
        if not force and now_m < pulse_state.next_stream_sync_at:
            return
        pulse_state.next_stream_sync_at = now_m + _jittered_delay(STREAM_SUBSCRIPTION_SYNC_SECONDS)
        try:
            from polymarket_weather_arb.adapters.polymarket.stream import (
                select_stream_tokens,
            )

            positions = self.repository.list_positions(limit=500, nonzero_only=True)
            open_orders = self.repository.list_open_orders(limit=500)
            ranked = self.repository.list_ranked_weather_opportunities(limit=200)
            market_rows: dict[str, dict[str, Any]] = {}

            def _ensure_market(market_id: str) -> None:
                if market_id in market_rows:
                    return
                row = self.repository.get_market(market_id)
                if row is None:
                    return
                payload = {
                    "yes_token_id": row["yes_token_id"],
                    "no_token_id": row["no_token_id"],
                }
                rule = self.repository.get_temperature_bucket_rule(market_id)
                if rule is not None:
                    payload["city"] = rule["city"]
                    payload["target_date"] = rule["target_date"]
                market_rows[market_id] = payload

            for pos in positions:
                _ensure_market(str(pos["market_id"]))
            for order in open_orders:
                mid = order["market_id"]
                if mid:
                    _ensure_market(str(mid))
            enriched_ranked: list[dict[str, Any]] = []
            for opp in ranked:
                market_id = str(opp["market_id"])
                _ensure_market(market_id)
                market = market_rows.get(market_id) or {}
                enriched_ranked.append(
                    {
                        "market_id": market_id,
                        "side": opp["side"] if "side" in opp.keys() else "buy_yes",
                        "city": market.get("city"),
                        "target_date": market.get("target_date"),
                    }
                )
            desired = select_stream_tokens(
                positions=[dict(p) for p in positions],
                open_orders=[dict(o) for o in open_orders],
                ranked_opportunities=enriched_ranked,
                market_rows=market_rows,
                rotation_slot=pulse_state.rotation_cursor,
            )
            stream_bridge.set_desired_tokens(
                desired.token_ids, token_to_market=desired.token_to_market
            )
        except Exception as exc:
            logger.warning("stream subscription sync failed: %s", exc)

    def _pulse_maybe_enter(
        self,
        *,
        app_mode: str,
        live_mode: bool,
        discovered: int,
        tick_start_ms: float,
        monotonic: MonotonicFn,
        deferred_count: int,
        failures: int,
        default_reason: str,
        preferred_market_ids: set[str] | None = None,
    ) -> AutopilotTickResult | None:
        """Shared entry tail for pulse paths (observe / dry-run / live)."""
        market_id = self._select_market()
        if market_id is None:
            return None
        if preferred_market_ids is not None and market_id not in preferred_market_ids:
            # Cached reprice only enters within the group just evaluated.
            return None
        # Active intent gate: never submit a second live entry while one is open.
        analysis_row = self.repository.latest_analysis(market_id)
        if analysis_row is None:
            return None
        side = str(analysis_row["side"] or "")
        if live_mode and side and self.repository.active_live_order_intent(market_id, side):
            result = AutopilotTickResult(
                status="skipped",
                action="skip",
                market_id=market_id,
                edge=Decimal(str(analysis_row["edge"] or 0)),
                reason=f"active live intent blocks re-entry; {default_reason}",
                blockers=[],
                discovered=discovered,
                duration_ms=int(monotonic() * 1000 - tick_start_ms),
                deferred_count=deferred_count,
                failures=failures,
                is_useful=True,
            )
            self._record_tick(result, discovered=discovered)
            return result

        edge = Decimal(str(analysis_row["edge"]))
        decision = str(analysis_row["decision"])
        llm_decision: LlmTradeDecision | None = None
        llm_error: str | None = None
        # LLM only on slow/full paths that already refreshed forecasts; skip on
        # pure cached reprice when no new revision work was done elsewhere.
        if not preferred_market_ids:
            try:
                llm_decision = self._evaluate_llm(market_id, analysis_row)
            except Exception as exc:
                llm_error = f"LLM review unavailable: {exc}"

        if app_mode == "observe":
            result = AutopilotTickResult(
                status="observed",
                action="observe",
                market_id=market_id,
                edge=edge,
                reason=_append_reason("observation mode records analysis only", llm_error),
                blockers=[],
                discovered=discovered,
                duration_ms=int(monotonic() * 1000 - tick_start_ms),
                deferred_count=deferred_count,
                failures=failures,
                is_useful=True,
            )
            self._record_tick(
                result, discovered=discovered, llm_decision=llm_decision, error=llm_error
            )
            return result

        if decision not in {"buy", "trade"} or edge < self.settings.min_edge:
            return None

        if live_mode:
            # Compliance/geoblock is a network read. Run the full live gate only
            # when a candidate can actually submit, not for every watch/reject
            # group visited by the fast quote loop.
            execution_blockers = self.collect_blockers(
                live_mode=True,
                app_mode=app_mode,
                require_fresh_reconciliation=True,
            )
            if execution_blockers.blocked:
                reason = "; ".join(execution_blockers.items)
                result = AutopilotTickResult(
                    status="blocked",
                    action="skip",
                    market_id=market_id,
                    edge=edge,
                    reason=reason,
                    blockers=execution_blockers.items,
                    discovered=discovered,
                    duration_ms=int(monotonic() * 1000 - tick_start_ms),
                    deferred_count=deferred_count,
                    failures=failures,
                    is_useful=True,
                )
                self._record_tick(
                    result,
                    discovered=discovered,
                    error=f"pulse_blocker|{reason}",
                    increment_tick_count=False,
                )
                return result
            intent_id, reasons = self._execute_live(market_id, analysis_row)
            action = _action_from_analysis_side(analysis_row["side"])
            submitted = "live order submitted" in reasons
            if submitted and intent_id is not None:
                self._notify_buy_submitted(intent_id, market_id, analysis_row)
            result = AutopilotTickResult(
                status="executed" if submitted else "rejected",
                action=action,
                market_id=market_id,
                edge=edge,
                reason=_append_reason("; ".join(reasons) or "live order submitted", llm_error),
                blockers=[],
                discovered=discovered,
                intent_id=intent_id,
                duration_ms=int(monotonic() * 1000 - tick_start_ms),
                deferred_count=deferred_count,
                failures=failures,
                is_useful=True,
            )
            self._record_tick(
                result, discovered=discovered, llm_decision=llm_decision, error=llm_error
            )
            return result

        workflow_result = self.workflow.dry_run_trade(market_id)
        action = _action_from_analysis_side(analysis_row["side"])
        result = AutopilotTickResult(
            status="executed",
            action=action,
            market_id=market_id,
            edge=edge,
            reason=_append_reason(workflow_result.summary, llm_error),
            blockers=[],
            discovered=discovered,
            duration_ms=int(monotonic() * 1000 - tick_start_ms),
            deferred_count=deferred_count,
            failures=failures,
            is_useful=True,
        )
        self._record_tick(result, discovered=discovered, llm_decision=llm_decision)
        return result

    def _tick_body(self) -> AutopilotTickResult:
        tick_start_ms = time.monotonic() * 1000
        tick_id = f"tick-{int(tick_start_ms)}"
        state = self.repository.get_autopilot_state()
        mode = state["mode"] if state is not None else "dry_run"
        tick_seconds = int(state["tick_seconds"]) if state else 300
        budget_sec = max(30, tick_seconds - 30)
        app_mode = _state_app_mode(state)
        logger.info(
            "Autopilot tick started tick_id=%s app_mode=%s mode=%s budget_sec=%s",
            tick_id,
            app_mode,
            mode,
            budget_sec,
        )
        live_mode = mode == "live"
        phase_t0 = time.monotonic()
        # A live tick owns reconciliation as its first capital phase. Do not let
        # yesterday's freshness state prevent the very read that refreshes it;
        # an unsuccessful reconciliation still fail-stops before any mutation.
        blockers = self.collect_blockers(
            live_mode=live_mode,
            app_mode=app_mode,
            require_fresh_reconciliation=not live_mode,
        )
        self._log_phase(
            tick_id,
            "compliance_blockers",
            phase_t0,
            items=len(blockers.items),
            detail="blocked" if blockers.blocked else "ok",
        )
        discovered = 0
        deferred_count = 0
        rotation_backlog = 0
        budget_deferred = 0
        failures = 0
        try:
            if blockers.blocked:
                result = AutopilotTickResult(
                    status="blocked",
                    action="skip",
                    market_id=None,
                    edge=None,
                    reason="; ".join(blockers.items),
                    blockers=blockers.items,
                    discovered=0,
                    duration_ms=int(time.monotonic() * 1000 - tick_start_ms),
                )
                self._record_tick(result, discovered=0)
                return result

            # Capital path first: recon / cancel / exit must not wait on discovery
            # or research HTTP. Research uses remaining cycle budget only.
            if live_mode:
                phase_t0 = time.monotonic()
                logger.info(
                    "tick phase=reconciliation start tick_id=%s app_mode=%s budget_sec=%s",
                    tick_id,
                    app_mode,
                    budget_sec,
                )
                recon = ReconciliationService(self.client, self.repository).reconcile()
                # Fill rows must be durable before any Telegram notify/flush so a
                # failed commit never produces a one-shot fill alert that restart
                # cannot de-duplicate.
                self._commit_reconciliation_then_notify_fills(recon)
                recon_status = str(recon.get("status") or "")
                failed_stage = recon.get("failed_stage")
                self._log_phase(
                    tick_id,
                    "reconciliation",
                    phase_t0,
                    detail=f"status={recon_status} stage={failed_stage or 'none'}",
                    failure=None if recon_status == "ok" else recon_status,
                    deferred=deferred_count,
                )
                if recon_status != "ok":
                    # Fail-stop: no cancel / SELL / BUY when reconciliation is not ok.
                    signature = build_recon_alert_signature(recon)
                    reason = (
                        f"reconciliation status={recon_status} fail-stop; "
                        f"stage={failed_stage or 'unknown'}; "
                        "cancel/exit/entry blocked this tick"
                    )
                    result = AutopilotTickResult(
                        status="failed",
                        action="skip",
                        market_id=None,
                        edge=None,
                        reason=reason,
                        blockers=[f"reconciliation status={recon_status}"],
                        discovered=discovered,
                        duration_ms=int(time.monotonic() * 1000 - tick_start_ms),
                    )
                    # Load prior before overwrite so cross-cycle dedupe works.
                    prior_signature = self._load_recon_alert_signature()
                    # Persist signature in last_error so the next /app service
                    # instance can dedupe/recover without a new table/field.
                    self._record_tick(result, discovered=discovered, error=signature)
                    self._commit_recon_alert_state("failure")
                    self._notify_reconciliation_failure(
                        signature=signature,
                        prior_signature=prior_signature,
                        recon_status=recon_status,
                        failed_stage=failed_stage,
                        reason=reason,
                    )
                    return result
                self._notify_reconciliation_recovery_if_needed()

            phase_t0 = time.monotonic()
            audited, settled = self._backfill_resolved_model_signals()
            self._log_phase(
                tick_id,
                "resolution_calibration",
                phase_t0,
                items=audited,
                detail=f"settled_signals={settled}",
            )
            if live_mode:
                resolution_blocker = live_execution_blocked(self.repository)
                if resolution_blocker:
                    result = AutopilotTickResult(
                        status="blocked",
                        action="skip",
                        market_id=None,
                        edge=None,
                        reason=resolution_blocker,
                        blockers=[resolution_blocker],
                        discovered=discovered,
                        duration_ms=int(time.monotonic() * 1000 - tick_start_ms),
                    )
                    self._record_tick(result, discovered=discovered, error=resolution_blocker)
                    self.repository.connection.commit()
                    return result

            # Stale/open order lifecycle before exits/entries (live modes only).
            phase_t0 = time.monotonic()
            lifecycle_notes = self._maybe_manage_stale_orders(app_mode=app_mode)
            lifecycle_failed = any(
                "cancel_failed" in note
                or "lifecycle_failed" in note
                or "cancel_commit_failed" in note
                for note in lifecycle_notes
            )
            self._log_phase(
                tick_id,
                "stale_order_lifecycle",
                phase_t0,
                items=len(lifecycle_notes),
                detail=";".join(lifecycle_notes) if lifecycle_notes else "none",
                failure="lifecycle_failed" if lifecycle_failed else None,
                deferred=deferred_count,
            )

            # Refresh analyses for open positions so ExitGuardian sees current edge.
            phase_t0 = time.monotonic()
            if app_mode in {"micro_live", "full_live"}:
                self._refresh_position_analyses()
            redeem_executed, redeem_attempted = self._maybe_auto_redeem(
                app_mode=app_mode
            )
            # Exit before/alongside entry: reduce risk positions even if no new buy.
            # Do not combine a redemption attempt with a SELL in one tick.
            exit_executed, exit_attempted = (0, 0)
            if not redeem_attempted:
                exit_executed, exit_attempted = self._maybe_auto_exit(app_mode=app_mode)
            self._log_phase(
                tick_id,
                "position_refresh_redeem_auto_exit",
                phase_t0,
                items=redeem_attempted + exit_attempted,
                detail=(
                    f"redeem_confirmed={redeem_executed} "
                    f"redeem_attempted={redeem_attempted} "
                    f"exit_executed={exit_executed} exit_attempted={exit_attempted}"
                ),
                deferred=deferred_count,
            )

            # --- Decoupled Discovery ---
            elapsed_sec = (time.monotonic() * 1000 - tick_start_ms) / 1000
            rem_budget = budget_sec - elapsed_sec

            run_discovery = False
            if state is None or int(state["tick_count"] or 0) % 3 == 0:
                run_discovery = True
            else:
                cands = self.repository.list_candidates(
                    limit=6, status="dry_run_ready", module_id="global_temp_bucket"
                )
                if len(cands) < 6:
                    run_discovery = True

            if rem_budget >= 8.0 and run_discovery:
                phase_t0 = time.monotonic()
                logger.info(
                    "tick phase=discovery start tick_id=%s rem_budget=%.1fs",
                    tick_id,
                    rem_budget,
                )
                discovery = DiscoveryService(self.client, self.repository)
                # One shared CLOB fallback budget for weather-events + list scan.
                discovery.reset_fallback_budget()
                discovered += discovery.discover_weather_events(
                    limit=30,
                    time_budget=rem_budget,
                    rotation_slot=state["tick_count"] if state else 0,
                    reset_fallback_budget=False,
                )
                discovered += discovery.discover(limit=50, pages=1, reset_fallback_budget=False)
                clob_calls = int(getattr(discovery, "clob_book_calls", 0) or 0)
                self.repository.connection.commit()
                self._log_phase(
                    tick_id,
                    "discovery_metadata",
                    phase_t0,
                    items=discovered,
                    requests=clob_calls,
                    detail=f"clob_fallback_calls={clob_calls}",
                    deferred=deferred_count,
                )
            else:
                if not run_discovery:
                    logger.info(
                        "tick phase=discovery skipped tick_id=%s rem_budget=%.1fs (cadence decoupling)",
                        tick_id,
                        rem_budget,
                    )
                else:
                    budget_deferred += 1
                    logger.info(
                        "tick phase=discovery deferred tick_id=%s rem_budget=%.1fs",
                        tick_id,
                        rem_budget,
                    )
            # --- End Discovery ---

            elapsed_sec = (time.monotonic() * 1000 - tick_start_ms) / 1000
            rem_budget = max(0.0, budget_sec - elapsed_sec)
            if rem_budget >= 5.0:
                phase_t0 = time.monotonic()
                analyzed, rb, bd, fail = self._prepare_global_bucket_candidates(
                    time_budget=rem_budget
                )
                rotation_backlog += rb
                budget_deferred += bd
                failures += fail
                deferred_count = rotation_backlog + budget_deferred
                self.repository.connection.commit()
                self._log_phase(
                    tick_id,
                    "candidate_analysis",
                    phase_t0,
                    items=analyzed,
                    detail=f"rotation_backlog={rb} budget_deferred={bd} failures={fail}",
                    deferred=deferred_count,
                )
            else:
                budget_deferred += 1
                deferred_count = rotation_backlog + budget_deferred
                logger.info(
                    "tick phase=candidate_analysis deferred tick_id=%s rem_budget=%.1fs budget_deferred=%s",
                    tick_id,
                    rem_budget,
                    budget_deferred,
                )

            market_id = self._select_market()
            if market_id is None:
                idle_reason = "no actionable weather market found"
                if lifecycle_notes:
                    idle_reason = f"{idle_reason}; {'; '.join(lifecycle_notes)}"
                result = AutopilotTickResult(
                    status="failed" if lifecycle_failed else "idle",
                    action="skip",
                    market_id=None,
                    edge=None,
                    reason=idle_reason,
                    blockers=[],
                    discovered=discovered,
                    auto_exit_executed=exit_executed,
                    auto_exit_attempted=exit_attempted,
                    auto_redeem_executed=redeem_executed,
                    auto_redeem_attempted=redeem_attempted,
                    duration_ms=int(time.monotonic() * 1000 - tick_start_ms),
                    deferred_count=deferred_count,
                    rotation_backlog=rotation_backlog,
                    budget_deferred=budget_deferred,
                    failures=failures,
                    is_useful=(not lifecycle_failed),
                )
                self._record_tick(
                    result,
                    discovered=discovered,
                    error=idle_reason if lifecycle_failed else None,
                )
                logger.info(
                    "tick complete tick_id=%s status=%s duration_ms=%s deferred=%s",
                    tick_id,
                    result.status,
                    result.duration_ms,
                    deferred_count,
                )
                return result

            phase_t0 = time.monotonic()
            self._prepare_market(market_id)
            analysis_row = self.repository.latest_analysis(market_id)
            if analysis_row is None:
                result = AutopilotTickResult(
                    status="failed",
                    action="skip",
                    market_id=market_id,
                    edge=None,
                    reason="analysis failed",
                    blockers=[],
                    discovered=discovered,
                    auto_exit_executed=exit_executed,
                    auto_exit_attempted=exit_attempted,
                    auto_redeem_executed=redeem_executed,
                    auto_redeem_attempted=redeem_attempted,
                    duration_ms=int(time.monotonic() * 1000 - tick_start_ms),
                    deferred_count=deferred_count,
                    rotation_backlog=rotation_backlog,
                    budget_deferred=budget_deferred,
                    failures=failures,
                )
                self._record_tick(result, discovered=discovered)
                return result

            edge = Decimal(str(analysis_row["edge"]))
            decision = str(analysis_row["decision"])
            llm_decision: LlmTradeDecision | None = None
            llm_error: str | None = None
            try:
                llm_decision = self._evaluate_llm(market_id, analysis_row)
            except Exception as exc:
                llm_error = f"LLM review unavailable: {exc}"
            if app_mode == "observe":
                result = AutopilotTickResult(
                    status="observed",
                    action="observe",
                    market_id=market_id,
                    edge=edge,
                    reason="observation mode records analysis only",
                    blockers=[],
                    discovered=discovered,
                    auto_exit_executed=exit_executed,
                    auto_exit_attempted=exit_attempted,
                    auto_redeem_executed=redeem_executed,
                    auto_redeem_attempted=redeem_attempted,
                    duration_ms=int(time.monotonic() * 1000 - tick_start_ms),
                    deferred_count=deferred_count,
                    rotation_backlog=rotation_backlog,
                    budget_deferred=budget_deferred,
                    failures=failures,
                    is_useful=True,
                )
                self._record_tick(
                    result, discovered=discovered, llm_decision=llm_decision, error=llm_error
                )
                return result
            if decision not in {"buy", "trade"} or edge < self.settings.min_edge:
                result = AutopilotTickResult(
                    status="skipped",
                    action="skip",
                    market_id=market_id,
                    edge=edge,
                    reason=_append_reason(
                        f"edge {edge} below min {self.settings.min_edge} or decision={decision}",
                        llm_error,
                    ),
                    blockers=[],
                    discovered=discovered,
                    auto_exit_executed=exit_executed,
                    auto_exit_attempted=exit_attempted,
                    auto_redeem_executed=redeem_executed,
                    auto_redeem_attempted=redeem_attempted,
                    duration_ms=int(time.monotonic() * 1000 - tick_start_ms),
                    deferred_count=deferred_count,
                    rotation_backlog=rotation_backlog,
                    budget_deferred=budget_deferred,
                    failures=failures,
                    is_useful=True,
                )
                self._record_tick(
                    result, discovered=discovered, llm_decision=llm_decision, error=llm_error
                )
                return result

            if live_mode:
                intent_id, reasons = self._execute_live(market_id, analysis_row)
                action = _action_from_analysis_side(analysis_row["side"])
                submitted = "live order submitted" in reasons
                if submitted and intent_id is not None:
                    self._notify_buy_submitted(intent_id, market_id, analysis_row)
                self._log_phase(
                    tick_id,
                    "final_entry",
                    phase_t0,
                    items=1 if submitted else 0,
                    detail=f"submitted={submitted} market={market_id}",
                    failure=None if submitted else "not_submitted",
                )
                result = AutopilotTickResult(
                    status="executed" if submitted else "rejected",
                    action=action,
                    market_id=market_id,
                    edge=edge,
                    reason=_append_reason("; ".join(reasons) or "live order submitted", llm_error),
                    blockers=[],
                    discovered=discovered,
                    intent_id=intent_id,
                    auto_exit_executed=exit_executed,
                    auto_exit_attempted=exit_attempted,
                    auto_redeem_executed=redeem_executed,
                    auto_redeem_attempted=redeem_attempted,
                    duration_ms=int(time.monotonic() * 1000 - tick_start_ms),
                    deferred_count=deferred_count,
                    rotation_backlog=rotation_backlog,
                    budget_deferred=budget_deferred,
                    failures=failures,
                    is_useful=True,
                )
                self._record_tick(
                    result, discovered=discovered, llm_decision=llm_decision, error=llm_error
                )
            else:
                workflow_result = self.workflow.dry_run_trade(market_id)
                action = _action_from_analysis_side(analysis_row["side"])
                self._log_phase(
                    tick_id,
                    "final_entry",
                    phase_t0,
                    items=1,
                    detail=f"dry_run market={market_id}",
                )
                result = AutopilotTickResult(
                    status="executed",
                    action=action,
                    market_id=market_id,
                    edge=edge,
                    reason=_append_reason(workflow_result.summary, llm_error),
                    blockers=[],
                    discovered=discovered,
                    auto_exit_executed=exit_executed,
                    auto_exit_attempted=exit_attempted,
                    auto_redeem_executed=redeem_executed,
                    auto_redeem_attempted=redeem_attempted,
                    duration_ms=int(time.monotonic() * 1000 - tick_start_ms),
                    deferred_count=deferred_count,
                    rotation_backlog=rotation_backlog,
                    budget_deferred=budget_deferred,
                    failures=failures,
                    is_useful=True,
                )
                self._record_tick(result, discovered=discovered, llm_decision=llm_decision)
            logger.info(
                "tick complete tick_id=%s status=%s duration_ms=%s deferred=%s",
                tick_id,
                result.status,
                result.duration_ms,
                deferred_count,
            )
            return result
        except Exception as exc:
            logger.exception("autopilot loop failed tick_id=%s", tick_id)
            result = AutopilotTickResult(
                status="failed",
                action="skip",
                market_id=None,
                edge=None,
                reason=str(exc),
                blockers=[str(exc)],
                discovered=discovered,
                duration_ms=int(time.monotonic() * 1000 - tick_start_ms),
                deferred_count=deferred_count,
                is_useful=False,
            )
            self._record_tick(result, discovered=discovered, error=str(exc))
            return result

    def run_loop(
        self,
        *,
        tick_seconds: int,
        sleep: SleepFn = time.sleep,
        monotonic: MonotonicFn = time.monotonic,
        max_ticks: int | None = None,
    ) -> None:
        tick_count = 0
        while max_ticks is None or tick_count < max_ticks:
            cycle_started = monotonic()
            state = self.repository.get_autopilot_state()
            if state is not None and bool(state["enabled"]):
                self.tick()
                self.repository.connection.commit()
            tick_count += 1
            if max_ticks is not None and tick_count >= max_ticks:
                break
            sleep(
                remaining_cycle_delay(
                    tick_seconds=tick_seconds,
                    cycle_started=cycle_started,
                    monotonic=monotonic,
                )
            )

    def _select_market(self) -> str | None:
        app_mode = _state_app_mode(self.repository.get_autopilot_state())
        live_entry_policy = _execution_mode_for_app_mode(app_mode) == "live"
        for opportunity in self.repository.list_ranked_weather_opportunities(limit=200):
            market_id = str(opportunity["market_id"])
            if str(opportunity["decision"] or "") not in {"buy", "trade"}:
                continue
            if Decimal(str(opportunity["edge"] or 0)) < self.settings.min_edge:
                continue
            if self.repository.market_has_live_activity(market_id):
                continue
            if self.repository.active_live_sibling_market(market_id) is not None:
                continue
            market = self.repository.get_market(market_id)
            analysis = self.repository.latest_analysis(market_id)
            if market is None or analysis is None:
                continue
            if live_entry_policy:
                policy_rejection = self._v5_live_entry_rejection_reason(market, analysis)
                if policy_rejection is not None:
                    continue
            if self._live_opportunity_already_recorded(market, analysis):
                continue
            if self._retire_if_unorderable(market):
                continue
            rejection = self._candidate_entry_rejection_reason(market, analysis)
            if rejection is not None:
                self._record_minimum_order_blocker(market, analysis, rejection)
                continue
            return market_id
        candidates = [
            row
            for row in self.repository.list_candidates(
                limit=GLOBAL_BUCKET_CANDIDATE_SCAN_LIMIT,
                status="dry_run_ready",
            )
            if row["module_id"] in {"weather", "global_temp_bucket"}
        ]
        ranked: list[tuple[int, str]] = []
        from polymarket_weather_arb.domain.market_eligibility import local_weather_day

        for candidate in candidates:
            market_id = str(candidate["market_id"])
            if market_id.startswith("demo-"):
                continue
            if self.repository.market_has_live_activity(market_id):
                continue
            if self.repository.active_live_sibling_market(market_id) is not None:
                continue
            row = self.repository.get_market(market_id)
            if row is None:
                continue
            analysis = self.repository.latest_analysis(market_id)
            if analysis is not None and str(analysis["decision"] or "") in {"buy", "trade"}:
                if live_entry_policy:
                    policy_rejection = self._v5_live_entry_rejection_reason(row, analysis)
                    if policy_rejection is not None:
                        continue
                if self._live_opportunity_already_recorded(row, analysis):
                    continue
                rejection = self._candidate_entry_rejection_reason(row, analysis)
                if rejection is not None:
                    self._record_minimum_order_blocker(row, analysis, rejection)
                    continue
            if self._retire_if_unorderable(row):
                continue
            if row["module_id"] == "weather" and not _market_supported_by_weather_provider(
                self.repository, market_id, self.settings.weather_provider
            ):
                continue
            timezone_name = (
                str(candidate["settlement_timezone"] or "")
                if str(candidate["module_id"] or "") == "global_temp_bucket"
                else ""
            )
            local_day = local_weather_day(
                title=row["title"],
                timezone_name=timezone_name or None,
            )
            event_day = event_date_from_market_title(row["title"], today=local_day)
            if event_day is None:
                rank = 30
            else:
                rank = abs((event_day - local_day).days)
                if event_day < local_day:
                    # Past local target date: shared eligibility should already exclude;
                    # keep as last-resort deprioritization if parsing differs.
                    rank += 100
            ranked.append((rank, market_id))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0])
        return ranked[0][1]

    def _live_opportunity_already_recorded(self, market, analysis) -> bool:
        revision = self.repository.forecast_revision_for_analysis(int(analysis["id"]))
        if revision is None:
            return False
        side = str(analysis["side"] or "")
        if side == "buy_yes":
            token_id = market["yes_token_id"]
        elif side == "buy_no":
            token_id = market["no_token_id"]
        else:
            return False
        key = live_order_opportunity_key(
            market_id=str(market["id"]),
            side=side,
            token_id=str(token_id) if token_id else None,
            opportunity_id=f"forecast:{revision}",
        )
        return self.repository.order_intent_by_idempotency_key(key) is not None

    def _prepare_global_bucket_candidates(
        self, *, max_groups: int = 6, time_budget: float = float("inf")
    ) -> tuple[int, int, int, int]:
        candidates = self.repository.list_candidates(
            limit=GLOBAL_BUCKET_CANDIDATE_SCAN_LIMIT,
            status="dry_run_ready",
            module_id="global_temp_bucket",
        )
        start_time = time.monotonic()

        groups: dict[tuple[str, str], list[str]] = {}
        universe_groups: set[tuple[str, str]] = set()
        live_groups: set[tuple[str, str]] = set()
        city_timezones: dict[str, str] = {}

        for candidate in candidates:
            market_id = str(candidate["market_id"])
            market = self.repository.get_market(market_id)
            if market is None or self._retire_if_unorderable(
                market, location_hint=str(candidate["city"] or "")
            ):
                continue
            group = (
                str(candidate["city"] or market_id),
                str(candidate["target_date"] or ""),
            )
            timezone_name = str(candidate["settlement_timezone"] or "")
            if timezone_name:
                city_timezones[group[0]] = timezone_name
            universe_groups.add(group)
            has_live = self.repository.market_has_live_activity(market_id)
            if has_live:
                live_groups.add(group)

            if candidate["best_bid"] is None and candidate["best_ask"] is None:
                continue

            analysis = self.repository.latest_analysis(market_id)
            max_age = _weather_analysis_max_age(
                city=str(candidate["city"] or ""),
                target_date=str(candidate["target_date"] or ""),
                timezone_name=timezone_name or None,
            )
            if (
                analysis is not None
                and not has_live
                and _row_timestamp_is_fresh(analysis["created_at"], max_age=max_age)
            ):
                continue

            if group not in groups:
                groups[group] = []
            groups[group].append(market_id)

        state = self.repository.get_autopilot_state()
        rotation_slot = state["tick_count"] if state else 0
        ordered_groups = select_fair_analysis_groups(
            list(groups.keys()),
            rotation_slot,
            city_timezones=city_timezones,
        )

        selected_groups: set[tuple[str, str]] = set()
        market_ids: list[str] = []
        rotation_backlog = 0

        date_bands = {}
        for group in list(groups.keys()):
            date_bands[group[1]] = date_bands.get(group[1], 0) + 1

        for group in ordered_groups:
            if group not in live_groups and len(selected_groups) >= max_groups:
                rotation_backlog += len(groups[group])
                continue
            selected_groups.add(group)
            market_ids.extend(groups[group])

        logger.info(
            "Analysis candidates: "
            f"scanned_buckets={len(candidates)} universe_groups={len(universe_groups)} "
            f"ready_groups={len(groups)} date_bands={date_bands} "
            f"selected_groups={len(selected_groups)} rotation_backlog={rotation_backlog}"
        )

        if not market_ids:
            return 0, rotation_backlog, 0, 0

        # Keep batch research when budget remains; avoid N sequential HTTP chains.
        if (time.monotonic() - start_time) > time_budget:
            return 0, rotation_backlog, len(market_ids), 0
        # LLM review remains an explicit research tool. Production Autopilot
        # does not spend tokens or alter pricing until a future policy version
        # deliberately re-enables it with out-of-sample evidence.
        analyzed, failures = self.workflow.research_global_bucket_batch(
            market_ids,
            allow_llm=False,
        )
        if failures:
            logger.warning(
                "global bucket analysis skipped: failures=%s first=%s",
                len(failures),
                failures[0],
            )
        return analyzed, rotation_backlog, 0, len(failures)

    def _prepare_market(self, market_id: str) -> None:
        analysis = self.repository.latest_analysis(market_id)
        bucket_rule = self.repository.get_temperature_bucket_rule(market_id)
        max_age = timedelta(minutes=30)
        if bucket_rule is not None:
            max_age = _weather_analysis_max_age(
                city=str(bucket_rule["city"] or ""),
                target_date=str(bucket_rule["target_date"] or ""),
                timezone_name=str(bucket_rule["settlement_timezone"] or "") or None,
            )
        if analysis is not None and _row_timestamp_is_fresh(
            analysis["created_at"], max_age=max_age
        ):
            return
        if self.repository.latest_pricing_snapshot(market_id) is None:
            row = self.repository.get_market(market_id)
            if row is not None:
                try:
                    from polymarket_weather_arb.domain.markets import Market

                    market = Market(
                        id=row["id"],
                        title=row["title"],
                        slug=row["slug"],
                        description=row["description"],
                        yes_token_id=row["yes_token_id"],
                        no_token_id=row["no_token_id"],
                        is_weather=bool(row["is_weather"]),
                    )
                    snapshot, raw_snapshot = self.client.get_order_book(market)
                    self.repository.save_market_snapshot(
                        snapshot,
                        raw_snapshot,
                        token_id=snapshot.token_id or market.yes_token_id or market.no_token_id,
                    )
                except Exception:
                    pass
        self.workflow.research_market(market_id)

    def _execute_live(self, market_id: str, analysis_row) -> tuple[int | None, list[str]]:
        market = self.repository.get_market(market_id)
        if market is None:
            return None, ["unknown market"]
        module_id = str(market["module_id"] or "weather")
        if module_id not in {"weather", "global_temp_bucket"}:
            return None, [f"live trade not enabled for module {market['module_id']}"]
        live_ids = live_market_ids_from_settings(self.settings)
        # Empty LIVE_MARKET_IDS = open whitelist (same posture as operator --full-auto).
        if live_ids and market_id not in live_ids:
            return None, ["market is not whitelisted in LIVE_MARKET_IDS"]
        app_mode = _state_app_mode(self.repository.get_autopilot_state())
        profile = get_profile(profile_name_for_app_mode(app_mode))
        override = None
        if app_mode != "full_live":
            override = self.repository.effective_strategy_override(market_id, profile.name)
            if not live_auto_enabled_by_override(override):
                return None, ["live auto override is not enabled"]
        policy_rejection = self._v5_live_entry_rejection_reason(market, analysis_row)
        if policy_rejection is not None:
            return None, [policy_rejection]
        if module_id == "global_temp_bucket":
            analysis_row, revalidation_error = self._revalidate_d0_live_analysis(
                market_id,
                analysis_row,
            )
            if revalidation_error:
                return None, [revalidation_error]
            if analysis_row is None:
                return None, ["D0 live revalidation did not produce an analysis"]
            version_error = _global_entry_model_version_error(analysis_row)
            if version_error is not None:
                return None, [version_error]
            if str(analysis_row["decision"] or "") not in {"buy", "trade"}:
                return None, [
                    "D0 live revalidation no longer supports entry "
                    f"(decision={analysis_row['decision']})"
                ]
            if Decimal(str(analysis_row["edge"] or 0)) < self.settings.min_edge:
                return None, [
                    "D0 live revalidation edge is below the configured minimum "
                    f"(edge={analysis_row['edge']}, minimum={self.settings.min_edge})"
                ]
        # Always refresh token-specific book before live decision; never fall back
        # to a pre-existing stale snapshot when refresh fails.
        analysis = analysis_from_row(analysis_row)
        forecast_row = self.repository.latest_forecast(market_id)
        if module_id == "global_temp_bucket" and _analysis_uses_stale_weather(analysis_row):
            forecast_age = age_seconds(forecast_row["fetched_at"]) if forecast_row else None
            stale_live_limit = min(int(self.settings.stale_forecast_seconds), 90 * 60)
            if forecast_age is None or forecast_age > stale_live_limit:
                return None, [
                    "stale-if-error forecast is too old for live entry "
                    f"(age_seconds={forecast_age}, maximum={stale_live_limit})"
                ]
        snapshot_row, refresh_error = self._refresh_live_order_book(
            market_id=market_id,
            market_row=market,
            analysis=analysis,
        )
        if refresh_error:
            return None, [refresh_error]
        if module_id == "global_temp_bucket":
            fresh_ask = snapshot_row["best_ask"] if snapshot_row is not None else None
            analyzed_price = analysis.reference_price
            if fresh_ask is None:
                return None, ["order book refresh failed: fresh ask unavailable"]
            if analyzed_price is None or Decimal(str(fresh_ask)) != Decimal(str(analyzed_price)):
                analysis_row, reprice_error = self._reprice_changed_live_quote(
                    market_id=market_id,
                    market_row=market,
                    fresh_ask=Decimal(str(fresh_ask)),
                )
                if reprice_error:
                    return None, [reprice_error]
                if analysis_row is None:
                    return None, ["fresh quote reprice did not produce an analysis"]
                version_error = _global_entry_model_version_error(analysis_row)
                if version_error is not None:
                    return None, [version_error]
                analysis = analysis_from_row(analysis_row)
                if analysis.decision not in {"buy", "trade"}:
                    return None, [
                        "fresh quote reprice no longer supports entry "
                        f"(decision={analysis.decision})"
                    ]
                if analysis.edge is None or analysis.edge < self.settings.min_edge:
                    return None, [
                        "fresh quote reprice edge is below the configured minimum "
                        f"(edge={analysis.edge}, minimum={self.settings.min_edge})"
                    ]
        today = datetime.now(timezone.utc).date().isoformat()
        if module_id == "global_temp_bucket":
            rule = parse_global_temperature_bucket_rule(market["title"], market["description"])
            stored_rule = self.repository.get_temperature_bucket_rule(market_id)
            if stored_rule is not None and stored_rule["settlement_timezone"]:
                rule = with_settlement_timezone(rule, str(stored_rule["settlement_timezone"]))
        else:
            rule = parse_resolution_rule(market["title"], market["description"])
        profile_settings = settings_for_override(
            settings_for_profile(self.settings, profile), override
        )
        try:
            market_payload = json.loads(market["raw_payload"]) if market["raw_payload"] else {}
        except Exception:
            market_payload = {}
        service = TradingService(profile_settings, self.client, self.repository)
        staged_headroom = (
            self._risk_adjusted_entry_headroom(market, analysis_row)
            if module_id == "global_temp_bucket"
            else None
        )
        if staged_headroom is not None:
            performance = self._entry_performance_calibration(market, analysis_row)
            raw_headroom = self._raw_risk_adjusted_entry_headroom(
                market,
                analysis_row,
                performance_multiplier=performance.multiplier,
            )
            headroom_reasons = [f"risk-adjusted entry headroom={staged_headroom} USDC"]
            if self._uses_full_live_v5_configured_sizing(market):
                headroom_reasons.append(
                    f"{WEATHER_ENTRY_POLICY_VERSION} full_live uses configured hard-cap sizing; "
                    "legacy calibration remains shadow-only "
                    f"(shadow_multiplier={performance.multiplier}, "
                    f"shadow_headroom={raw_headroom} USDC)"
                )
            elif staged_headroom > raw_headroom:
                headroom_reasons.append(
                    f"exchange minimum uplift from {raw_headroom} to {staged_headroom} USDC"
                )
            analysis = replace(
                analysis,
                reasons=[
                    *analysis.reasons,
                    *headroom_reasons,
                    performance.reason,
                ],
            )
            staged_rejection = self._staged_entry_rejection_reason(
                market,
                analysis_row,
                headroom=staged_headroom,
            )
            if staged_rejection is not None:
                return None, [staged_rejection]
        forecast_revision = self.repository.forecast_revision_for_analysis(int(analysis_row["id"]))
        opportunity_id = f"forecast:{forecast_revision}" if forecast_revision is not None else None
        if opportunity_id is not None:
            analysis = replace(
                analysis,
                reasons=[*analysis.reasons, f"entry opportunity={opportunity_id}"],
            )
        policy_rejection = self._v5_live_entry_rejection_reason(market, analysis_row)
        if policy_rejection is not None:
            return None, [policy_rejection]
        return service.trade(
            analysis=analysis,
            yes_token_id=market["yes_token_id"],
            no_token_id=market["no_token_id"],
            context=risk_context(
                self.repository,
                market_id,
                today,
                snapshot_row,
                forecast_row,
                rule,
                reconciliation_fresh=_fresh_reconciliation(
                    self.repository.latest_successful_reconciliation()
                ),
            ),
            dry_run=False,
            source_grade=_forecast_source_grade(forecast_row),
            allow_research_forecast_live=module_id == "global_temp_bucket",
            on_submitted=lambda _intent_id: self.repository.connection.commit(),
            market_payload=market_payload if isinstance(market_payload, dict) else None,
            max_notional_override=staged_headroom,
            opportunity_id=opportunity_id,
        )

    def _reprice_changed_live_quote(
        self,
        *,
        market_id: str,
        market_row,
        fresh_ask: Decimal,
    ):
        """Recompute fee-aware bucket pricing after the final quote refresh.

        The fresh target-token snapshot has already been persisted. Reuse the
        network-free event-group repricer so a better ask can proceed while a
        worse ask is still rejected when it removes the edge.
        """
        rule = self.repository.get_temperature_bucket_rule(market_id)
        if rule is None:
            return None, "fresh quote reprice requires a parsed bucket rule"
        siblings = self.repository.list_temperature_bucket_rules(
            limit=100,
            city=str(rule["city"] or ""),
            target_date=str(rule["target_date"] or ""),
        )
        market_ids = [str(row["market_id"]) for row in siblings]
        if market_id not in market_ids:
            market_ids.append(market_id)
        analyzed, failures, slow_reason, _revision = (
            self.workflow.reprice_global_bucket_group_cached(market_ids)
        )
        if slow_reason:
            return None, (
                f"order book changed after analysis; fresh quote reprice deferred ({slow_reason})"
            )
        if failures:
            return None, "fresh quote reprice failed: " + "; ".join(failures[:3])
        if analyzed < 1:
            return None, "fresh quote reprice produced no analyses"
        latest = self.repository.latest_analysis(market_id)
        if latest is None:
            return None, "fresh quote reprice analysis is unavailable"
        repriced = analysis_from_row(latest)
        if repriced.reference_price is None or repriced.reference_price != fresh_ask:
            return None, (
                "fresh quote reprice did not bind the submitted ask "
                f"(repriced={repriced.reference_price}, fresh_ask={fresh_ask})"
            )
        return latest, None

    def _revalidate_d0_live_analysis(self, market_id: str, analysis_row):
        """Refresh D0 official evidence before a real BUY can reach TradingService."""
        rule = self.repository.get_temperature_bucket_rule(market_id)
        if rule is None:
            return analysis_row, "D0 live revalidation requires a parsed bucket rule"
        city = str(rule["city"] or "").strip()
        target_date = str(rule["target_date"] or "").strip()
        if not city or not target_date:
            return analysis_row, "D0 live revalidation requires city and target date"

        from polymarket_weather_arb.domain.market_eligibility import try_local_weather_day

        timezone_name = str(rule["settlement_timezone"] or "").strip()
        local_day = try_local_weather_day(
            location_hint=city,
            timezone_name=timezone_name or None,
        )
        if local_day is None:
            return analysis_row, "market timezone unknown; live entry cannot be revalidated"
        if target_date[:10] != local_day.isoformat():
            return analysis_row, None

        group_key = (city.casefold(), target_date[:10])
        now_m = time.monotonic()
        last_refresh = self._d0_live_revalidated_at.get(group_key, 0.0)
        if last_refresh and now_m - last_refresh < D0_LIVE_REVALIDATION_SECONDS:
            latest = self.repository.latest_analysis(market_id)
            return latest, None

        siblings = self.repository.list_temperature_bucket_rules(
            limit=100,
            city=city,
            target_date=target_date,
        )
        market_ids = [str(row["market_id"]) for row in siblings]
        if market_id not in market_ids:
            market_ids.append(market_id)
        analyzed, failures = self.workflow.research_global_bucket_batch(
            market_ids,
            allow_llm=False,
        )
        if failures:
            return analysis_row, "D0 live revalidation failed: " + "; ".join(failures[:3])
        if analyzed < 1:
            return analysis_row, "D0 live revalidation produced no analyses"
        self._d0_live_revalidated_at[group_key] = now_m
        latest = self.repository.latest_analysis(market_id)
        if latest is None:
            return analysis_row, "D0 live revalidation analysis is unavailable"
        return latest, None

    def _staged_entry_headroom(self, market_row) -> Decimal:
        bucket_rule = self.repository.get_temperature_bucket_rule(str(market_row["id"]))
        timezone_name = (
            str(bucket_rule["settlement_timezone"] or "") if bucket_rule is not None else ""
        )
        cap = min(
            _staged_entry_cap(
                str(market_row["title"] or ""),
                timezone_name=timezone_name or None,
            ),
            self.settings.max_market_usdc,
        )
        deployed = self.repository.live_buy_notional_for_market(str(market_row["id"]))
        return max(Decimal("0"), cap - deployed)

    def _risk_adjusted_entry_headroom(self, market_row, analysis_row) -> Decimal:
        base_headroom = self._staged_entry_headroom(market_row)
        performance = self._entry_performance_calibration(market_row, analysis_row)
        history_multiplier = (
            Decimal("1")
            if self._uses_full_live_v5_configured_sizing(market_row)
            else performance.multiplier
        )
        adjusted = _risk_adjusted_entry_cap(
            base_headroom,
            analysis_row,
            history_multiplier=history_multiplier,
            max_order_usdc=self.settings.max_order_usdc,
            max_daily_usdc=self.settings.max_daily_usdc,
        )
        return self._minimum_viable_entry_headroom(
            market_row,
            analysis_row,
            adjusted=adjusted,
            staged_headroom=base_headroom,
            allow_uplift=history_multiplier >= Decimal("1"),
        )

    def _uses_full_live_v5_configured_sizing(self, market_row) -> bool:
        """Keep legacy calibration observable without deadlocking V5 full-live entry."""
        return (
            str(market_row["module_id"] or "") == "global_temp_bucket"
            and _state_app_mode(self.repository.get_autopilot_state()) == "full_live"
        )

    def _raw_risk_adjusted_entry_headroom(
        self,
        market_row,
        analysis_row,
        *,
        performance_multiplier: Decimal | None = None,
    ) -> Decimal:
        base_headroom = self._staged_entry_headroom(market_row)
        multiplier = performance_multiplier
        if multiplier is None:
            multiplier = self._entry_performance_calibration(
                market_row,
                analysis_row,
            ).multiplier
        return _risk_adjusted_entry_cap(
            base_headroom,
            analysis_row,
            history_multiplier=multiplier,
            max_order_usdc=self.settings.max_order_usdc,
            max_daily_usdc=self.settings.max_daily_usdc,
        )

    def _minimum_viable_entry_headroom(
        self,
        market_row,
        analysis_row,
        *,
        adjusted: Decimal,
        staged_headroom: Decimal,
        allow_uplift: bool = True,
    ) -> Decimal:
        """Lift an accepted signal only to its exchange minimum within hard caps."""
        adjusted = max(Decimal("0"), Decimal(str(adjusted)))
        if adjusted <= 0 or _analysis_uses_stale_weather(analysis_row):
            return adjusted
        try:
            market_payload = (
                json.loads(market_row["raw_payload"]) if market_row["raw_payload"] else {}
            )
        except Exception:
            market_payload = {}
        required = minimum_buy_cash_required(
            analysis_from_row(analysis_row),
            market_payload=market_payload if isinstance(market_payload, dict) else None,
        )
        if required is None or required <= adjusted:
            return adjusted
        if not allow_uplift:
            # A poor calibrated history intentionally reduced this trade below
            # the exchange floor. Raising it back to the minimum would invert
            # the risk signal and spend more precisely when trust is weakest.
            return adjusted
        today = datetime.now(timezone.utc).date().isoformat()
        daily_remaining = max(
            Decimal("0"),
            self.settings.max_daily_usdc - self.repository.daily_order_notional(today),
        )
        hard_headroom = min(
            max(Decimal("0"), staged_headroom),
            self.settings.max_order_usdc,
            daily_remaining,
            Decimal("25"),
        )
        return required if required <= hard_headroom else adjusted

    def _entry_performance_calibration(self, market_row, analysis_row):
        bucket_rule = self.repository.get_temperature_bucket_rule(str(market_row["id"]))
        timezone_name = (
            str(bucket_rule["settlement_timezone"] or "") if bucket_rule is not None else ""
        )
        horizon = _staged_entry_horizon(
            str(market_row["title"] or ""),
            timezone_name=timezone_name or None,
        )
        try:
            reference_price = Decimal(str(analysis_row["reference_price"] or 0))
        except (KeyError, IndexError, TypeError):
            reference_price = Decimal("0")
        entry_performance = self.calibration_service.entry_performance(
            horizon=horizon,
            reference_price=reference_price,
        )
        try:
            model_version = str(analysis_row["model_version"] or "")
        except (KeyError, IndexError, TypeError):
            model_version = ""
        model_sizing = self.calibration_service.weather_model_sizing(
            market_id=str(market_row["id"]),
            model_version=model_version,
            horizon=horizon,
        )
        return replace(
            entry_performance,
            multiplier=min(entry_performance.multiplier, model_sizing.multiplier),
            reason=f"{entry_performance.reason}; {model_sizing.reason}",
        )

    def _retire_if_unorderable(self, market_row, *, location_hint: str | None = None) -> bool:
        """Retire terminal candidates before any weather or CLOB refresh work."""
        try:
            payload = json.loads(market_row["raw_payload"]) if market_row["raw_payload"] else {}
        except Exception:
            payload = {}
        bucket_rule = self.repository.get_temperature_bucket_rule(str(market_row["id"]))
        timezone_name = (
            str(bucket_rule["settlement_timezone"] or "") if bucket_rule is not None else None
        )
        eligibility = evaluate_market_orderability(
            raw_payload=payload if isinstance(payload, dict) else {},
            title=market_row["title"],
            close_time=market_row["close_time"],
            location_hint=location_hint,
            timezone_name=timezone_name,
        )
        if eligibility.orderable:
            return False
        self.repository.mark_candidate(
            str(market_row["id"]),
            eligibility.terminal_status or "expired",
            eligibility.reason or "market is not orderable",
        )
        return True

    def _staged_entry_rejection_reason(
        self,
        market_row,
        analysis_row,
        *,
        headroom: Decimal | None = None,
    ) -> str | None:
        """Reject a scale-in whose remaining stage cap cannot form a valid order."""
        available = (
            self._risk_adjusted_entry_headroom(market_row, analysis_row)
            if headroom is None
            else headroom
        )
        if available <= 0:
            return "staged entry cap already reached for this target horizon"
        available = min(available, self.settings.max_order_usdc, Decimal("25"))
        return self._buy_preflight_rejection_reason(
            market_row,
            analysis_row,
            max_notional=available,
        )

    def _candidate_entry_rejection_reason(self, market_row, analysis_row) -> str | None:
        if str(market_row["module_id"] or "") == "global_temp_bucket":
            version_error = _global_entry_model_version_error(analysis_row)
            if version_error is not None:
                return version_error
            return self._staged_entry_rejection_reason(market_row, analysis_row)
        return self._buy_preflight_rejection_reason(
            market_row,
            analysis_row,
            max_notional=min(self.settings.max_order_usdc, Decimal("25")),
        )

    def _v5_live_entry_rejection_reason(self, market_row, analysis_row) -> str | None:
        """Enforce the non-negotiable live-entry boundary for weather V5."""
        if str(market_row["module_id"] or "") != "global_temp_bucket":
            return None
        version_error = _global_entry_model_version_error(analysis_row)
        if version_error is not None:
            return version_error
        try:
            edge = Decimal(str(analysis_row["edge"]))
        except (InvalidOperation, KeyError, IndexError, TypeError):
            return f"{WEATHER_ENTRY_POLICY_VERSION} live entry requires a numeric edge"
        if edge < WEATHER_V5_LIVE_MIN_EDGE:
            return (
                f"{WEATHER_ENTRY_POLICY_VERSION} live edge is below "
                f"{WEATHER_V5_LIVE_MIN_EDGE} (edge={edge})"
            )
        try:
            reference_price = Decimal(str(analysis_row["reference_price"]))
        except (InvalidOperation, KeyError, IndexError, TypeError):
            return f"{WEATHER_ENTRY_POLICY_VERSION} live entry requires an executable ask"
        if reference_price < WEATHER_V5_LIVE_MIN_PRICE:
            return (
                f"{WEATHER_ENTRY_POLICY_VERSION} live ask is below "
                f"{WEATHER_V5_LIVE_MIN_PRICE} (ask={reference_price})"
            )
        bucket_rule = self.repository.get_temperature_bucket_rule(str(market_row["id"]))
        timezone_name = (
            str(bucket_rule["settlement_timezone"] or "") if bucket_rule is not None else ""
        )
        horizon = _staged_entry_horizon(
            str(market_row["title"] or ""),
            timezone_name=timezone_name or None,
        )
        if horizon == "D0":
            return f"{WEATHER_ENTRY_POLICY_VERSION} pauses D0 live entry"
        prior = self.repository.prior_accepted_live_buy_in_event(str(market_row["id"]))
        if prior is not None:
            return (
                f"{WEATHER_ENTRY_POLICY_VERSION} freezes scale-in/re-entry for this event "
                f"(prior_intent={prior['id']}, prior_market={prior['bought_market_id']})"
            )
        return None

    def _record_minimum_order_blocker(self, market_row, analysis_row, reason: str) -> None:
        """Expose trade signals that cannot satisfy exchange minimums under the cap."""
        if "order below exchange minimum" not in reason:
            return
        market_id = str(market_row["id"])
        action = "entry_minimum_blocked"
        if self.repository.has_recent_autopilot_decision(
            market_id=market_id,
            action=action,
            since_minutes=60,
        ):
            return
        state = self.repository.get_autopilot_state()
        try:
            edge = Decimal(str(analysis_row["edge"])) if analysis_row["edge"] is not None else None
        except (KeyError, IndexError, TypeError):
            edge = None
        self.repository.save_autopilot_decision(
            market_id=market_id,
            action=action,
            mode=str(state["mode"] if state is not None else "live"),
            edge=edge,
            reason=reason,
            blockers=[reason],
            status="skipped",
        )

    @staticmethod
    def _buy_preflight_rejection_reason(
        market_row,
        analysis_row,
        *,
        max_notional: Decimal,
    ) -> str | None:
        try:
            market_payload = (
                json.loads(market_row["raw_payload"]) if market_row["raw_payload"] else {}
            )
        except Exception:
            market_payload = {}
        return preflight_buy_rejection_reason(
            analysis_from_row(analysis_row),
            max_notional,
            market_payload=market_payload if isinstance(market_payload, dict) else None,
        )

    def _refresh_live_order_book(
        self,
        *,
        market_id: str,
        market_row,
        analysis,
    ) -> tuple[Any, str | None]:
        """Fetch a fresh token book, persist it, and return the stored snapshot row."""
        side = str(getattr(analysis, "side", None) or "").lower()
        return self._refresh_token_order_book(
            market_id=market_id,
            market_row=market_row,
            side=side,
        )

    def _refresh_token_order_book(
        self,
        *,
        market_id: str,
        market_row,
        side: str,
    ) -> tuple[Any, str | None]:
        """Shared token-specific read used by live pre-submit and group quote refresh."""
        from polymarket_weather_arb.domain.markets import MarketSnapshot

        side = str(side or "").lower()
        token_id = market_row["no_token_id"] if side == "buy_no" else market_row["yes_token_id"]
        if not token_id:
            token_id = market_row["yes_token_id"] or market_row["no_token_id"]
        if not token_id:
            return None, "order book refresh failed: market has no token id"
        try:
            token_snapshot, raw_snapshot = self.client.get_token_order_book(token_id)
        except Exception as exc:
            return None, f"order book refresh failed: {exc}"
        # get_token_order_book may label market_id as "token_book"; bind to real market.
        snapshot = MarketSnapshot(
            market_id=market_id,
            best_bid=token_snapshot.best_bid,
            best_ask=token_snapshot.best_ask,
            midpoint=token_snapshot.midpoint,
            spread=token_snapshot.spread,
            liquidity=token_snapshot.liquidity,
            fetched_at=token_snapshot.fetched_at or datetime.now(timezone.utc),
            token_id=str(token_id),
        )
        self.repository.save_market_snapshot(snapshot, raw_snapshot, token_id=str(token_id))
        stored = self.repository.latest_market_snapshot(market_id, token_id=str(token_id))
        if stored is None:
            return None, "order book refresh failed: snapshot not persisted"
        return stored, None

    def _maybe_manage_stale_orders(self, *, app_mode: str) -> list[str]:
        """Cancel stale open orders via OrderLifecycleService; never erase prior audit.

        Cancel failures are persisted, surfaced on /app (last_error + decision), and
        sent as material Telegram. Success is never implied when failures exist.
        """
        if app_mode not in {"micro_live", "full_live"}:
            return []
        from polymarket_weather_arb.services.order_lifecycle_service import OrderLifecycleService

        notes: list[str] = []
        try:
            lifecycle = OrderLifecycleService(self.client, self.repository)
            cancel_result = lifecycle.cancel_stale_orders(
                stale_threshold_seconds=int(self.settings.stale_order_book_seconds)
            )
            if cancel_result.cancelled:
                notes.append(f"cancelled_stale_orders={len(cancel_result.cancelled)}")
            if cancel_result.failures:
                failure_text = "; ".join(
                    f"{item.get('exchange_order_id')}:{item.get('error')}"
                    for item in cancel_result.failures
                )
                notes.append(f"stale_order_cancel_failed={failure_text}")
                self._record_lifecycle_cancel_failure(
                    failure_text,
                    cancelled=len(cancel_result.cancelled),
                    failed=len(cancel_result.failures),
                )
            try:
                self.repository.connection.commit()
            except Exception as exc:
                logger.warning(
                    "stale-order cancel commit failed (audit rows preserved): %s",
                    exc,
                )
                notes.append(f"stale_order_cancel_commit_failed: {exc}")
                self._record_lifecycle_cancel_failure(
                    f"commit failed: {exc}",
                    cancelled=len(cancel_result.cancelled),
                    failed=len(cancel_result.failures) + 1,
                )
        except Exception as exc:
            # Record and surface; do not wipe audit. Do not pretend success.
            logger.warning("stale order lifecycle failed: %s", exc)
            notes.append(f"stale_order_lifecycle_failed: {exc}")
            self._record_lifecycle_cancel_failure(str(exc), cancelled=0, failed=1)
        return notes

    def _record_lifecycle_cancel_failure(
        self,
        detail: str,
        *,
        cancelled: int,
        failed: int,
    ) -> None:
        """Persist + material Telegram for stale-order cancel failures."""
        summary = f"stale order cancel failed cancelled={cancelled} failed={failed}: {detail}"
        try:
            self.repository.save_autopilot_decision(
                market_id=None,
                action="cancel_stale",
                mode=self.repository.get_autopilot_state()["mode"]
                if self.repository.get_autopilot_state()
                else "live",
                edge=None,
                reason=summary,
                blockers=[detail],
                status="failed",
            )
            self.repository.update_autopilot_state(last_error=summary)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to persist lifecycle cancel failure: %s", exc)
        self._notify(
            "app_execution_risk",
            {
                "status": "failed",
                "kind": "trade_event",
                "summary": f"过期挂单撤销失败 cancelled={cancelled} failed={failed}",
                "items": [detail],
            },
        )

    def _refresh_position_analyses(self) -> None:
        """Ensure nonzero positions have reasonably fresh analysis before exit decisions."""
        from polymarket_weather_arb.domain.market_eligibility import is_market_orderable
        import json

        provider_cooldown = open_meteo_cooldown_remaining()
        positions = self.repository.list_positions(limit=100, nonzero_only=True)
        cached_groups: dict[tuple[str, str], list[str]] = {}
        cached_deferred: list[str] = []
        cached_failures: list[str] = []
        cached_analyzed = 0
        for position in positions:
            try:
                market_id = str(position["market_id"])
            except (KeyError, IndexError, TypeError):
                continue
            if not market_id:
                continue
            try:
                row = self.repository.get_market(market_id)
                if row is None:
                    continue
                try:
                    payload = json.loads(row["raw_payload"]) if row["raw_payload"] else {}
                except Exception:
                    payload = {}
                if not is_market_orderable(
                    raw_payload=payload if isinstance(payload, dict) else {},
                    title=row["title"],
                    close_time=row["close_time"],
                    check_target_date=True,
                ):
                    self._handle_expired_position(market_id, market_row=row, payload=payload)
                    continue

                if provider_cooldown:
                    rule = self.repository.get_temperature_bucket_rule(market_id)
                    if rule is None or str(row["module_id"] or "") != "global_temp_bucket":
                        cached_deferred.append(f"unsupported_cached_refresh:{market_id}")
                        continue
                    city = str(rule["city"] or "")
                    target_date = str(rule["target_date"] or "")
                    if not city or not target_date:
                        cached_deferred.append(f"missing_event_identity:{market_id}")
                        continue
                    sibling_ids = [
                        str(item["market_id"])
                        for item in self.repository.list_temperature_bucket_rules(
                            limit=100,
                            city=city,
                            target_date=target_date,
                        )
                    ]
                    cached_groups[(city.casefold(), target_date)] = [
                        market_id,
                        *[item for item in sibling_ids if item != market_id],
                    ]
                    continue

                self._prepare_market(market_id)
            except Exception as exc:
                logger.warning("position analysis refresh failed for %s: %s", market_id, exc)

        if provider_cooldown:
            for market_ids in cached_groups.values():
                try:
                    analyzed, failures, deferred, _revision = (
                        self.workflow.reprice_global_bucket_group_cached(market_ids)
                    )
                    cached_analyzed += analyzed
                    cached_failures.extend(failures)
                    if deferred:
                        cached_deferred.append(deferred)
                except Exception as exc:  # noqa: BLE001 - summarize all cached groups
                    cached_failures.append(f"{market_ids[0]}: {exc}")
            log = logger.warning if cached_failures else logger.info
            log(
                "position analysis cache refresh during Open-Meteo cooldown: "
                "remaining=%ss groups=%s analyzed=%s deferred=%s failures=%s",
                provider_cooldown,
                len(cached_groups),
                cached_analyzed,
                len(cached_deferred),
                len(cached_failures),
            )

    def _backfill_resolved_model_signals(self) -> tuple[int, int]:
        """Settle pending calibration signals from official Gamma winners.

        Cached resolved payloads are consumed first without HTTP. Expired events
        are then refreshed by event slug so one Gamma read covers all sibling
        temperature buckets.
        """
        from polymarket_weather_arb.services.circuit_breaker_service import (
            CircuitBreakerService,
        )
        from polymarket_weather_arb.services.resolution_audit_service import (
            ResolutionAuditService,
        )

        service = ResolutionAuditService(
            self.repository,
            self.client,
            CircuitBreakerService(self.repository),
        )
        audited = 0
        settled = 0
        cached_ids = self.repository.get_markets_needing_resolution_audit(limit=100)
        for market_id in cached_ids:
            try:
                result = service.audit_cached_market(market_id)
                self.repository.connection.commit()
                audited += 1
                settled += result.updated_signals
            except Exception as exc:
                self.repository.connection.rollback()
                logger.warning("cached resolution audit failed market=%s: %s", market_id, exc)

        for event_slug in self.repository.get_event_slugs_needing_resolution_audit(limit=2):
            try:
                results = service.audit_event(event_slug)
                self.repository.connection.commit()
                audited += len(results)
                settled += sum(result.updated_signals for result in results)
            except Exception as exc:
                self.repository.connection.rollback()
                logger.warning("event resolution audit failed event=%s: %s", event_slug, exc)
        return audited, settled

    def _handle_expired_position(
        self,
        market_id: str,
        *,
        market_row=None,
        payload: dict | None = None,
    ) -> None:
        """Route expired/closed positions to settlement visibility without forecast HTTP.

        Read-only: uses local market payload + optional observations. Does **not**
        call ResolutionAuditService.audit_market (that path writes audits and can
        trip the global circuit breaker).
        """
        from decimal import Decimal

        from polymarket_weather_arb.domain.polymarket_resolution import parse_resolution_state
        from polymarket_weather_arb.domain.pricing import Analysis

        try:
            if market_row is None:
                market_row = self.repository.get_market(market_id)
            if payload is None and market_row is not None:
                try:
                    payload = (
                        json.loads(market_row["raw_payload"]) if market_row["raw_payload"] else {}
                    )
                except Exception:
                    payload = {}
            if not isinstance(payload, dict):
                payload = {}

            state = parse_resolution_state(payload)
            observation = self.repository.latest_observation(market_id)
            if state.resolved_outcome:
                positions = self.repository.list_positions(market_id=market_id, nonzero_only=True)
                held_outcomes = {str(position["outcome"]).strip().upper() for position in positions}
                winner = state.resolved_outcome.strip().upper()
                if winner in held_outcomes:
                    status = "settled_win_redeemable"
                else:
                    status = "settled_loss_zero_value"
            elif state.closed and state.uma_resolution_status == "resolved":
                status = "awaiting Polymarket resolution"
            elif state.closed:
                status = (
                    "awaiting observation"
                    if observation is None
                    else "awaiting Polymarket resolution"
                )
            else:
                status = "settlement_pending"

            reason = f"Position expired/closed; settlement state: {status}"
            # Idempotent: skip duplicate settlement marker if latest analysis already says so.
            latest = self.repository.latest_analysis(market_id)
            if latest is not None:
                try:
                    reasons = latest["reasons"]
                    if isinstance(reasons, str):
                        reasons_blob = reasons
                    else:
                        reasons_blob = json.dumps(reasons)
                    if "settlement state:" in reasons_blob and status in reasons_blob:
                        return
                except Exception:
                    pass

            self.repository.save_analysis(
                Analysis(
                    market_id=market_id,
                    model_version="settlement-route-v1",
                    fair_lower=Decimal("0"),
                    fair_upper=Decimal("0"),
                    reference_price=None,
                    edge=Decimal("0"),
                    side=None,
                    decision="skip",
                    reasons=[reason],
                )
            )
            state = self.repository.get_autopilot_state()
            self.repository.save_autopilot_decision(
                market_id=market_id,
                action="settlement_pending",
                mode=state["mode"] if state is not None else "live",
                edge=None,
                reason=reason,
                blockers=[],
                status="skipped",
            )
            logger.info("expired position routed market_id=%s status=%s", market_id, status)
        except Exception as exc:
            logger.warning("Expired position settlement check failed for %s: %s", market_id, exc)

    def _maybe_auto_redeem(self, *, app_mode: str) -> tuple[int, int]:
        """Redeem at most one reconciled winning condition in full-live.

        This reuses the single capital pulse and authenticated Polymarket
        adapter. It requires a fresh live Gamma resolution check and writes a
        durable decision before submitting. Any ambiguous outcome is never
        replayed automatically.
        """
        if app_mode != "full_live" or self.settings.trading_disabled:
            return 0, 0

        candidates = self._redeemable_position_candidates()
        if not candidates:
            return 0, 0

        for candidate in candidates:
            market_id = candidate["market_id"]
            prior = self.repository.latest_autopilot_decision_for_action(
                market_id=market_id,
                action="auto_redeem",
            )
            if prior is not None and str(prior["status"] or "") in {
                "prepared",
                "submitted",
                "submitted_unverified",
                "redeemed",
            }:
                logger.warning(
                    "auto redeem held for prior durable state market=%s status=%s decision_id=%s",
                    market_id,
                    prior["status"],
                    prior["id"],
                )
                continue

            readiness_fn = getattr(self.client, "validate_redemption_signing", None)
            if not callable(readiness_fn):
                self._record_auto_redeem_blocker(
                    market_id,
                    "client does not expose the official redemption readiness check",
                )
                continue
            try:
                readiness = readiness_fn()
            except Exception as exc:
                self._record_auto_redeem_blocker(
                    market_id,
                    "redemption readiness failed: " + _redacted_exception(exc),
                )
                continue
            if not isinstance(readiness, dict) or readiness.get("ok") is not True:
                detail = (
                    str(readiness.get("detail") or readiness.get("status") or "not ready")
                    if isinstance(readiness, dict)
                    else "malformed redemption readiness response"
                )
                self._record_auto_redeem_blocker(market_id, detail)
                continue

            verified = self._verify_redeemable_position_live(candidate)
            if verified is None:
                continue
            condition_id = verified["condition_id"]
            held_outcome = verified["outcome"]
            size = verified["size"]

            redeem_fn = getattr(self.client, "redeem_positions", None)
            if not callable(redeem_fn):
                self._record_auto_redeem_blocker(
                    market_id,
                    "client does not expose the official redemption mutation",
                )
                continue

            mode = _execution_mode_for_app_mode(app_mode)
            decision_id = self.repository.save_autopilot_decision(
                market_id=market_id,
                action="auto_redeem",
                mode=mode,
                edge=None,
                reason=(
                    "prepared official CTF redemption after fresh Polymarket resolution "
                    f"winner={held_outcome} reconciled_size={size}"
                ),
                blockers=[],
                status="prepared",
            )
            # Durable intent before mutation. A crash after this point blocks
            # replay and requires reconciliation/operator review.
            self.repository.connection.commit()

            submitted: dict[str, Any] = {}

            def on_submitted(payload: dict[str, Any]) -> None:
                submitted.update(payload)
                reason = (
                    "official CTF redemption submitted; "
                    f"transaction_id={payload.get('transaction_id') or '-'} "
                    f"transaction_hash={payload.get('transaction_hash') or '-'}"
                )
                self.repository.update_autopilot_decision_outcome(
                    decision_id,
                    status="submitted",
                    reason=reason,
                )
                self.repository.connection.commit()

            try:
                outcome = redeem_fn(
                    condition_id=condition_id,
                    on_submitted=on_submitted,
                )
                if not isinstance(outcome, dict) or outcome.get("status") != "confirmed":
                    raise RuntimeError("redemption returned without a confirmed outcome")
                transaction_hash = str(outcome.get("transaction_hash") or "").strip()
                transaction_id = str(outcome.get("transaction_id") or "").strip()
                if not transaction_hash:
                    raise RuntimeError("confirmed redemption omitted transaction hash")
                self.repository.update_autopilot_decision_outcome(
                    decision_id,
                    status="redeemed",
                    reason=(
                        "official CTF redemption confirmed; "
                        f"transaction_id={transaction_id or '-'} "
                        f"transaction_hash={transaction_hash}"
                    ),
                )
                self.repository.connection.commit()
                logger.info(
                    "auto redemption confirmed market=%s decision_id=%s transaction_id=%s "
                    "transaction_hash=%s",
                    market_id,
                    decision_id,
                    transaction_id or "-",
                    transaction_hash,
                )
                self._notify(
                    "app_redeem_confirmed",
                    {
                        "status": "executed",
                        "market_id": market_id,
                        "market_title": self._market_title(market_id),
                        "outcome": held_outcome,
                        "size": size,
                        "summary": (
                            "结算赢家自动赎回已链上确认 "
                            f"transaction_hash={transaction_hash}"
                        ),
                    },
                )
                return 1, 1
            except Exception as exc:
                detail = _redacted_exception(exc)
                submitted_hint = (
                    f" transaction_id={submitted.get('transaction_id') or '-'}"
                    f" transaction_hash={submitted.get('transaction_hash') or '-'}"
                )
                self.repository.update_autopilot_decision_outcome(
                    decision_id,
                    status="submitted_unverified",
                    reason=(
                        "redemption outcome is ambiguous; automatic replay blocked;"
                        f"{submitted_hint}; error={detail}"
                    ),
                    blockers=[detail],
                )
                self.repository.connection.commit()
                logger.error(
                    "auto redemption unverified market=%s decision_id=%s detail=%s",
                    market_id,
                    decision_id,
                    detail,
                )
                self._notify(
                    "app_redeem_unverified",
                    {
                        "status": "submitted_unverified",
                        "market_id": market_id,
                        "market_title": self._market_title(market_id),
                        "outcome": held_outcome,
                        "size": size,
                        "summary": "自动赎回结果未核实；已禁止自动重试",
                        "items": [detail],
                    },
                )
                return 0, 1
        return 0, 0

    def _redeemable_position_candidates(self) -> list[dict[str, str]]:
        from polymarket_weather_arb.domain.polymarket_resolution import (
            parse_resolution_state,
        )

        candidates: list[dict[str, str]] = []
        for position in self.repository.list_positions(limit=1000, nonzero_only=True):
            market_id = str(position["market_id"] or "").strip()
            outcome = str(position["outcome"] or "").strip().upper()
            size = str(position["size"] or "").strip()
            if not market_id or outcome not in {"YES", "NO"}:
                continue
            market = self.repository.get_market(market_id)
            if market is None:
                continue
            try:
                payload = json.loads(market["raw_payload"] or "{}")
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                continue
            state = parse_resolution_state(payload)
            if (
                state.resolved_outcome is None
                or state.resolved_outcome.upper() != outcome
            ):
                continue
            candidates.append(
                {
                    "market_id": market_id,
                    "outcome": outcome,
                    "size": size,
                }
            )
        candidates.sort(key=lambda item: item["market_id"])
        return candidates

    def _verify_redeemable_position_live(
        self,
        candidate: dict[str, str],
    ) -> dict[str, str] | None:
        """Refresh Gamma and require an unambiguous exchange winner."""
        from polymarket_weather_arb.domain.polymarket_resolution import (
            parse_resolution_state,
        )

        market_id = candidate["market_id"]
        fetch_market = getattr(self.client, "get_market", None)
        if not callable(fetch_market):
            self._record_auto_redeem_blocker(
                market_id,
                "fresh Polymarket market verification is unavailable",
            )
            return None
        try:
            fetched = fetch_market(market_id)
        except Exception as exc:
            self._record_auto_redeem_blocker(
                market_id,
                "fresh Polymarket resolution read failed: " + _redacted_exception(exc),
            )
            return None
        if not isinstance(fetched, tuple) or len(fetched) != 2:
            self._record_auto_redeem_blocker(
                market_id,
                "fresh Polymarket resolution response is missing",
            )
            return None
        market, payload = fetched
        if not isinstance(payload, dict):
            self._record_auto_redeem_blocker(
                market_id,
                "fresh Polymarket resolution payload is malformed",
            )
            return None
        state = parse_resolution_state(payload)
        winner = str(state.resolved_outcome or "").upper()
        if winner != candidate["outcome"]:
            self._record_auto_redeem_blocker(
                market_id,
                (
                    "fresh Polymarket resolution does not prove the held outcome wins "
                    f"(held={candidate['outcome']} winner={winner or 'unresolved'})"
                ),
            )
            return None
        condition_id = str(
            payload.get("conditionId") or payload.get("condition_id") or ""
        ).strip()
        if not condition_id:
            self._record_auto_redeem_blocker(
                market_id,
                "fresh resolved payload has no conditionId",
            )
            return None

        # Persist the same official payload used for the mutation decision.
        self.repository.upsert_market(market, payload)
        self.repository.connection.commit()
        return {
            **candidate,
            "condition_id": condition_id,
        }

    def _record_auto_redeem_blocker(self, market_id: str, detail: str) -> None:
        detail = redact_text(str(detail))[:500]
        if self.repository.has_recent_autopilot_decision(
            market_id=market_id,
            action="auto_redeem",
            since_minutes=60,
        ):
            return
        state = self.repository.get_autopilot_state()
        self.repository.save_autopilot_decision(
            market_id=market_id,
            action="auto_redeem",
            mode=str(state["mode"] if state is not None else "live"),
            edge=None,
            reason=f"automatic redemption blocked: {detail}",
            blockers=[detail],
            status="blocked",
        )
        self.repository.connection.commit()
        logger.warning("automatic redemption blocked market=%s detail=%s", market_id, detail)

    def _maybe_auto_exit(self, *, app_mode: str) -> tuple[int, int]:
        """Run guarded auto-exit when app is micro_live/full_live and env allows it."""
        if app_mode not in {"micro_live", "full_live"}:
            return 0, 0
        if not _auto_exit_enabled_for_app_mode(self.settings, app_mode):
            return 0, 0
        from polymarket_weather_arb.services.auto_exit_service import AutoExitService

        result = AutoExitService(self.repository, self.client).run_tick(
            settings=self.settings,
            profile_name=profile_name_for_app_mode(app_mode),
            # App live mode is the operator-side arming gate (like daemon --allow-auto-exit).
            allow_auto_exit=True,
            on_submitted=lambda _intent_id: self.repository.connection.commit(),
        )
        if app_mode == "full_live" and not result.enabled_gates_ok:
            blockers = result.notes or ["unknown auto-exit gate failure"]
            logger.error("full-live auto-exit gates closed: %s", "; ".join(blockers))
            self._notify(
                "app_execution_risk",
                {
                    "status": "failed",
                    "kind": "trade_event",
                    "summary": "正式实盘自动退出闸门未开启",
                    "items": blockers[:5],
                },
            )
        skipped = list(getattr(result, "skipped", []))
        if skipped:
            logger.info(
                "auto-exit skipped count=%s first=%s",
                len(skipped),
                skipped[0],
            )
        for submission in result.submissions:
            self._notify_sell_submission(submission)
        failures = list(getattr(result, "failures", []))
        if failures:
            self._notify(
                "app_execution_risk",
                {
                    "status": "failed",
                    "kind": "trade_event",
                    "summary": (
                        f"自动卖出失败 {len(failures)} 笔；"
                        f"尝试={result.attempted} 成功={result.executed}"
                    ),
                    "items": failures[:5],
                },
            )
        return result.executed, result.attempted

    def _notify(
        self,
        event: str,
        payload: dict[str, object],
    ) -> None:
        """Queue a material event. Never raises into the trading path."""
        if self.notifier is None:
            return
        try:
            self.notifier(
                {
                    **payload,
                    "daemon_event": event,
                    "kind": payload.get("kind") or "trade_event",
                    "project": "polymarket-weather-arb",
                }
            )
        except Exception as exc:  # noqa: BLE001 - notifications must not break ticks
            logger.warning("app telegram notify queue failed: %s", exc)

    def _flush_notifier(self) -> None:
        if self.notifier is None:
            return
        try:
            flush = getattr(self.notifier, "flush", None)
            if callable(flush):
                flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning("app telegram notify flush failed: %s", exc)

    def _market_title(self, market_id: str | None) -> str | None:
        if not market_id:
            return None
        row = self.repository.get_market(market_id)
        if row is None:
            return None
        title = row["title"] if "title" in row.keys() else None
        return str(title) if title else None

    def _log_phase(
        self,
        tick_id: str,
        phase: str,
        started_at: float,
        *,
        items: int | None = None,
        requests: int | None = None,
        detail: str | None = None,
        failure: str | None = None,
        deferred: int | None = None,
    ) -> None:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        logger.info(
            "tick phase=%s finish tick_id=%s elapsed_ms=%s items=%s requests=%s deferred=%s failure=%s detail=%s",
            phase,
            tick_id,
            elapsed_ms,
            items if items is not None else "-",
            requests if requests is not None else "-",
            deferred if deferred is not None else "-",
            failure or "none",
            detail or "",
        )

    def _load_recon_alert_signature(self) -> str | None:
        """Read prior recon-failure identity from existing autopilot_state.last_error."""
        state = self.repository.get_autopilot_state()
        if state is None:
            return None
        try:
            last_error = state["last_error"]
        except (KeyError, IndexError, TypeError):
            last_error = None
        if last_error is None:
            return None
        text = str(last_error)
        if text.startswith(RECON_ALERT_PREFIX):
            return text
        return None

    def _notify_reconciliation_failure(
        self,
        *,
        signature: str,
        prior_signature: str | None,
        recon_status: str,
        failed_stage: object,
        reason: str,
    ) -> None:
        stage = str(failed_stage or "unknown")
        # Identical repeated failure across /app service instances stays quiet.
        if prior_signature == signature:
            logger.warning(
                "reconciliation still failed stage=%s status=%s (telegram suppressed)",
                stage,
                recon_status,
            )
            return
        self._notify(
            "app_execution_risk",
            {
                "status": "failed",
                "kind": "trade_event",
                "summary": (
                    f"对账失败 fail-stop status={recon_status} stage={stage}；"
                    "本轮禁止撤单/卖出/买入"
                ),
                "items": [reason, f"failed_stage={stage}"],
            },
        )

    def _notify_reconciliation_recovery_if_needed(self) -> None:
        prior = self._load_recon_alert_signature()
        if prior is None:
            return
        # Make the recovery transition durable before Telegram flushes. If this
        # commit fails, the outer tick remains fail-stop and retries recovery on
        # the next healthy cycle instead of sending an undurable notification.
        self.repository.update_autopilot_state(clear_last_error=True)
        self._commit_recon_alert_state("recovery")
        self._notify(
            "app_execution_risk",
            {
                "status": "recovered",
                "kind": "trade_event",
                "summary": "对账已恢复，本轮可继续撤单/卖出/买入",
                "items": [f"prior_failure={prior}"],
            },
        )

    def _commit_recon_alert_state(self, transition: str) -> None:
        """Durably store alert state before a failure/recovery notification."""
        try:
            self.repository.connection.commit()
        except Exception as exc:
            try:
                self.repository.connection.rollback()
            except Exception as rollback_exc:  # noqa: BLE001
                logger.warning(
                    "reconciliation alert %s rollback failed: %s",
                    transition,
                    rollback_exc,
                )
            raise RuntimeError(f"reconciliation alert {transition} state commit failed") from exc

    def _commit_reconciliation_then_notify_fills(self, recon: dict[str, Any]) -> None:
        """Commit reconciliation rows, then queue fill notifications only on success.

        BUY/SELL submitted notifications keep their own ``on_submitted`` commit
        hooks; this path only gates fill alerts on durable fill inserts.
        """
        new_fills = recon.get("new_fills") or []
        if not isinstance(new_fills, list):
            new_fills = []
        market_ids = {
            str(fill.get("market_id"))
            for fill in new_fills
            if isinstance(fill, dict) and fill.get("market_id")
        }
        try:
            market_ids.update(
                self.repository.list_roundtrip_markets_needing_status_refresh(limit=100)
            )
        except Exception as exc:
            # Roundtrip status is a derived audit view. It must never prevent
            # reconciliation rows and exchange fills from becoming durable.
            logger.warning(
                "roundtrip recovery scan failed after reconciliation: %s",
                exc,
            )
        if market_ids:
            from polymarket_weather_arb.services.roundtrip_status_service import (
                RoundtripStatusService,
            )

            roundtrip_status = RoundtripStatusService(self.repository)
            for market_id in sorted(market_ids):
                try:
                    roundtrip_status.get_status(market_id)
                except Exception as exc:
                    # Reconciliation and fill durability outrank the derived
                    # roundtrip view. A later reconciliation can retry it.
                    logger.warning(
                        "roundtrip status refresh failed after reconciliation for %s: %s",
                        market_id,
                        exc,
                    )
        try:
            self.repository.connection.commit()
        except Exception as exc:
            logger.warning(
                "reconciliation commit failed; skipping fill telegram notify: %s",
                exc,
            )
            try:
                self.repository.connection.rollback()
            except Exception as rollback_exc:
                logger.warning("reconciliation rollback failed: %s", rollback_exc)
            raise RuntimeError(f"reconciliation commit failed: {exc}") from exc
        if new_fills:
            self._notify_new_fills({"new_fills": new_fills})
        if str(recon.get("status") or "") == "ok":
            self._maybe_notify_portfolio_digest()

    def _maybe_notify_portfolio_digest(self, *, now: datetime | None = None) -> None:
        if self.notifier is None:
            return
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        state = self.repository.get_autopilot_state()
        last_sent_raw = None
        if state is not None and "last_portfolio_digest_at" in state.keys():
            last_sent_raw = state["last_portfolio_digest_at"]
        if last_sent_raw:
            try:
                last_sent = datetime.fromisoformat(str(last_sent_raw))
                if last_sent.tzinfo is None:
                    last_sent = last_sent.replace(tzinfo=timezone.utc)
                if (current - last_sent.astimezone(timezone.utc)).total_seconds() < (
                    PORTFOLIO_DIGEST_INTERVAL_SECONDS
                ):
                    return
            except ValueError:
                logger.warning("invalid last_portfolio_digest_at=%s; rebuilding", last_sent_raw)

        try:
            from polymarket_weather_arb.services.cockpit_service import (
                build_portfolio_digest,
            )

            digest = build_portfolio_digest(self.repository, now=current)
        except Exception as exc:  # noqa: BLE001 - reporting must not break trading
            logger.warning("portfolio digest build failed: %s", exc)
            return
        if digest.open_position_count <= 0:
            return
        if not digest.reconciliation_fresh:
            logger.warning("portfolio digest skipped: reconciliation is not fresh")
            return

        payload_positions = [
            {
                "market_id": item.market_id,
                "market_title": item.market_title,
                "city": item.city,
                "bucket": item.bucket,
                "outcome": item.outcome,
                "position_size": item.position_size,
                "buy_cost": item.buy_cost,
                "sell_proceeds": item.sell_proceeds,
                "current_value": item.current_value,
                "estimated_pnl": item.estimated_pnl,
                "estimated_return_pct": item.estimated_return_pct,
                "target_date": item.target_date,
                "timezone_name": item.timezone_name,
                "local_day_offset": item.local_day_offset,
                "seconds_to_target_end": item.seconds_to_target_end,
            }
            for item in digest.positions
        ]
        try:
            self.repository.update_autopilot_state(
                last_portfolio_digest_at=current.astimezone(timezone.utc).isoformat()
            )
            self.repository.connection.commit()
        except Exception as exc:  # noqa: BLE001 - reporting must not break trading
            try:
                self.repository.connection.rollback()
            except Exception as rollback_exc:  # noqa: BLE001
                logger.warning("portfolio digest rollback failed: %s", rollback_exc)
            logger.warning(
                "portfolio digest timestamp commit failed; notification skipped: %s", exc
            )
            return
        self._notify(
            "app_portfolio_digest",
            {
                "status": "ok",
                "kind": "portfolio_digest",
                "reconciliation_fresh": digest.reconciliation_fresh,
                "reconciliation_age_minutes": digest.reconciliation_age_minutes,
                "open_position_count": digest.open_position_count,
                "open_order_count": digest.open_order_count,
                "unverified_open_positions": digest.unverified_open_positions,
                "total_buy_cost": digest.total_buy_cost,
                "total_sell_proceeds": digest.total_sell_proceeds,
                "total_current_value": digest.total_current_value,
                "total_estimated_pnl": digest.total_estimated_pnl,
                "total_estimated_return_pct": digest.total_estimated_return_pct,
                "total_realized_pnl": digest.total_realized_pnl,
                "positions": payload_positions,
            },
        )

    def _notify_new_fills(self, recon: dict[str, Any]) -> None:
        new_fills = recon.get("new_fills") or []
        if not isinstance(new_fills, list):
            return
        for fill in new_fills:
            if not isinstance(fill, dict):
                continue
            exchange_fill_id = fill.get("exchange_fill_id")
            market_id = fill.get("market_id")
            self._notify(
                "app_fill",
                {
                    "status": "filled",
                    "summary": (
                        f"交易所成交确认 成交ID={exchange_fill_id} "
                        f"方向={fill.get('side')} 价格={fill.get('price')} 数量={fill.get('size')}"
                    ),
                    "market_id": market_id,
                    "market": market_id,
                    "market_title": self._market_title(str(market_id) if market_id else None),
                    "side": fill.get("side"),
                    "price": fill.get("price"),
                    "size": fill.get("size"),
                    "order_id": fill.get("order_id"),
                    "exchange_fill_id": exchange_fill_id,
                },
            )

    def _notify_buy_submitted(
        self,
        intent_id: int,
        market_id: str,
        analysis_row: Any,
    ) -> None:
        intent = self.repository.get_order_intent(intent_id)
        side = None
        price = None
        size = None
        if intent is not None:
            side = intent["side"] if "side" in intent.keys() else None
            price = intent["limit_price"] if "limit_price" in intent.keys() else None
            size = intent["size"] if "size" in intent.keys() else None
        if side is None and analysis_row is not None:
            side = analysis_row["side"] if "side" in analysis_row.keys() else None
        self._notify(
            "app_buy_submitted",
            {
                "status": "submitted",
                "summary": (
                    f"实盘买入限价单已提交 意图={intent_id} 方向={side} 价格={price} 数量={size}"
                ),
                "market_id": market_id,
                "market": market_id,
                "market_title": self._market_title(market_id),
                "side": side or "BUY",
                "price": price,
                "size": size,
                "intent_id": intent_id,
            },
        )

    def _notify_sell_submission(self, submission: dict[str, Any]) -> None:
        status = str(submission.get("status") or "submitted")
        market_id = submission.get("market_id")
        base: dict[str, object] = {
            "market_id": market_id,
            "market": market_id,
            "market_title": self._market_title(str(market_id) if market_id else None),
            "side": submission.get("side") or "SELL",
            "outcome": submission.get("outcome"),
            "price": submission.get("price"),
            "size": submission.get("size"),
            "order_id": submission.get("order_id"),
            "intent_id": submission.get("intent_id"),
            "policy_stage": submission.get("policy_stage"),
            "policy_version": submission.get("policy_version"),
        }
        if status in _MATERIAL_UNVERIFIED_STATUSES:
            # Exchange accepted the order, but local verification failed — material risk.
            status_zh = {
                "submitted_unverified": "已提交未核实",
                "reconcile_failed": "对账失败",
            }.get(status, status)
            self._notify(
                "app_order_unverified",
                {
                    **base,
                    "status": status,
                    "summary": (
                        f"自动卖出{status_zh} 订单={submission.get('order_id')} "
                        f"意图={submission.get('intent_id')}（请勿重复提交，请核对交易所）"
                    ),
                    "items": [str(submission.get("warning") or status_zh)],
                },
            )
            return
        # Exchange-accepted limit SELL: always label "submitted", never profit/matched.
        guardian_action = str(submission.get("guardian_action") or "")
        policy_stage = str(submission.get("policy_stage") or "")
        if policy_stage in {"official_observation", "near_settlement"}:
            kind_zh = "官方证据全量退出卖出"
            kind_en = "official-evidence full-exit SELL"
        elif guardian_action in {"exit_full", "position_at_risk"}:
            kind_zh = "证据确认全量退出卖出"
            kind_en = "evidence-confirmed full-exit SELL"
        else:
            kind_zh = "自动卖出"
            kind_en = "auto SELL"
        self._notify(
            "app_sell_submitted",
            {
                **base,
                "status": "submitted",
                "guardian_action": guardian_action or None,
                "summary": (
                    f"{kind_zh}限价单已提交 订单={submission.get('order_id')} "
                    f"价格={submission.get('price')} 数量={submission.get('size')} "
                    f"({kind_en})"
                ),
            },
        )

    def _evaluate_llm(self, market_id: str, analysis_row) -> LlmTradeDecision | None:
        if not self.llm_advisor.enabled:
            return None
        return self.llm_advisor.evaluate(market_id, analysis_row)

    def _record_tick(
        self,
        result: AutopilotTickResult,
        *,
        discovered: int,
        error: str | None = None,
        llm_decision: LlmTradeDecision | None = None,
        increment_tick_count: bool = True,
    ) -> None:
        self.repository.save_autopilot_decision(
            market_id=result.market_id,
            action=result.action,
            mode=self.repository.get_autopilot_state()["mode"]
            if self.repository.get_autopilot_state()
            else "dry_run",
            edge=result.edge,
            reason=result.reason,
            blockers=result.blockers,
            status=result.status,
            intent_id=result.intent_id,
            discovered=discovered,
            llm_provider=llm_decision.provider if llm_decision else None,
            llm_model=llm_decision.model if llm_decision else None,
            llm_confidence=llm_decision.confidence if llm_decision else None,
            llm_reason=(f"{llm_decision.action}: {llm_decision.reason}" if llm_decision else None),
        )
        now = _now_iso()
        self.repository.update_autopilot_state(
            last_tick_at=now,
            last_tick_status=result.status,
            last_error=error,
            clear_last_error=error is None,
            increment_tick_count=increment_tick_count,
            latest_useful_tick_at=now if getattr(result, "is_useful", False) else None,
            last_tick_duration_ms=getattr(result, "duration_ms", 0),
            deferred_candidates_count=getattr(result, "deferred_count", 0),
        )
        logger.info(
            f"Autopilot tick finished: status={result.status} action={result.action} "
            f"market={result.market_id or 'none'} duration_ms={getattr(result, 'duration_ms', 0)} "
            f"deferred={getattr(result, 'deferred_count', 0)} reason='{result.reason}'"
        )

    def _record_health_pulse(self, result: AutopilotTickResult) -> None:
        """Update process liveness without manufacturing a decision/chart point."""
        state = self.repository.get_autopilot_state()
        recovering_from_blocker = bool(
            state is not None
            and str(state["last_tick_status"] or "") == "blocked"
            and str(state["last_error"] or "").startswith("pulse_blocker|")
            and result.status != "blocked"
        )
        initialize_status = state is None or not str(state["last_tick_status"] or "")
        self.repository.update_autopilot_state(
            last_tick_at=_now_iso(),
            last_tick_status="idle" if recovering_from_blocker or initialize_status else None,
            clear_last_error=recovering_from_blocker,
            increment_tick_count=False,
        )
        logger.debug(
            "Autopilot health pulse: duration_ms=%s deferred=%s reason=%s",
            getattr(result, "duration_ms", 0),
            getattr(result, "deferred_count", 0),
            result.reason,
        )

    def _decision_rows(self) -> list[dict[str, object]]:
        return [
            {
                "id": row["id"],
                "market_id": row["market_id"],
                "action": row["action"],
                "mode": row["mode"],
                "edge": row["edge"],
                "reason": row["reason"],
                "status": row["status"],
                "created_at": row["created_at"],
                "llm_provider": row["llm_provider"] if "llm_provider" in row.keys() else None,
                "llm_model": row["llm_model"] if "llm_model" in row.keys() else None,
                "llm_confidence": row["llm_confidence"] if "llm_confidence" in row.keys() else None,
                "llm_reason": row["llm_reason"] if "llm_reason" in row.keys() else None,
            }
            for row in self.repository.list_autopilot_decisions(limit=10)
        ]


def _market_supported_by_weather_provider(
    repository: Repository,
    market_id: str,
    provider: str,
) -> bool:
    if provider != "noaa":
        return True
    row = repository.get_market(market_id)
    if row is None:
        return False
    rule = enrich_rule_from_market_title(
        parse_resolution_rule(row["title"], row["description"]),
        row["title"],
    )
    noaa = NoaaProvider()
    locations: list[str] = []
    if rule.location:
        locations.append(rule.location)
    title = row["title"] or ""
    match = re.search(r"temperature in (.+?) be ", title, flags=re.IGNORECASE)
    if match:
        locations.append(match.group(1).strip())
    seen: set[str] = set()
    for location in locations:
        key = location.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            noaa._resolve_gridpoint(location, rule.station or rule.source)
            return True
        except ValueError:
            continue
    return False


def _weather_provider_factory(settings: Settings):
    if settings.weather_provider == "noaa":
        return NoaaProvider
    return OpenMeteoProvider


def _staged_entry_cap(
    title: str,
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> Decimal:
    """Cumulative BUY budget before evidence-based risk reductions."""
    horizon = _staged_entry_horizon(title, now=now, timezone_name=timezone_name)
    if horizon == "D2":
        return Decimal("4.00")
    if horizon in {"D0", "D1"}:
        return Decimal("10.00")
    return Decimal("0")


def _staged_entry_horizon(
    title: str,
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> str:
    from zoneinfo import ZoneInfo

    from polymarket_weather_arb.domain.market_eligibility import resolve_market_timezone

    timezone_name = timezone_name or resolve_market_timezone(title=title)
    if timezone_name is None:
        return "unknown"
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_now = current.astimezone(ZoneInfo(timezone_name))
    event_day = event_date_from_market_title(title, today=local_now.date())
    if event_day is None:
        return "unknown"
    days_to_event = (event_day - local_now.date()).days
    return f"D{days_to_event}" if days_to_event in {0, 1, 2} else "other"


def _analysis_reason_strings(analysis_row: Any) -> list[str]:
    try:
        raw = analysis_row["reasons"]
    except (KeyError, IndexError, TypeError):
        raw = None
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        parsed = json.loads(raw) if raw else []
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _analysis_uses_stale_weather(analysis_row: Any) -> bool:
    return any(
        "weather_cache_status=stale_if_error" in reason
        for reason in _analysis_reason_strings(analysis_row)
    )


def _global_entry_model_version_error(analysis_row: Any) -> str | None:
    try:
        model_version = str(analysis_row["model_version"] or "")
    except (KeyError, IndexError, TypeError):
        model_version = ""
    if model_version == GLOBAL_BUCKET_MODEL_VERSION:
        return None
    return (
        "global bucket live entry requires current model version "
        f"{GLOBAL_BUCKET_MODEL_VERSION}; got {model_version or 'missing'}"
    )


def _risk_adjusted_entry_cap(
    base_cap: Decimal,
    analysis_row: Any,
    *,
    history_multiplier: Decimal = Decimal("1"),
    max_order_usdc: Decimal | None = None,
    max_daily_usdc: Decimal | None = None,
) -> Decimal:
    """Size one entry from conservative v8 probability and remaining event headroom."""
    cap = max(Decimal("0"), Decimal(str(base_cap)))
    if max_order_usdc is not None:
        cap = min(cap, max(Decimal("0"), Decimal(str(max_order_usdc))))
    try:
        edge = Decimal(str(analysis_row["edge"] or 0))
    except (KeyError, IndexError, TypeError):
        edge = Decimal("0")
    if edge < Decimal("0.08"):
        cap = min(cap, Decimal("1.00"))
    elif edge < Decimal("0.15"):
        cap = min(cap, Decimal("2.00"))

    reasons = _analysis_reason_strings(analysis_row)
    support_match = next(
        (
            re.search(r"supporting_families=(\d+)/(\d+)", reason)
            for reason in reasons
            if "supporting_families=" in reason
        ),
        None,
    )
    if support_match is None:
        support_match = next(
            (
                re.search(r"supporting_models=(\d+)/(\d+)", reason)
                for reason in reasons
                if "supporting_models=" in reason
            ),
            None,
        )
    if support_match is not None:
        supporters, model_count = (int(value) for value in support_match.groups())
        if model_count > 0 and supporters * 3 == model_count * 2:
            cap = min(cap, Decimal("2.00"))
        elif 0 < supporters < model_count:
            cap = min(cap, Decimal("3.00"))

    dispersion = None
    for reason in reasons:
        match = re.search(r"entry_robust_dispersion=([0-9.]+)", reason)
        if match:
            dispersion = Decimal(match.group(1))
            break
    if dispersion is not None and dispersion > Decimal("0.20"):
        cap = min(cap, Decimal("1.00"))
    elif dispersion is not None and dispersion > Decimal("0.15"):
        cap = min(cap, Decimal("2.00"))
    haircut = None
    for reason in reasons:
        match = re.search(r"model_risk_haircut=([0-9.]+)", reason)
        if match:
            haircut = Decimal(match.group(1))
            break
    if haircut is not None and haircut > Decimal("0.20"):
        cap = min(cap, Decimal("1.00"))
    elif haircut is not None and haircut > Decimal("0.12"):
        cap = min(cap, Decimal("2.00"))
    if _analysis_uses_stale_weather(analysis_row):
        cap = min(cap, Decimal("0.50"))
    try:
        reference_price = Decimal(str(analysis_row["reference_price"] or 0))
    except (KeyError, IndexError, TypeError):
        reference_price = Decimal("0")
    if Decimal("0") < reference_price < Decimal("0.10"):
        cap = min(cap, Decimal("1.00"))
    decision_probability = None
    for reason in reasons:
        match = re.search(r"decision_probability_conservative=([0-9.]+)", reason)
        if match:
            decision_probability = Decimal(match.group(1))
            break
    if (
        decision_probability is not None
        and Decimal("0") < reference_price < Decimal("1")
        and max_daily_usdc is not None
    ):
        full_kelly = max(
            Decimal("0"),
            (decision_probability - reference_price) / (Decimal("1") - reference_price),
        )
        fractional_kelly_cap = Decimal(str(max_daily_usdc)) * Decimal("0.20") * full_kelly
        cap = min(cap, fractional_kelly_cap)
    bounded_history = min(Decimal("1"), max(Decimal("0.25"), history_multiplier))
    return cap * bounded_history


def _state_app_mode(state) -> str:
    if state is None:
        return DEFAULT_APP_MODE
    try:
        value = state["app_mode"]
    except (KeyError, IndexError):
        return _app_mode_for_mode(state["mode"])
    return value if value in APP_MODES else DEFAULT_APP_MODE


def _app_mode_for_mode(mode: str) -> str:
    return "micro_live" if mode == "live" else DEFAULT_APP_MODE


def _execution_mode_for_app_mode(app_mode: str) -> str:
    return "live" if app_mode in {"micro_live", "full_live"} else "dry_run"


def build_recon_alert_signature(recon: dict[str, Any]) -> str:
    """Stable recon-failure identity for cross-cycle Telegram dedupe/recovery.

    Includes status, stage, redacted error type, and a stable redacted message.
    Stored in existing ``autopilot_state.last_error`` (no new table/field).
    """
    status = str(recon.get("status") or "unknown")
    stage = str(recon.get("failed_stage") or "unknown")
    error_type = str(recon.get("error_type") or "Error")
    message = _stable_recon_error_message(str(recon.get("error") or ""))
    return f"{RECON_ALERT_PREFIX}{status}|{stage}|{error_type}|{message}"


def _stable_recon_error_message(error: str) -> str:
    text = " ".join(error.split())
    lowered = text.lower()
    for token in ("private_key", "api_key", "secret", "password", "bearer "):
        if token in lowered:
            return "[redacted]"
    # Drop volatile numbers that would break identical-failure dedupe (timestamps).
    text = re.sub(r"\b\d{10,}\b", "N", text)
    if len(text) > 160:
        text = text[:160]
    return text or "error"


def _redacted_exception(exc: BaseException) -> str:
    """Bounded error text safe for durable decisions and notifications."""
    return redact_text(f"{type(exc).__name__}: {exc}")[:500]


def profile_name_for_app_mode(app_mode: str) -> str:
    """Map /app mode to the matching builtin StrategyProfile name."""
    if app_mode == "full_live":
        return "full-live"
    return "micro-live"


def _full_live_readiness_ok(
    *,
    trading_disabled: bool,
    live_ready: bool,
    compliance_ok: bool,
    reconciliation_fresh: bool,
    breaker_ok: bool,
) -> bool:
    return (
        (not trading_disabled)
        and live_ready
        and compliance_ok
        and reconciliation_fresh
        and breaker_ok
    )


def _auto_exit_enabled_for_app_mode(settings: Settings, app_mode: str) -> bool:
    """Full live always includes exits; the env switch only arms micro live."""
    return app_mode == "full_live" or bool(settings.auto_exit_enabled)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_timestamp_is_fresh(value: object, *, max_age: timedelta) -> bool:
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - timestamp <= max_age


def _weather_analysis_max_age(
    *, city: str, target_date: str, timezone_name: str | None = None
) -> timedelta:
    from polymarket_weather_arb.domain.market_eligibility import try_local_weather_day

    local_day = try_local_weather_day(
        location_hint=city,
        timezone_name=timezone_name,
    )
    if local_day is not None and target_date[:10] == local_day.isoformat():
        return timedelta(minutes=5)
    return timedelta(minutes=30)


def _is_recent_stream_snapshot(row: Any) -> bool:
    """Allow a just-persisted stream BBO to survive subscription rotation."""
    try:
        payload = json.loads(row["raw_payload"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("source") != "polymarket_stream":
        return False
    from polymarket_weather_arb.adapters.polymarket.stream import (
        MARKET_TOKEN_STALE_SECONDS,
    )

    return _row_timestamp_is_fresh(
        row["fetched_at"],
        max_age=timedelta(seconds=MARKET_TOKEN_STALE_SECONDS),
    )


def _append_reason(reason: str, note: str | None) -> str:
    return f"{reason}; {note}" if note else reason


def _action_from_analysis_side(side: object) -> str:
    normalized = str(side or "").strip().lower()
    if normalized in {"yes", "buy_yes"}:
        return "buy_yes"
    if normalized in {"no", "buy_no"}:
        return "buy_no"
    return "skip"


def _fresh_reconciliation(row) -> bool:
    if row is None:
        return False
    created_at = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - created_at
    return age.total_seconds() <= 3600


def _reconciliation_health(row) -> tuple[str, bool, str]:
    if row is None:
        return "missing", False, "no reconciliation has completed"
    status = str(row["status"] or "unknown").strip().lower()
    if status != "ok":
        detail = f"latest reconciliation status={status}"
        try:
            payload = json.loads(row["details"]) if row["details"] else {}
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            stage = str(payload.get("failed_stage") or "").strip()
            error_type = str(payload.get("error_type") or "").strip()
            suffix = "/".join(value for value in (stage, error_type) if value)
            if suffix:
                detail = f"{detail} ({suffix})"
        return status, False, detail
    if not _fresh_reconciliation(row):
        return "stale", False, "latest successful reconciliation is stale"
    return "fresh", True, "latest reconciliation succeeded and is fresh"


def _forecast_source_grade(forecast_row) -> str:
    from polymarket_weather_arb.domain.source_grade import (
        UNKNOWN,
        extract_forecast_source_grade,
    )

    if forecast_row is None:
        return UNKNOWN
    try:
        raw_payload = forecast_row["raw_payload"]
        if not raw_payload:
            return UNKNOWN
        raw = json.loads(raw_payload)
    except (json.JSONDecodeError, KeyError, TypeError):
        return UNKNOWN
    return extract_forecast_source_grade(raw)
