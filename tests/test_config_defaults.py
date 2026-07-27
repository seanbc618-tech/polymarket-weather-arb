from decimal import Decimal

from polymarket_weather_arb.config import Settings


def test_settings_defaults_fail_closed_with_conservative_caps(monkeypatch) -> None:
    for key in (
        "TRADING_DISABLED",
        "MAX_ORDER_USDC",
        "MAX_DAILY_USDC",
        "MAX_MARKET_USDC",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=None)

    assert settings.trading_disabled is True
    assert settings.max_order_usdc == Decimal("1")
    assert settings.max_daily_usdc == Decimal("5")
    assert settings.max_market_usdc == Decimal("2")
