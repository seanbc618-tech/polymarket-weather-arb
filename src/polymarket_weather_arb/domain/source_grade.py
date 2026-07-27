"""Weather data provenance grades.

Forecast products (used for trading decisions) are not the same thing as
settlement observations (used after the fact to resolve markets).

Canonical grades written by adapters:

- ``official_forecast``: official agency forecast product (e.g. NOAA/NWS
  forecast). Eligible for live trading when used as the analysis forecast.
- ``research_forecast``: research / signal-only models (Open-Meteo, ensemble).
  Dry-run and research only.
- ``settlement_observation``: observed weather used for settlement / backfill.
  Never a live trading forecast source.

Safe handling of legacy payloads:

- ``research_grade`` → treated as ``research_forecast`` (non-live).
- ``settlement_grade`` → legacy mislabel that mixed forecast and settlement;
  treated as ``legacy`` and **not** live-eligible (require refresh).
- missing / unknown / ``signal_only`` → ``unknown`` / non-live.

Live trading only allows ``official_forecast``. Missing grades never default
to live-eligible.
"""

from __future__ import annotations

from typing import Any

# Canonical grades
OFFICIAL_FORECAST = "official_forecast"
RESEARCH_FORECAST = "research_forecast"
SETTLEMENT_OBSERVATION = "settlement_observation"
UNKNOWN = "unknown"
LEGACY = "legacy"

# Legacy tokens still present in older DB rows / tests
LEGACY_RESEARCH_GRADE = "research_grade"
LEGACY_SETTLEMENT_GRADE = "settlement_grade"
LEGACY_SIGNAL_ONLY = "signal_only"

LIVE_ELIGIBLE_FORECAST_GRADES = frozenset({OFFICIAL_FORECAST})

# Grades that are never acceptable as a live trading *forecast* input.
NON_FORECAST_GRADES = frozenset({SETTLEMENT_OBSERVATION})

GRADE_LABELS = {
    OFFICIAL_FORECAST: "Official forecast (agency product; live-eligible)",
    RESEARCH_FORECAST: "Research forecast (signal-only; dry-run)",
    SETTLEMENT_OBSERVATION: "Settlement observation (resolution only; not a forecast)",
    UNKNOWN: "Unknown provenance (refresh required for live)",
    LEGACY: "Legacy / ambiguous grade (refresh required for live)",
    LEGACY_RESEARCH_GRADE: "Research forecast (legacy token)",
    LEGACY_SETTLEMENT_GRADE: "Legacy settlement_grade token (ambiguous; not live)",
    LEGACY_SIGNAL_ONLY: "Signal-only (legacy token)",
}


def normalize_source_grade(raw: str | None) -> str:
    """Map raw tokens onto the canonical grade vocabulary."""
    if raw is None or raw == "":
        return UNKNOWN
    if raw == LEGACY_RESEARCH_GRADE:
        return RESEARCH_FORECAST
    if raw == LEGACY_SETTLEMENT_GRADE:
        return LEGACY
    if raw == LEGACY_SIGNAL_ONLY:
        return UNKNOWN
    if raw in {
        OFFICIAL_FORECAST,
        RESEARCH_FORECAST,
        SETTLEMENT_OBSERVATION,
        UNKNOWN,
        LEGACY,
    }:
        return raw
    return LEGACY


def is_live_eligible_forecast_grade(grade: str | None) -> bool:
    """True only for explicit official forecasts. Never promotes legacy tokens."""
    return normalize_source_grade(grade) in LIVE_ELIGIBLE_FORECAST_GRADES


def extract_forecast_source_grade(raw_payload: dict[str, Any] | None) -> str:
    """Extract a forecast provenance grade from a saved forecast raw_payload.

    Does **not** promote ``official_signal`` alone to live-eligible grades.
    Missing or ambiguous payloads return ``unknown`` / ``legacy``.
    """
    if not raw_payload:
        return UNKNOWN
    grade = raw_payload.get("source_grade")
    if grade is None or grade == "":
        return UNKNOWN
    return normalize_source_grade(str(grade))


def live_forecast_rejection_reason(grade: str | None) -> str | None:
    """Return a rejection reason when grade is not live-eligible, else None."""
    if is_live_eligible_forecast_grade(grade):
        return None
    normalized = normalize_source_grade(grade)
    if normalized == SETTLEMENT_OBSERVATION:
        return (
            "forecast source is a settlement observation, not an official forecast; "
            "refresh with an official forecast product"
        )
    if normalized in {UNKNOWN, LEGACY}:
        return (
            f"forecast source grade is {grade or normalized}; "
            "live trading requires official_forecast (refresh forecast)"
        )
    return (
        f"forecast source is not official_forecast (got {grade or normalized}); "
        "live trading requires official agency forecast provenance"
    )


def grade_label(grade: str | None) -> str:
    if grade is None:
        return GRADE_LABELS[UNKNOWN]
    return GRADE_LABELS.get(grade, f"Unrecognized grade: {grade}")
