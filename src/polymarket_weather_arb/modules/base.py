from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketModule:
    id: str
    label_key: str
    description_key: str
    supports_discovery: bool
    supports_analysis: bool
    supports_dry_run: bool
    # Live eligibility levels
    live_eligibility: str  # research_only, dry_run_only, candidate_gate_required, micro_live_ready
    # Promotion criteria
    requires_official_source: bool
    requires_settlement_grade: bool
    min_rule_confidence: float
    # Documentation
    promotion_criteria: list[str]
    blockers: list[str]
