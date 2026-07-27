"""Full-live full-auto mode: auto-buy + auto-exit under explicit operator arming.

Full auto means:
  automatic BUY when trade_live signal clears all gates
  + automatic SELL when position exists and ExitGuardian says position_at_risk

Defaults remain OFF. ``--full-auto`` is the explicit operator instruction that
arms automatic BUY and SELL; credentials, compliance, and execution gates remain.

Market whitelist is **optional**. Empty CLI/env list = open to all local candidates
(typical when the weather universe is small). Non-empty list still narrows entry.
"""

from __future__ import annotations

from dataclasses import dataclass

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.profiles import StrategyProfile
from polymarket_weather_arb.services.live_launchpad_service import live_market_ids_from_settings
from polymarket_weather_arb.storage.repositories import Repository

FULL_AUTO_PROFILE = "full-live"


@dataclass(frozen=True)
class FullAutoDaemonPlan:
    """Resolved daemon flags for a single full-auto session."""

    profile_name: str
    dry_run_only: bool
    allow_live_auto: bool
    allow_auto_exit: bool
    block_live_on_positions: bool
    include_reconciliation: bool
    auto_dry_run: bool
    max_live_actions_per_tick: int
    live_market_ids: frozenset[str]
    # True when no explicit whitelist: live gate allows any market id.
    live_whitelist_open: bool
    notes: tuple[str, ...]


def resolve_full_auto_plan(
    *,
    settings: Settings,
    profile: StrategyProfile,
    live_markets_cli: list[str] | None,
    max_live_actions_per_tick: int = 1,
) -> FullAutoDaemonPlan:
    """Validate and resolve full-auto daemon posture.

    Raises ValueError with an operator-facing reason when full-auto cannot arm.
    """
    notes: list[str] = []
    if profile.name != FULL_AUTO_PROFILE:
        raise ValueError(
            f"full-auto requires --profile {FULL_AUTO_PROFILE} (got profile={profile.name!r})"
        )
    if settings.trading_disabled:
        raise ValueError("full-auto blocked: TRADING_DISABLED=true")
    try:
        settings.ensure_live_trading_ready()
    except ValueError as exc:
        raise ValueError(f"full-auto blocked: {exc}") from exc

    cli_ids = {item.strip() for item in (live_markets_cli or []) if item and item.strip()}
    env_ids = live_market_ids_from_settings(settings)
    live_ids = cli_ids | env_ids
    # Empty whitelist is intentional: small weather universe does not need one.
    live_whitelist_open = len(live_ids) == 0
    if live_whitelist_open:
        notes.append("no LIVE_MARKET_IDS / --live-market: whitelist OPEN (all local candidates)")
    else:
        notes.append(f"whitelist restricted to {sorted(live_ids)}")
        if cli_ids and env_ids and cli_ids != env_ids:
            notes.append(f"whitelist union: cli={sorted(cli_ids)} env={sorted(env_ids)}")
    notes.append("full-auto arms: buy(trade_live)+sell(auto-exit) under full-live gates")
    notes.append("quantitative signal drives entry; LLM review is advisory and cannot veto")
    notes.append("positions allowed so buy is not blocked after first fill")
    notes.append("auto_dry_run disabled so ticks focus on trade_live + auto-exit")

    return FullAutoDaemonPlan(
        profile_name=profile.name,
        dry_run_only=False,
        allow_live_auto=True,
        allow_auto_exit=True,
        block_live_on_positions=False,
        include_reconciliation=True,
        auto_dry_run=False,
        max_live_actions_per_tick=max(1, min(max_live_actions_per_tick, 1)),
        live_market_ids=frozenset(live_ids),
        live_whitelist_open=live_whitelist_open,
        notes=tuple(notes),
    )


def clear_legacy_global_live_overrides(repository: Repository) -> None:
    """Remove old profile-wide mode markers superseded by ``app_mode``.

    Per-market micro-live approvals are preserved. Only the historical ``*``
    rows that duplicated runtime mode are removed.
    """
    repository.delete_strategy_override("*", "micro-live")
    repository.delete_strategy_override("*", "full-live")


def arm_legacy_operator_live_overrides(
    repository: Repository,
    *,
    market_ids: set[str] | frozenset[str],
    profile_name: str,
    open_whitelist: bool,
) -> list[str]:
    """Compatibility arming for the advanced legacy OperatorDaemon only."""
    armed: list[str] = []
    if open_whitelist:
        repository.upsert_strategy_override(
            market_id="*",
            profile=profile_name,
            live_auto_enabled=True,
            notes="legacy operator daemon full-auto",
        )
        return [f"*:{profile_name}"]
    for market_id in sorted(market_ids):
        if repository.get_market(market_id) is None:
            continue
        repository.upsert_strategy_override(
            market_id=market_id,
            profile=profile_name,
            live_auto_enabled=True,
            notes="legacy operator daemon full-auto",
        )
        armed.append(market_id)
    return armed


def describe_full_auto_plan(plan: FullAutoDaemonPlan) -> dict[str, object]:
    return {
        "mode": "full-live",
        "profile": plan.profile_name,
        "dry_run_only": plan.dry_run_only,
        "allow_live_auto": plan.allow_live_auto,
        "allow_auto_exit": plan.allow_auto_exit,
        "block_live_on_positions": plan.block_live_on_positions,
        "include_reconciliation": plan.include_reconciliation,
        "auto_dry_run": plan.auto_dry_run,
        "max_live_actions_per_tick": plan.max_live_actions_per_tick,
        "live_market_ids": sorted(plan.live_market_ids),
        "live_whitelist_open": plan.live_whitelist_open,
        "notes": list(plan.notes),
        "entry": "trade signal -> trade_live action -> gated live limit BUY",
        "exit": "nonzero position + ExitGuardian position_at_risk -> gated limit SELL",
    }
