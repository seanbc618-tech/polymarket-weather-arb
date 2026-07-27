# Weather Module Roadmap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand the weather automation system with a credibility layer plus `global_temp_bucket`, `precip_snow`, and research-only `hurricane_storm` modules without loosening live-trading gates.

**Architecture:** Keep the current module registry and workflow service, but add a read-only module credibility service that UI and Live Launchpad can use. Reuse `temperature_bucket_rules` for global temperature buckets, reuse `resolution_rules` for precipitation/snow, and keep hurricane/storm research-only until a dedicated NOAA/NHC adapter exists.

**Tech Stack:** Python 3.12, Typer CLI, SQLite repository layer, stdlib dashboard, pytest, ruff.

---

### Task 1: Register Weather Roadmap Modules

**Files:**
- Create: `src/polymarket_weather_arb/modules/global_temp_bucket.py`
- Create: `src/polymarket_weather_arb/modules/precip_snow.py`
- Create: `src/polymarket_weather_arb/modules/hurricane_storm.py`
- Modify: `src/polymarket_weather_arb/modules/registry.py`
- Modify: `src/polymarket_weather_arb/dashboard_ui/i18n.py`
- Test: `tests/test_weather_modules.py`

- [ ] **Step 1: Write the failing registry test**

```python
from polymarket_weather_arb.modules.registry import get_module, list_modules


def test_weather_roadmap_modules_are_registered():
    modules = {module.id: module for module in list_modules()}

    assert get_module("global_temp_bucket").supports_discovery is True
    assert get_module("global_temp_bucket").supports_analysis is True
    assert get_module("global_temp_bucket").supports_dry_run is True
    assert get_module("precip_snow").supports_discovery is True
    assert get_module("precip_snow").supports_analysis is True
    assert get_module("precip_snow").supports_dry_run is True
    assert get_module("hurricane_storm").supports_discovery is True
    assert get_module("hurricane_storm").supports_analysis is False
    assert get_module("hurricane_storm").supports_dry_run is False
    assert {"global_temp_bucket", "precip_snow", "hurricane_storm"} <= set(modules)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_weather_modules.py -q`

Expected: FAIL with `ValueError: unknown module: global_temp_bucket` or missing test file until added.

- [ ] **Step 3: Add module files**

```python
# src/polymarket_weather_arb/modules/global_temp_bucket.py
from __future__ import annotations

from polymarket_weather_arb.modules.base import MarketModule

GLOBAL_TEMP_BUCKET_MODULE = MarketModule(
    id="global_temp_bucket",
    label_key="module.global_temp_bucket.label",
    description_key="module.global_temp_bucket.description",
    supports_discovery=True,
    supports_analysis=True,
    supports_dry_run=True,
)
```

```python
# src/polymarket_weather_arb/modules/precip_snow.py
from __future__ import annotations

from polymarket_weather_arb.modules.base import MarketModule

PRECIP_SNOW_MODULE = MarketModule(
    id="precip_snow",
    label_key="module.precip_snow.label",
    description_key="module.precip_snow.description",
    supports_discovery=True,
    supports_analysis=True,
    supports_dry_run=True,
)
```

```python
# src/polymarket_weather_arb/modules/hurricane_storm.py
from __future__ import annotations

from polymarket_weather_arb.modules.base import MarketModule

HURRICANE_STORM_MODULE = MarketModule(
    id="hurricane_storm",
    label_key="module.hurricane_storm.label",
    description_key="module.hurricane_storm.description",
    supports_discovery=True,
    supports_analysis=False,
    supports_dry_run=False,
)
```

- [ ] **Step 4: Register modules**

Modify `src/polymarket_weather_arb/modules/registry.py` so imports and `MODULES` include all five modules.

- [ ] **Step 5: Add i18n labels**

Add English and Chinese keys for:

```python
"module.global_temp_bucket.label": "Global Temp Buckets"
"module.global_temp_bucket.description": "Non-China 1 degree temperature bucket research, analysis, and dry-run trading."
"module.precip_snow.label": "Precip / Snow"
"module.precip_snow.description": "Rainfall and snowfall threshold market research, analysis, and dry-run trading."
"module.hurricane_storm.label": "Hurricane / Storm"
"module.hurricane_storm.description": "NOAA/NHC storm market research. Trading remains disabled until a dedicated storm model is proven."
```

- [ ] **Step 6: Run registry tests**

Run: `uv run pytest tests/test_weather_modules.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/polymarket_weather_arb/modules tests/test_weather_modules.py src/polymarket_weather_arb/dashboard_ui/i18n.py
git commit -m "Register weather roadmap modules"
```

### Task 2: Add Module Credibility Service

**Files:**
- Create: `src/polymarket_weather_arb/services/module_credibility_service.py`
- Test: `tests/test_module_credibility_service.py`

- [ ] **Step 1: Write failing credibility tests**

```python
from polymarket_weather_arb.services.module_credibility_service import (
    build_module_credibility,
)


def test_credibility_marks_storm_research_only():
    snapshot = build_module_credibility(
        module_id="hurricane_storm",
        rule_confidence=None,
        source="NHC",
        source_grade="signal_only",
        forecast_age_seconds=None,
        analysis_model=None,
    )

    assert snapshot.live_eligibility == "research_only"
    assert "storm module is research-only" in snapshot.reasons


def test_credibility_marks_global_bucket_dry_run_only():
    snapshot = build_module_credibility(
        module_id="global_temp_bucket",
        rule_confidence=0.92,
        source="NOAA",
        source_grade="settlement_grade",
        forecast_age_seconds=100,
        analysis_model="global-temp-bucket-normal-v1",
    )

    assert snapshot.live_eligibility == "dry_run_only"
    assert snapshot.rule_status == "clear"
    assert snapshot.data_source == "NOAA"
```

- [ ] **Step 2: Verify red**

Run: `uv run pytest tests/test_module_credibility_service.py -q`

Expected: FAIL because the service module does not exist.

- [ ] **Step 3: Implement service**

Create a frozen dataclass `ModuleCredibilitySnapshot` with fields:

```python
module_id: str
rule_confidence: float | None
rule_status: str
data_source: str | None
source_grade: str
forecast_age_seconds: int | None
analysis_model: str | None
live_eligibility: str
reasons: list[str]
```

Implement `build_module_credibility(...)` with these rules:

- `hurricane_storm` => `live_eligibility="research_only"`.
- `global_temp_bucket` and `precip_snow` => `live_eligibility="dry_run_only"`.
- existing `weather` and `china_temp_bucket` => `live_eligibility="candidate_gate_required"`.
- `rule_status="clear"` when confidence is at least `0.85`; otherwise `needs_review`.
- add reasons for missing source, missing forecast age, non-settlement source grade, and research/dry-run live status.

- [ ] **Step 4: Verify green**

Run: `uv run pytest tests/test_module_credibility_service.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/polymarket_weather_arb/services/module_credibility_service.py tests/test_module_credibility_service.py
git commit -m "Add module credibility snapshots"
```

### Task 3: Add Global Temperature Bucket Parser And Pricing

**Files:**
- Create: `src/polymarket_weather_arb/domain/global_temperature_bucket.py`
- Create: `src/polymarket_weather_arb/domain/global_bucket_pricing.py`
- Test: `tests/test_global_temperature_bucket.py`

- [ ] **Step 1: Write parser and pricing tests**

Test parsing:

```python
from decimal import Decimal

from polymarket_weather_arb.domain.global_temperature_bucket import (
    parse_global_temperature_bucket_rule,
)


def test_parse_global_temperature_bucket_fahrenheit_range():
    rule = parse_global_temperature_bucket_rule(
        "Will the high temperature in New York be 80-81F on June 10, 2026?",
        "Settlement source: NOAA station KNYC.",
    )

    assert rule.tradable is True
    assert rule.location == "New York"
    assert rule.station == "KNYC"
    assert rule.variable == "temperature_high"
    assert rule.bucket_lower == Decimal("80")
    assert rule.bucket_upper == Decimal("81")
    assert rule.unit == "F"
    assert rule.target_date == "2026-06-10"
```

Test pricing:

```python
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_weather_arb.domain.global_bucket_pricing import (
    analyze_global_bucket_price,
)
from polymarket_weather_arb.domain.weather import ForecastSnapshot


def test_global_bucket_price_trades_when_ask_below_conservative_fair():
    rule = parse_global_temperature_bucket_rule(
        "Will the high temperature in New York be 80-81F on June 10, 2026?",
        "Settlement source: NOAA station KNYC.",
    )
    now = datetime(2026, 6, 9, tzinfo=timezone.utc)
    forecast = ForecastSnapshot(
        provider="NOAA",
        variable="temperature_high",
        value=Decimal("80.5"),
        unit="F",
        issue_time=now,
        valid_time=now,
        fetched_at=now,
        market_id="m1",
        location="New York",
        station="KNYC",
    )

    analysis = analyze_global_bucket_price("m1", rule, forecast, Decimal("0.08"), now=now)

    assert analysis.decision == "trade"
    assert analysis.side == "buy_yes"
```

- [ ] **Step 2: Verify red**

Run: `uv run pytest tests/test_global_temperature_bucket.py -q`

Expected: FAIL because modules do not exist.

- [ ] **Step 3: Implement parser**

Parser returns `GlobalTemperatureBucketRule` with:

- `raw_text`
- `location`
- `station`
- `source`
- `variable`
- `bucket_center`
- `bucket_lower`
- `bucket_upper`
- `unit`
- `target_date`
- `settlement_timezone`
- `confidence`
- `tradable`
- `rejection_reason`

Only accept high-temperature, one location or station, explicit date, source, and 1 degree bucket.

- [ ] **Step 4: Implement pricing**

Use the same normal-CDF structure as China bucket pricing, but choose sigma by unit:

- Fahrenheit sigma: `1.2F` within 24h, `1.8F` within 48h, `2.8F` within 120h, `3.6F` beyond.
- Celsius sigma: reuse China sigma values.

Model version: `global-temp-bucket-normal-v1`.

- [ ] **Step 5: Verify green**

Run: `uv run pytest tests/test_global_temperature_bucket.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/polymarket_weather_arb/domain/global_temperature_bucket.py src/polymarket_weather_arb/domain/global_bucket_pricing.py tests/test_global_temperature_bucket.py
git commit -m "Add global temperature bucket parser"
```

### Task 4: Wire Global Temperature Buckets Into Workflow

**Files:**
- Modify: `src/polymarket_weather_arb/services/market_workflow_service.py`
- Modify: `src/polymarket_weather_arb/storage/repositories.py`
- Test: `tests/test_market_workflow_service.py`

- [ ] **Step 1: Write failing workflow test**

Create a market with `module_id="global_temp_bucket"`, a quote snapshot, and a parsable global bucket title. Assert `research_market()` saves a temperature bucket rule with `module_id="global_temp_bucket"` and saves an analysis.

- [ ] **Step 2: Verify red**

Run: `uv run pytest tests/test_market_workflow_service.py::test_research_global_temp_bucket_market_saves_rule_and_analysis -q`

Expected: FAIL because workflow does not route this module.

- [ ] **Step 3: Implement workflow route**

Add `_global_rule`, `_analyze_global_bucket_market`, and use `repository.save_temperature_bucket_rule(..., module_id="global_temp_bucket")`.

- [ ] **Step 4: Verify green**

Run: `uv run pytest tests/test_market_workflow_service.py::test_research_global_temp_bucket_market_saves_rule_and_analysis -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/polymarket_weather_arb/services/market_workflow_service.py tests/test_market_workflow_service.py
git commit -m "Wire global temperature bucket workflow"
```

### Task 5: Add Precip / Snow Module Credibility Routing

**Files:**
- Modify: `src/polymarket_weather_arb/services/discovery_service.py`
- Modify: `src/polymarket_weather_arb/services/market_workflow_service.py`
- Test: `tests/test_precip_snow_module.py`

- [ ] **Step 1: Write tests**

Assert rainfall and snowfall threshold markets can be inspected as `precip_snow`, save a `ResolutionRule`, and are marked dry-run only in credibility.

- [ ] **Step 2: Implement conservative routing**

Do not invent a new precipitation model. Route through existing `AnalysisService` and `estimate_probability_interval`, but set module id and credibility.

- [ ] **Step 3: Verify**

Run: `uv run pytest tests/test_precip_snow_module.py -q`

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/polymarket_weather_arb/services/discovery_service.py src/polymarket_weather_arb/services/market_workflow_service.py tests/test_precip_snow_module.py
git commit -m "Route precipitation and snow module"
```

### Task 6: Add Hurricane / Storm Research-Only Classification

**Files:**
- Create: `src/polymarket_weather_arb/domain/hurricane_storm.py`
- Test: `tests/test_hurricane_storm_module.py`

- [ ] **Step 1: Write tests**

Assert NHC/NOAA storm market text is classified as research-only and not tradable.

- [ ] **Step 2: Implement classifier**

Return a plain dataclass with `tradable=False`, `research_only=True`, and reasons such as `storm pricing requires dedicated NHC adapter`.

- [ ] **Step 3: Verify**

Run: `uv run pytest tests/test_hurricane_storm_module.py -q`

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/polymarket_weather_arb/domain/hurricane_storm.py tests/test_hurricane_storm_module.py
git commit -m "Add storm research classifier"
```

### Task 7: Display Credibility In Dashboard And Launchpad

**Files:**
- Modify: `src/polymarket_weather_arb/services/live_launchpad_service.py`
- Modify: `src/polymarket_weather_arb/dashboard_ui/live.py`
- Modify: `src/polymarket_weather_arb/dashboard_ui/markets.py`
- Test: `tests/test_live_launchpad_service.py`
- Test: `tests/test_dashboard_market_workflow.py`

- [ ] **Step 1: Write tests**

Assert a new module candidate shows `dry_run_only` and cannot preview live. Assert dashboard includes credibility labels.

- [ ] **Step 2: Implement credibility fields**

Add credibility snapshot into candidate DTOs and render source grade, rule status, and live eligibility.

- [ ] **Step 3: Verify**

Run: `uv run pytest tests/test_live_launchpad_service.py tests/test_dashboard_market_workflow.py -q`

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/polymarket_weather_arb/services/live_launchpad_service.py src/polymarket_weather_arb/dashboard_ui/live.py src/polymarket_weather_arb/dashboard_ui/markets.py tests/test_live_launchpad_service.py tests/test_dashboard_market_workflow.py
git commit -m "Show module credibility in operator UI"
```

### Task 8: Full Verification

**Files:**
- No new files.

- [ ] **Step 1: Run all tests**

Run: `uv run pytest -q`

Expected: all tests pass.

- [ ] **Step 2: Run ruff**

Run: `uv run ruff check src/ tests/ scripts/rehearse_live_readiness.py scripts/backup_restore_check.py scripts/check_deployment_files.py`

Expected: `All checks passed!`

- [ ] **Step 3: Push**

```bash
git push origin main
```

