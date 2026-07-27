from __future__ import annotations

import pytest

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.services.compliance_service import ComplianceService


def test_trading_disabled_blocks_live_execution():
    service = ComplianceService(
        Settings(TRADING_DISABLED=True),
        fetch_json=lambda url: {"blocked": False, "country": "HK"},
    )

    decision = service.check_live_allowed()

    assert decision.ok is False
    assert decision.status == "disabled"
    assert "TRADING_DISABLED" in decision.reason


def test_hk_geoblock_response_allows_live_execution():
    service = ComplianceService(
        Settings(TRADING_DISABLED=False, COMPLIANCE_ALLOWED_COUNTRIES="HK"),
        fetch_json=lambda url: {"blocked": False, "country": "HK"},
    )

    decision = service.check_live_allowed()

    assert decision.ok is True
    assert decision.country == "HK"
    assert decision.status == "allowed"


def test_blocked_or_disallowed_country_fails_closed():
    blocked = ComplianceService(
        Settings(TRADING_DISABLED=False, COMPLIANCE_ALLOWED_COUNTRIES="HK"),
        fetch_json=lambda url: {"blocked": True, "country": "US"},
    ).check_live_allowed()
    disallowed = ComplianceService(
        Settings(TRADING_DISABLED=False, COMPLIANCE_ALLOWED_COUNTRIES="HK"),
        fetch_json=lambda url: {"blocked": False, "countryCode": "SG"},
    ).check_live_allowed()

    assert blocked.ok is False
    assert blocked.status == "blocked"
    assert disallowed.ok is False
    assert disallowed.status == "disallowed_country"


def test_geoblock_fetch_errors_fail_closed():
    def fail(url: str):
        raise RuntimeError("network down")

    decision = ComplianceService(
        Settings(TRADING_DISABLED=False), fetch_json=fail
    ).check_live_allowed()

    assert decision.ok is False
    assert decision.status == "unknown"
    assert "network down" in decision.reason


def test_compliance_check_can_be_explicitly_disabled_for_dry_lab_runs():
    service = ComplianceService(
        Settings(TRADING_DISABLED=False, COMPLIANCE_CHECK_ENABLED=False)
    )

    decision = service.check_live_allowed()

    assert decision.ok is True
    assert decision.status == "check_disabled"


def test_ensure_live_allowed_raises_on_blocked_state():
    service = ComplianceService(Settings(TRADING_DISABLED=True))

    with pytest.raises(ValueError, match="TRADING_DISABLED"):
        service.ensure_live_allowed()
