"""Full-auto full-live plan resolution tests (offline)."""

from __future__ import annotations

import pytest

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.profiles import get_profile
from polymarket_weather_arb.services.full_auto_service import (
    clear_legacy_global_live_overrides,
    describe_full_auto_plan,
    resolve_full_auto_plan,
)
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def _settings(tmp_path, **overrides) -> Settings:
    base = dict(
        DATABASE_PATH=tmp_path / "full-auto.db",
        POLYMARKET_PRIVATE_KEY="k",
        POLYMARKET_FUNDER="0xf",
        TRADING_DISABLED=False,
        AUTO_EXIT_ENABLED=True,
        LIVE_MARKET_IDS="",
    )
    base.update(overrides)
    return Settings(**base)


def test_resolve_full_auto_requires_full_live(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(ValueError, match="full-live"):
        resolve_full_auto_plan(
            settings=settings,
            profile=get_profile("micro-live"),
            live_markets_cli=["m1"],
        )
    with pytest.raises(ValueError, match="full-live"):
        resolve_full_auto_plan(
            settings=settings,
            profile=get_profile("balanced"),
            live_markets_cli=["m1"],
        )


def test_resolve_full_auto_implies_auto_exit(tmp_path):
    settings = _settings(tmp_path, AUTO_EXIT_ENABLED=False)
    plan = resolve_full_auto_plan(
        settings=settings,
        profile=get_profile("full-live"),
        live_markets_cli=["m1"],
    )
    assert plan.allow_auto_exit is True


def test_resolve_full_auto_open_whitelist_when_empty(tmp_path):
    settings = _settings(tmp_path, LIVE_MARKET_IDS="")
    plan = resolve_full_auto_plan(
        settings=settings,
        profile=get_profile("full-live"),
        live_markets_cli=None,
    )
    assert plan.live_whitelist_open is True
    assert plan.live_market_ids == frozenset()
    assert plan.profile_name == "full-live"
    assert any("whitelist OPEN" in n for n in plan.notes)


def test_resolve_full_auto_arms_buy_and_sell_gates(tmp_path):
    settings = _settings(tmp_path, LIVE_MARKET_IDS="m1,m2")
    plan = resolve_full_auto_plan(
        settings=settings,
        profile=get_profile("full-live"),
        live_markets_cli=["m2", "m3"],
        max_live_actions_per_tick=5,
    )
    assert plan.dry_run_only is False
    assert plan.allow_live_auto is True
    assert plan.allow_auto_exit is True
    assert plan.block_live_on_positions is False
    assert plan.include_reconciliation is True
    assert plan.auto_dry_run is False
    assert plan.max_live_actions_per_tick == 1  # capped for full-auto
    assert plan.live_whitelist_open is False
    assert plan.live_market_ids == frozenset({"m1", "m2", "m3"})
    desc = describe_full_auto_plan(plan)
    assert desc["mode"] == "full-live"
    assert desc["profile"] == "full-live"
    assert "trade_live" in str(desc["entry"])
    assert "position_at_risk" in str(desc["exit"])


def test_clear_legacy_global_live_overrides_preserves_market_specific_approval(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    repo.upsert_strategy_override(
        market_id="*", profile="full-live", live_auto_enabled=True
    )
    repo.upsert_strategy_override(
        market_id="*", profile="micro-live", live_auto_enabled=True
    )
    repo.upsert_strategy_override(
        market_id="m1", profile="micro-live", live_auto_enabled=True
    )

    clear_legacy_global_live_overrides(repo)

    assert repo.get_strategy_override("*", "full-live") is None
    assert repo.get_strategy_override("*", "micro-live") is None
    assert repo.get_strategy_override("m1", "micro-live") is not None
    conn.close()


def test_resolve_blocked_when_trading_disabled(tmp_path):
    settings = _settings(tmp_path, TRADING_DISABLED=True, LIVE_MARKET_IDS="m1")
    with pytest.raises(ValueError, match="TRADING_DISABLED"):
        resolve_full_auto_plan(
            settings=settings,
            profile=get_profile("full-live"),
            live_markets_cli=None,
        )
