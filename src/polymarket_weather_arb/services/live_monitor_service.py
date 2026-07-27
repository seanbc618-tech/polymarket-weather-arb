from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from sqlite3 import Row

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.profiles import (
    StrategyProfile,
    live_auto_enabled_by_override,
    settings_for_profile,
)
from polymarket_weather_arb.storage.repositories import Repository


@dataclass(frozen=True)
class LiveGate:
    name: str
    ok: bool
    reason: str
    detail: str | None = None


@dataclass(frozen=True)
class LiveActionReadiness:
    action_id: str
    market_id: str | None
    status: str
    gates: list[LiveGate]
    can_auto_execute: bool


@dataclass(frozen=True)
class LiveMonitorSnapshot:
    profile: str
    allow_live_auto: bool
    risk_status: str
    reconciliation_fresh: bool
    open_orders_count: int
    positions_count: int
    nonzero_positions_count: int
    pending_live_actions: list[LiveActionReadiness]
    blockers: list[str]


def build_live_monitor_snapshot(
    repository: Repository,
    *,
    profile: StrategyProfile,
    allow_live_auto: bool,
    live_market_ids: set[str],
    require_fresh_reconciliation: bool = True,
    block_live_on_positions: bool = True,
    dry_run_only: bool = False,
    settings: Settings | None = None,
) -> LiveMonitorSnapshot:
    effective_settings = settings_for_profile(settings or Settings(), profile)
    reconciliation_fresh = is_fresh_reconciliation(repository.latest_successful_reconciliation())
    risk_anomalies = live_monitor_risk_anomalies(
        repository,
        settings=effective_settings,
        reconciliation_fresh=reconciliation_fresh,
        require_fresh_reconciliation=require_fresh_reconciliation,
        block_live_on_positions=block_live_on_positions,
    )
    risk_status = "warn" if risk_anomalies else "ok"
    pending_actions = repository.list_automation_actions(
        limit=20, status="pending", kind="trade_live"
    )
    action_readiness = [
        live_action_readiness(
            repository,
            action,
            profile=profile,
            allow_live_auto=allow_live_auto,
            live_market_ids=live_market_ids,
            require_fresh_reconciliation=require_fresh_reconciliation,
            reconciliation_fresh=reconciliation_fresh,
            risk_status=risk_status,
            dry_run_only=dry_run_only,
        )
        for action in pending_actions
    ]
    blockers = _unique_blockers(
        [
            *risk_anomalies,
            *[gate.reason for action in action_readiness for gate in action.gates if not gate.ok],
        ]
    )
    return LiveMonitorSnapshot(
        profile=profile.name,
        allow_live_auto=allow_live_auto,
        risk_status=risk_status,
        reconciliation_fresh=reconciliation_fresh,
        open_orders_count=len(repository.list_open_orders(limit=100)),
        positions_count=len(repository.list_positions(limit=100)),
        nonzero_positions_count=repository.nonzero_positions_count(),
        pending_live_actions=action_readiness,
        blockers=blockers,
    )


def live_action_readiness(
    repository: Repository,
    action: Row,
    *,
    profile: StrategyProfile,
    allow_live_auto: bool,
    live_market_ids: set[str],
    require_fresh_reconciliation: bool,
    reconciliation_fresh: bool,
    risk_status: str,
    dry_run_only: bool = False,
    live_whitelist_open: bool = False,
) -> LiveActionReadiness:
    gates = live_action_gates(
        repository,
        action,
        profile=profile,
        allow_live_auto=allow_live_auto,
        live_market_ids=live_market_ids,
        require_fresh_reconciliation=require_fresh_reconciliation,
        reconciliation_fresh=reconciliation_fresh,
        risk_status=risk_status,
        dry_run_only=dry_run_only,
        live_whitelist_open=live_whitelist_open,
    )
    return LiveActionReadiness(
        action_id=action["id"],
        market_id=action["market_id"],
        status=action["status"],
        gates=gates,
        can_auto_execute=all(gate.ok for gate in gates),
    )


def live_action_gates(
    repository: Repository,
    action: Row,
    *,
    profile: StrategyProfile,
    allow_live_auto: bool,
    live_market_ids: set[str],
    require_fresh_reconciliation: bool,
    reconciliation_fresh: bool,
    risk_status: str,
    dry_run_only: bool = False,
    live_whitelist_open: bool = False,
) -> list[LiveGate]:
    market_id = action["market_id"]
    override = repository.effective_strategy_override(market_id, profile.name)
    profile_allows_live = (
        profile.name in {"micro-live", "full-live"}
        and profile.normalized_action_kind() == "trade_live"
    )
    # Open whitelist (full-auto without LIVE_MARKET_IDS): any market may pass.
    # Restricted list: only explicit ids. Empty list + closed = deny (legacy safe).
    whitelist_ok = live_whitelist_open or (market_id in live_market_ids)
    return [
        LiveGate(
            "live_auto",
            (not dry_run_only) and allow_live_auto,
            "live auto is enabled",
            None if allow_live_auto else "allow_live_auto=false",
        )
        if (not dry_run_only) and allow_live_auto
        else LiveGate(
            "live_auto",
            False,
            "live auto is disabled",
            "dry_run_only=true" if dry_run_only else "allow_live_auto=false",
        ),
        LiveGate(
            "profile",
            profile_allows_live,
            "profile allows live auto"
            if profile_allows_live
            else "profile is not micro-live/full-live trade_live",
            profile.name,
        ),
        LiveGate(
            "whitelist",
            whitelist_ok,
            "market whitelist open"
            if live_whitelist_open
            else (
                "market is whitelisted"
                if market_id in live_market_ids
                else "market is not whitelisted"
            ),
            market_id if not live_whitelist_open else "open",
        ),
        LiveGate(
            "override",
            live_auto_enabled_by_override(override),
            "live auto override is enabled"
            if live_auto_enabled_by_override(override)
            else "live auto override is not enabled",
            market_id,
        ),
        LiveGate(
            "reconciliation",
            (not require_fresh_reconciliation) or reconciliation_fresh,
            "fresh reconciliation is present"
            if require_fresh_reconciliation
            else "fresh reconciliation is not required",
            None if reconciliation_fresh else "stale_or_missing",
        ),
        LiveGate(
            "risk",
            risk_status == "ok",
            "risk guard is ok" if risk_status == "ok" else "risk guard is not ok",
            risk_status,
        ),
    ]


def live_monitor_risk_anomalies(
    repository: Repository,
    *,
    settings: Settings,
    reconciliation_fresh: bool,
    require_fresh_reconciliation: bool,
    block_live_on_positions: bool,
) -> list[str]:
    anomalies: list[str] = []
    latest_reconciliation = repository.latest_reconciliation()
    latest_success = repository.latest_successful_reconciliation()
    latest_status = latest_reconciliation["status"] if latest_reconciliation else None
    if require_fresh_reconciliation:
        if latest_success is None:
            anomalies.append("no successful reconciliation")
        elif not reconciliation_fresh:
            anomalies.append("successful reconciliation is stale")
    if latest_status and latest_status != "ok":
        anomalies.append(f"latest reconciliation status is {latest_status}")
    nonzero_positions = repository.nonzero_positions_count()
    if block_live_on_positions and nonzero_positions:
        anomalies.append(f"nonzero positions present: {nonzero_positions}")
    today = datetime.now(timezone.utc).date().isoformat()
    daily = repository.daily_order_notional(today)
    if daily > settings.max_daily_usdc:
        anomalies.append(f"daily live notional exceeds {settings.max_daily_usdc}")
    for market in repository.list_weather_markets():
        exposure = repository.market_exposure(market["id"])
        if exposure > settings.max_market_usdc:
            anomalies.append(f"market {market['id']} exposure exceeds {settings.max_market_usdc}")
    return anomalies


def is_fresh_reconciliation(row) -> bool:
    if row is None:
        return False
    created_at = datetime.fromisoformat(row["created_at"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created_at).total_seconds() <= 300


def _unique_blockers(blockers: list[str]) -> list[str]:
    unique: list[str] = []
    for blocker in blockers:
        if blocker not in unique:
            unique.append(blocker)
    return unique
