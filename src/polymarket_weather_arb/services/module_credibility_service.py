from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from polymarket_weather_arb.domain.source_grade import (
    OFFICIAL_FORECAST,
    RESEARCH_FORECAST,
    is_live_eligible_forecast_grade,
    normalize_source_grade,
)
from polymarket_weather_arb.modules.registry import get_module


@dataclass(frozen=True)
class ModuleCredibilitySnapshot:
    module_id: str
    rule_confidence: float | None
    rule_status: str
    data_source: str | None
    source_grade: str
    forecast_age_seconds: int | None
    analysis_model: str | None
    live_eligibility: str
    reasons: list[str]
    promotion_criteria: list[str]
    blockers: list[str]


def _load_data_source_strategy() -> dict:
    """加载数据源选择策略"""
    path = Path(__file__).parent.parent.parent.parent / "data" / "data_source_strategy.json"
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _is_us_market(location: str | None) -> bool:
    """判断是否是美国市场"""
    if not location:
        return False
    us_cities = [
        "new york",
        "nyc",
        "los angeles",
        "la",
        "chicago",
        "miami",
        "san francisco",
        "sf",
        "seattle",
        "boston",
        "washington dc",
        "dc",
        "denver",
        "phoenix",
        "houston",
        "dallas",
        "atlanta",
    ]
    return location.lower() in us_cities


def _is_china_market(location: str | None) -> bool:
    """判断是否是中国市场"""
    if not location:
        return False
    china_cities = [
        "beijing",
        "shanghai",
        "guangzhou",
        "shenzhen",
        "chengdu",
        "chongqing",
        "hangzhou",
        "wuhan",
        "nanjing",
        "tianjin",
        "xian",
        "xiamen",
        "changsha",
        "zhengzhou",
        "qingdao",
    ]
    return location.lower() in china_cities


def build_module_credibility(
    *,
    module_id: str,
    rule_confidence: float | None,
    source: str | None,
    source_grade: str | None,
    forecast_age_seconds: int | None,
    analysis_model: str | None,
    location: str | None = None,
) -> ModuleCredibilitySnapshot:
    module = get_module(module_id)
    normalized_source_grade = source_grade or "unknown"
    rule_status = _rule_status(rule_confidence, module.min_rule_confidence)
    live_eligibility = module.live_eligibility
    reasons = _reasons(
        module=module,
        rule_confidence=rule_confidence,
        source=source,
        source_grade=normalized_source_grade,
        forecast_age_seconds=forecast_age_seconds,
        live_eligibility=live_eligibility,
        location=location,
    )
    blockers = _blockers(
        module=module,
        rule_confidence=rule_confidence,
        source=source,
        source_grade=normalized_source_grade,
        location=location,
    )
    return ModuleCredibilitySnapshot(
        module_id=module_id,
        rule_confidence=rule_confidence,
        rule_status=rule_status,
        data_source=source,
        source_grade=normalized_source_grade,
        forecast_age_seconds=forecast_age_seconds,
        analysis_model=analysis_model,
        live_eligibility=live_eligibility,
        reasons=reasons,
        promotion_criteria=module.promotion_criteria,
        blockers=blockers,
    )


def _rule_status(rule_confidence: float | None, min_confidence: float) -> str:
    if rule_confidence is not None and rule_confidence >= min_confidence:
        return "clear"
    return "needs_review"


def _reasons(
    *,
    module: object,
    rule_confidence: float | None,
    source: str | None,
    source_grade: str,
    forecast_age_seconds: int | None,
    live_eligibility: str,
    location: str | None = None,
) -> list[str]:
    reasons: list[str] = []
    if rule_confidence is None:
        reasons.append("rule confidence is missing")
    elif rule_confidence < module.min_rule_confidence:
        reasons.append(f"rule confidence below {module.min_rule_confidence}")
    if not source:
        reasons.append("data source is missing")
    if not _source_live_ok(module, source_grade):
        reasons.append(f"source grade is {source_grade} (live needs {OFFICIAL_FORECAST})")
    # 检查数据源是否匹配市场地区
    if source and location and module.id != "global_temp_bucket":
        if _is_us_market(location) and source not in {"noaa", "NOAA", "NWS"}:
            reasons.append(f"US market should use NOAA, got {source}")
        elif _is_china_market(location) and source not in {"china_official", "CMA", "NMC"}:
            reasons.append(f"China market should use official source, got {source}")
    if forecast_age_seconds is None:
        reasons.append("forecast freshness is missing")
    if live_eligibility == "research_only":
        reasons.append("module is research-only")
    elif live_eligibility == "dry_run_only":
        reasons.append("module is dry-run-only until live gates are proven")
    if not reasons:
        reasons.append("module credibility checks passed")
    return reasons


def _blockers(
    *,
    module: object,
    rule_confidence: float | None,
    source: str | None,
    source_grade: str,
    location: str | None = None,
) -> list[str]:
    blockers: list[str] = []
    if rule_confidence is not None and rule_confidence < module.min_rule_confidence:
        blockers.append(
            f"Rule confidence {rule_confidence:.2f} below minimum {module.min_rule_confidence}"
        )
    if not source:
        blockers.append("No data source configured")
    if not _source_live_ok(module, source_grade):
        blockers.append(f"Source grade is {source_grade}, not {OFFICIAL_FORECAST}")
    # 检查数据源是否匹配市场地区
    if source and location and module.id != "global_temp_bucket":
        if _is_us_market(location) and source not in {"noaa", "NOAA", "NWS"}:
            blockers.append(f"US market requires NOAA data source, got {source}")
        elif _is_china_market(location) and source not in {"china_official", "CMA", "NMC"}:
            blockers.append(f"China market requires official data source, got {source}")
    if module.live_eligibility == "research_only":
        blockers.append("Module is research-only")
    elif module.live_eligibility == "dry_run_only":
        blockers.append("Module is dry-run-only")
    return blockers


def _source_live_ok(module: object, source_grade: str) -> bool:
    if is_live_eligible_forecast_grade(source_grade):
        return True
    return (
        module.id == "global_temp_bucket"
        and normalize_source_grade(source_grade) == RESEARCH_FORECAST
    )
