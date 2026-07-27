from decimal import Decimal

import pytest

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.profiles import (
    get_profile,
    list_profiles,
    live_auto_enabled_by_override,
    settings_for_override,
    settings_for_profile,
)


def test_builtin_profiles_are_available():
    names = [profile.name for profile in list_profiles()]

    assert names == [
        "balanced",
        "conservative",
        "dry-run-demo",
        "full-live",
        "micro-live",
        "research-only",
    ]
    assert get_profile("dry-run-demo").normalized_action_kind() == "dry_run"
    assert get_profile("micro-live").normalized_action_kind() == "trade_live"
    assert get_profile("full-live").normalized_action_kind() == "trade_live"


def test_unknown_profile_is_rejected():
    with pytest.raises(ValueError, match="unknown profile"):
        get_profile("reckless")


def test_profile_tightens_but_never_loosens_caps():
    settings = Settings(
        MAX_ORDER_USDC=Decimal("999"),
        MAX_DAILY_USDC=Decimal("999"),
        MAX_MARKET_USDC=Decimal("999"),
        MIN_EDGE=Decimal("0.01"),
    )
    conservative = get_profile("conservative")

    effective = settings_for_profile(settings, conservative)

    assert effective.max_order_usdc == Decimal("10")
    assert effective.max_daily_usdc == Decimal("40")
    assert effective.max_market_usdc == Decimal("20")
    assert effective.min_edge == Decimal("0.08")


def test_balanced_profile_still_respects_existing_settings():
    settings = Settings(MAX_ORDER_USDC=Decimal("7"))

    effective = settings_for_profile(settings, get_profile("balanced"))

    assert effective.max_order_usdc == Decimal("7")


def test_micro_live_profile_is_tightly_capped_and_not_dry_run():
    settings = Settings(
        MAX_ORDER_USDC=Decimal("999"), MAX_DAILY_USDC=Decimal("999"), MAX_MARKET_USDC=Decimal("999")
    )

    effective = settings_for_profile(settings, get_profile("micro-live"))

    assert get_profile("micro-live").dry_run is False
    assert effective.max_order_usdc == Decimal("5")
    assert effective.max_daily_usdc == Decimal("10")
    assert effective.max_market_usdc == Decimal("5")


def test_full_live_profile_uses_configured_caps_not_micro_live():
    settings = Settings(
        MAX_ORDER_USDC=Decimal("20"),
        MAX_DAILY_USDC=Decimal("80"),
        MAX_MARKET_USDC=Decimal("40"),
        MIN_EDGE=Decimal("0.06"),
    )
    profile = get_profile("full-live")
    effective = settings_for_profile(settings, profile)

    assert profile.dry_run is False
    assert profile.normalized_action_kind() == "trade_live"
    assert profile.max_order_usdc is None
    assert profile.min_edge is None
    # Does not inherit micro-live 5/10/5 or MIN_EDGE=0.10
    assert effective.max_order_usdc == Decimal("20")
    assert effective.max_daily_usdc == Decimal("80")
    assert effective.max_market_usdc == Decimal("40")
    assert effective.min_edge == Decimal("0.06")

    micro = settings_for_profile(settings, get_profile("micro-live"))
    assert micro.max_order_usdc == Decimal("5")
    assert micro.min_edge == Decimal("0.10")


def test_strategy_override_tightens_but_never_loosens_settings():
    settings = Settings(
        MAX_ORDER_USDC=Decimal("7"),
        MAX_DAILY_USDC=Decimal("40"),
        MAX_MARKET_USDC=Decimal("20"),
        MIN_EDGE=Decimal("0.05"),
    )

    effective = settings_for_override(
        settings,
        {
            "max_order_usdc": 10,
            "max_daily_usdc": 30,
            "max_market_usdc": 50,
            "min_edge": 0.08,
            "live_auto_enabled": 1,
        },
    )

    assert effective.max_order_usdc == Decimal("7")
    assert effective.max_daily_usdc == Decimal("30")
    assert effective.max_market_usdc == Decimal("20")
    assert effective.min_edge == Decimal("0.08")


def test_live_auto_requires_explicit_override_enablement():
    assert live_auto_enabled_by_override(None) is False
    assert live_auto_enabled_by_override({"live_auto_enabled": None}) is False
    assert live_auto_enabled_by_override({"live_auto_enabled": 0}) is False
    assert live_auto_enabled_by_override({"live_auto_enabled": 1}) is True
