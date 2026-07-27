from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from sqlite3 import Row
from typing import Mapping

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.risk import (
    HARDCODED_MAX_DAILY_USDC,
    HARDCODED_MAX_MARKET_USDC,
    HARDCODED_MAX_ORDER_USDC,
)
from polymarket_weather_arb.services.automation_service import normalize_kind


@dataclass(frozen=True)
class StrategyProfile:
    name: str
    description: str
    role: str
    default_action_kind: str
    action_ttl_minutes: int
    discovery_limit: int
    discovery_pages: int
    dry_run: bool
    max_order_usdc: Decimal | None = None
    max_daily_usdc: Decimal | None = None
    max_market_usdc: Decimal | None = None
    min_edge: Decimal | None = None

    def normalized_action_kind(self) -> str:
        return normalize_kind(self.default_action_kind)


BUILTIN_PROFILES: dict[str, StrategyProfile] = {
    "balanced": StrategyProfile(
        name="balanced",
        description="Current default workflow with hard live-trading gates unchanged.",
        role="operator",
        default_action_kind="analyze",
        action_ttl_minutes=60,
        discovery_limit=50,
        discovery_pages=1,
        dry_run=True,
    ),
    "conservative": StrategyProfile(
        name="conservative",
        description="Tighter sizing and shorter approval TTL for cautious live-readiness review.",
        role="risk",
        default_action_kind="dry_run",
        action_ttl_minutes=20,
        discovery_limit=30,
        discovery_pages=1,
        dry_run=True,
        max_order_usdc=Decimal("10"),
        max_daily_usdc=Decimal("40"),
        max_market_usdc=Decimal("20"),
        min_edge=Decimal("0.08"),
    ),
    "dry-run-demo": StrategyProfile(
        name="dry-run-demo",
        description="Demo-friendly profile for fixture-backed dry-run operator testing.",
        role="demo",
        default_action_kind="dry_run",
        action_ttl_minutes=60,
        discovery_limit=20,
        discovery_pages=1,
        dry_run=True,
        max_order_usdc=Decimal("5"),
        max_daily_usdc=Decimal("20"),
        max_market_usdc=Decimal("10"),
    ),
    "research-only": StrategyProfile(
        name="research-only",
        description="Discovery and analysis oriented; proposes analysis actions by default.",
        role="research",
        default_action_kind="analyze",
        action_ttl_minutes=120,
        discovery_limit=100,
        discovery_pages=2,
        dry_run=True,
        min_edge=Decimal("0.07"),
    ),
    "micro-live": StrategyProfile(
        name="micro-live",
        description="Default-off micro live profile with very tight caps and mandatory daemon gates.",
        role="risk",
        default_action_kind="trade_live",
        action_ttl_minutes=5,
        discovery_limit=10,
        discovery_pages=1,
        dry_run=False,
        max_order_usdc=Decimal("5"),
        max_daily_usdc=Decimal("10"),
        max_market_usdc=Decimal("5"),
        min_edge=Decimal("0.10"),
    ),
    "full-live": StrategyProfile(
        name="full-live",
        description=(
            "Full live autopilot using operator-configured risk caps and min-edge "
            "(still hard-capped). Automatic entry and exit under existing live gates."
        ),
        role="risk",
        default_action_kind="trade_live",
        action_ttl_minutes=15,
        discovery_limit=50,
        discovery_pages=1,
        dry_run=False,
        # None = inherit Settings (MAX_* / MIN_EDGE); hard caps apply at trade time.
        max_order_usdc=None,
        max_daily_usdc=None,
        max_market_usdc=None,
        min_edge=None,
    ),
}


def list_profiles() -> list[StrategyProfile]:
    return [BUILTIN_PROFILES[name] for name in sorted(BUILTIN_PROFILES)]


def get_profile(name: str | None) -> StrategyProfile:
    key = name or "balanced"
    try:
        return BUILTIN_PROFILES[key]
    except KeyError as exc:
        allowed = ", ".join(sorted(BUILTIN_PROFILES))
        raise ValueError(f"unknown profile: {key}; allowed: {allowed}") from exc


def settings_for_profile(settings: Settings, profile: StrategyProfile) -> Settings:
    updates = {}
    if profile.max_order_usdc is not None:
        updates["max_order_usdc"] = min(
            settings.max_order_usdc, profile.max_order_usdc, HARDCODED_MAX_ORDER_USDC
        )
    if profile.max_daily_usdc is not None:
        updates["max_daily_usdc"] = min(
            settings.max_daily_usdc, profile.max_daily_usdc, HARDCODED_MAX_DAILY_USDC
        )
    if profile.max_market_usdc is not None:
        updates["max_market_usdc"] = min(
            settings.max_market_usdc, profile.max_market_usdc, HARDCODED_MAX_MARKET_USDC
        )
    if profile.min_edge is not None:
        updates["min_edge"] = max(settings.min_edge, profile.min_edge)
    return settings.model_copy(update=updates)


def settings_for_override(
    settings: Settings, override: Mapping[str, object] | Row | None
) -> Settings:
    if override is None:
        return settings
    updates = {}
    max_order = _override_decimal(override, "max_order_usdc")
    max_daily = _override_decimal(override, "max_daily_usdc")
    max_market = _override_decimal(override, "max_market_usdc")
    min_edge = _override_decimal(override, "min_edge")
    if max_order is not None:
        updates["max_order_usdc"] = min(
            settings.max_order_usdc, max_order, HARDCODED_MAX_ORDER_USDC
        )
    if max_daily is not None:
        updates["max_daily_usdc"] = min(
            settings.max_daily_usdc, max_daily, HARDCODED_MAX_DAILY_USDC
        )
    if max_market is not None:
        updates["max_market_usdc"] = min(
            settings.max_market_usdc, max_market, HARDCODED_MAX_MARKET_USDC
        )
    if min_edge is not None:
        updates["min_edge"] = max(settings.min_edge, min_edge)
    return settings.model_copy(update=updates)


def live_auto_enabled_by_override(override: Mapping[str, object] | Row | None) -> bool:
    return bool(override is not None and override["live_auto_enabled"])


def profile_summary(profile: StrategyProfile) -> dict[str, object]:
    return {
        "name": profile.name,
        "role": profile.role,
        "description": profile.description,
        "default_action_kind": profile.normalized_action_kind(),
        "action_ttl_minutes": profile.action_ttl_minutes,
        "discovery_limit": profile.discovery_limit,
        "discovery_pages": profile.discovery_pages,
        "dry_run": profile.dry_run,
        "max_order_usdc": str(profile.max_order_usdc)
        if profile.max_order_usdc is not None
        else None,
        "max_daily_usdc": str(profile.max_daily_usdc)
        if profile.max_daily_usdc is not None
        else None,
        "max_market_usdc": str(profile.max_market_usdc)
        if profile.max_market_usdc is not None
        else None,
        "min_edge": str(profile.min_edge) if profile.min_edge is not None else None,
    }


def _override_decimal(override: Mapping[str, object] | Row, key: str) -> Decimal | None:
    value = override[key]
    return None if value is None else Decimal(str(value))
