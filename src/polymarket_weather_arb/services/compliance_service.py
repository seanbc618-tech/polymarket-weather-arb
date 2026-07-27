from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


from polymarket_weather_arb.adapters.http_client import build_httpx_client

from polymarket_weather_arb.config import Settings

FetchJson = Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class ComplianceDecision:
    ok: bool
    status: str
    reason: str
    country: str | None = None


class ComplianceService:
    def __init__(self, settings: Settings, *, fetch_json: FetchJson | None = None) -> None:
        self.settings = settings
        self.fetch_json = fetch_json or _http_fetch_json

    def check_live_allowed(self) -> ComplianceDecision:
        if self.settings.trading_disabled:
            return ComplianceDecision(
                ok=False,
                status="disabled",
                reason="TRADING_DISABLED=true blocks live trading",
            )
        if not self.settings.compliance_check_enabled:
            return ComplianceDecision(
                ok=True,
                status="check_disabled",
                reason="COMPLIANCE_CHECK_ENABLED=false",
            )
        try:
            payload = self.fetch_json(self.settings.polymarket_geoblock_api_url)
        except Exception as exc:
            return ComplianceDecision(
                ok=False,
                status="unknown",
                reason=f"geoblock check failed: {exc}",
            )
        country = _country_code(payload)
        blocked = bool(
            payload.get("blocked") or payload.get("isBlocked") or payload.get("restricted")
        )
        if blocked:
            return ComplianceDecision(
                ok=False,
                status="blocked",
                reason=f"geoblock response blocks country={country or 'unknown'}",
                country=country,
            )
        allowed = _allowed_countries(self.settings)
        if not country:
            return ComplianceDecision(
                ok=False,
                status="unknown",
                reason="geoblock response did not include a country code",
            )
        if country not in allowed:
            return ComplianceDecision(
                ok=False,
                status="disallowed_country",
                reason=f"country={country} is not in COMPLIANCE_ALLOWED_COUNTRIES",
                country=country,
            )
        return ComplianceDecision(
            ok=True,
            status="allowed",
            reason=f"country={country} is allowed",
            country=country,
        )

    def ensure_live_allowed(self) -> None:
        decision = self.check_live_allowed()
        if not decision.ok:
            raise ValueError(decision.reason)


def _http_fetch_json(url: str) -> dict[str, Any]:
    with build_httpx_client(timeout=10) as client:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("geoblock response is not an object")
    return payload


def _country_code(payload: dict[str, Any]) -> str | None:
    raw = (
        payload.get("country")
        or payload.get("countryCode")
        or payload.get("country_code")
        or _nested_country(payload.get("geolocation"))
        or _nested_country(payload.get("location"))
    )
    return str(raw).strip().upper() if raw else None


def _nested_country(value: object) -> object | None:
    if isinstance(value, dict):
        return value.get("country") or value.get("countryCode") or value.get("country_code")
    return None


def _allowed_countries(settings: Settings) -> set[str]:
    return {
        part.strip().upper()
        for part in settings.compliance_allowed_countries.split(",")
        if part.strip()
    }
