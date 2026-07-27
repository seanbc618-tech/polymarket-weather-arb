# Settlement Observation Backfill Audit

**Date**: 2026-06-13  
**Auditor**: Grok (read-only review)  
**Scope**: Audit only — no code, tests, or git changes.  
**Key files reviewed**:
- src/polymarket_weather_arb/adapters/weather/noaa.py (fetch_observation)
- src/polymarket_weather_arb/services/settlement_service.py (backfill_market + _matches_rule)
- src/polymarket_weather_arb/storage/repositories.py (save_observation, settle_model_signals_for_market, list_recent_observations)
- src/polymarket_weather_arb/dashboard.py (handle /calibration/backfill)
- src/polymarket_weather_arb/dashboard_ui/calibration.py (render + forms)
- tests/test_noaa_provider.py
- tests/test_settlement_service.py
- tests/test_dashboard_calibration.py
- Supporting: cli.py (settlement-backfill cmd + _forecast_source_grade), domain/rules.py (parse + operators), domain/weather.py (normalize + WeatherObservation), storage/db.py (weather_observations + model_signals schema), services/trading_service.py + module_credibility_service.py + modules/weather.py (live gates), test_cli_operator.py (CLI coverage), dashboard_ui/i18n.py (strings)

## Executive Summary

The recently added settlement observation backfill path (NoaaProvider.fetch_observation, SettlementService.backfill_market, `uv run polymarket-weather settlement-backfill --market <id>`, dashboard `/calibration` "Official Observation Backfill" button, and "Recent Observations" table) lets the system pull real NWS station temperature observations for a market's window, derive a YES/NO outcome by applying the stored ResolutionRule, persist a WeatherObservation (with partial raw payload), and stamp historical model_signals as resolved for calibration scoring (Brier, hit rate).

It is a calibration/research tool, not a trading input path. Observations are stored separately from forecasts; settlement only updates `outcome_status`/`resolved_outcome`/`settlement_*` on existing signals (see repositories.py:411-439). No forecasts are created or mutated.

**Core findings**:
- NWS observation usage has material hazards around date alignment (UTC slice vs local station day), reduction method (max/min of returned samples vs official daily high/low), and qualityControl handling (captured but never filtered).
- ResolutionRule → YES/NO mapping (`_matches_rule`) follows parse operators, but parse heuristics for `below`/`under` vs `at least`/`above` are keyword-proximity based and can mismatch actual market resolution text.
- Data safety: raw payload is only a curated subset (no full response), and both CLI and dashboard backfill are fully blind (one market_id field + immediate live API call + mutate).
- Live safety: **no direct loosening** of trading gates. `source_grade` on signals comes from the *forecast* raw_payload at analysis time (cli.py:480-494, repositories.py:316-321). Obs backfill writes `settlement_observation` (different token) and never touches forecast grades or `requires_settlement_grade` paths. `research_grade` forecasts remain blocked for live (trading_service.py:42, module_credibility_service.py:159).
- Test coverage is thin on the new hot path; high-risk cases (QC, TZ boundaries, operator edge cases, blind failure modes, parse drift) are untested.

The feature is usable for local research today but should not be treated as authoritative settlement ground truth or used to "bless" historical calibration data without preview/audit steps.

## Blockers

None that are absolute (the path is explicitly calibration-only and requires a pre-existing tradable rule; live trading gates are orthogonal). However, the High Risk items below constitute effective blockers for any production or shared use of the backfill results.

## High Risk

1. **UTC date window vs market local station day (noaa.py:201-203, 246)**  
   ```python
   target_date = (rule.window_start or ... )[:10]
   start = f'{target_date}T00:00:00Z'
   end = f'{target_date}T23:59:59Z'
   ... /stations/{station}/observations?start=...&end=...
   observed_at = _parse_datetime(...)
   ```
   `window_start` (from parse_resolution_rule or stored rule) is a bare YYYY-MM-DD (often taken from market title). The code forces a strict UTC calendar-day range. For US NWS stations (KNYC etc.) the local "high temperature on 2026-06-03" typically spans ~04:00Z prior day to ~04:00Z next day (EDT). The max temperature sample inside the UTC slice can therefore come from the wrong local day or truncate the true local max. No tz handling, no station offset lookup (despite noaa_station_mapping.json), and no cross-check against NWS "daily" or climate summary products. This is the most direct threat to correctness of derived YES/NO.

2. **High/low via simple max/min over hourly observations, not station day summary (noaa.py:235-242)**  
   ```python
   if variable == 'temperature_high':
       ... = max(observations, key=lambda item: item[0])
   else:
       ... = min(...)
   ```
   The /observations endpoint returns a time series of (typically hourly or 20-min) samples. Official NWS "daily high temperature" for settlement is frequently defined via a specific summary statistic, the max of 5-min observations, or a published daily value — not the max of whatever samples happened to be returned for a UTC range. Incomplete coverage (night-only returns for a "high", sensor gaps) silently produces wrong values. No warning when observation_count is low.

3. **qualityControl ignored for filtering (noaa.py:229, 235-242, 270)**  
   `quality_status = properties.get('qualityControl')` is captured for the *selected* observation and stored in DB + raw_payload. It is **never used to skip bad samples** (`'Z'`, `'X'`, `None`, coarse values etc.). All rows with non-null `temperature.value` participate in the max/min. A backfill can therefore derive a YES/NO from demonstrably bad sensor data and still mark signals resolved. Recent observations table (calibration.py:157-184) surfaces the QC but the user must notice manually.

4. **Completely blind one-click backfill (dashboard.py:566-576, dashboard_ui/calibration.py:88-99, cli.py:225-244, settlement_service.py:35-56)**  
   - Dashboard form: only `<input name="market_id">` + "Fetch NWS observation" button. POST `/calibration/backfill` immediately constructs `NoaaProvider()`, calls `backfill_market`, commits.
   - CLI: `settlement-backfill --market <id>` — same, no flags for preview/dry-run/confirm.
   - No display of the rule that will be used, the target_date, the raw observation samples, the selected value+QC, or the *inferred* resolved_outcome before mutation.
   - No equivalent of the live "LIVE 2 USDC" confirmation pattern.
   - A typo, a rule that was saved with drifted parse, or a station that returns sparse data permanently corrupts the model_signals history for that market (used by CalibrationService for Brier/hit-rate).

5. **Insufficient raw payload for traceability (noaa.py:258-272, repositories.py:233-253)**  
   Only a hand-curated dict is saved:
   ```python
   raw_payload = {
       'source': 'nws-observation', 'station':..., 'target_date':...,
       'source_grade': 'settlement_observation', 'official_signal': True, 'settlement_source': True,
       'observation_count': len(observations),
       'selected_observation': {'timestamp':..., 'value':..., 'unit':..., 'quality_status':...}
   }
   ```
   The full `response.json()` (all features, their full properties, any pagination metadata) is discarded after the in-memory max/min. Future re-audit or "what if we had filtered QC" is impossible from the DB alone. Contrast with market_snapshots/forecasts which store richer raw.

## Medium Risk

- **Observation API pagination / completeness contract (noaa.py:218-219)**: `features = payload.get('features', [])`. No handling of `@iot.nextLink`, `x-` headers, or `limit` behavior on the NWS observations history endpoint. For a single day this is usually <50 entries, but the code assumes the response contains everything needed for the day max. Silent truncation = silent wrong high/low.

- **Operator and "below" direction logic fragility (domain/rules.py:153-159, _extract_threshold_event, settlement_service.py:96-108)**:
  ```python
  if any(token in window for token in ('below', 'under', 'less than', '<=', '<')): return '<='
  ...
  # then _matches_rule does literal <= etc.
  ```
  The heuristic window is 50 chars before + 80 after the number. Titles like "Will the high stay under 85 or drop below?" or double-negation or "exceed on the cool side" can mis-set `>=` vs `<=`. `parse_resolution_rule` rejects only when no operator is found at all; once a rule is `tradable=True` the backfill trusts the operator blindly. `>` vs `>=` and `<` vs `<=` are accepted by _matches_rule but rarely distinguished in market text. Unit extraction can also drift (text "80F" vs actual settlement spec).

- **Hardcoded NOAA provider + station mapping assumptions (cli.py:233, dashboard.py:573, noaa.py:55-71, 200)**: Backfill always does `SettlementService(repository, NoaaProvider())`. Non-US markets or markets whose rule source is "NWS" but station not in mapping will raise at resolve time or pick a wrong K*** station. China/official markets have no observation backfill path at all.

- **Manual settle vs backfill provenance (dashboard.py:551-565 vs 566-576, calibration.py)**: Both paths end up calling `settle_model_signals_for_market` (or the service). Nothing distinguishes "human typed 83" from "NWS obs 86.0 QC=V inferred yes". Calibration scores treat them identically.

- **Validate rule is only called on backfill, not previewable (settlement_service.py:84-93)**: `_validate_rule` requires `tradable`, variable, threshold, operator, unit. These come from either a prior `resolution_rules` row or a fresh `parse_resolution_rule` + `save`. If the market description changed or parse is now stricter, a previously-settled market can suddenly become un-backfillable.

## Low Risk

- **No indirect relaxation of live trading gates**. 
  - TradingService.trade rejects `source_grade != 'settlement_grade'` for `!dry_run` (trading_service.py:42-47).
  - `module_credibility` and live launchpad require `source_grade == "settlement_grade"` (module_credibility_service.py:159, live_launchpad_service.py).
  - `_forecast_source_grade` (cli.py:490-494) only inspects forecast `raw_payload` for `settlement_grade` or `official_signal`.
  - Observations write `source_grade: 'settlement_observation'` (different string) + `settlement_source: True`. This value is never read by the live decision paths.
  - `research_grade` (ensemble, open-meteo, etc.) continues to be blocked for live exactly as before (see handoff doc slice 4 and current module weather.py:14 `requires_settlement_grade=True`).
  - Backfill never creates analyses or forecasts.

- DB schema is sound for the feature: `weather_observations.raw_payload NOT NULL`, `quality_status`, `observed_at`, FK to markets (db.py:141-154). `model_signals` has the settlement columns (171-194).

- Backfill only supports `temperature_high`/`temperature_low` (noaa.py:197-198); attempts on precip/snow raise before any network call. Matches the current provider limitation.

- Recent observations table and "Official Observation Backfill" copy (i18n + calibration.py) make the intent visible to dashboard users.

## Recommended Next Implementation Slice

(Ordered by risk reduction.)

1. **Preview / dry-run / confirm for backfill** (highest impact, low code diff)
   - Add `dry_run: bool = False` (or separate `preview_backfill`) to SettlementService.
   - Return a richer result including: the rule used, the full list of candidate obs (or at least count + min/max + selected with QC), the would-be resolved_outcome, and any quality warnings.
   - CLI: `settlement-backfill --market M --dry-run` prints the above and does not write.
   - Dashboard: two-stage — "Preview NWS observation" button (POST that returns a confirmation page with the details + hidden fields) then explicit "Confirm & Backfill" (or reuse the existing confirmation pattern like live actions).
   - This directly mitigates the blind-mutation high risk.

2. **Correct day bounds + QC policy for NWS observations**
   - Use station tz (or US default) or lookup from mapping to expand the query window to the local calendar day.
   - Decide and document a QC allow-list (e.g. only proceed if selected has 'V', or surface a hard warning/block when QC is poor).
   - Consider also storing the complete API payload (or at least all feature timestamps+values+qc) for the window so that alternative reductions (e.g. "max of all, even bad QC") can be re-run later.

3. **Make raw_payload faithful**
   - At minimum: include the full `features` list (or the original response minus huge fields) under a `response` key, plus any pagination metadata.

4. **Strengthen rule/operator provenance**
   - When saving resolution_rule or during backfill, also persist the exact matched text snippet that drove the operator choice.
   - Expose it on the calibration page next to recent signals/observations.

5. **Broader provider support + guardrails**
   - Make the dashboard/CLI settlement-backfill accept (or infer) a provider; keep NOAA default for US temp markets.
   - Add a `backfill_supported` flag to modules / ObservationProvider.
   - In SettlementService._validate_rule also cross-check that the rule's source/location is compatible with the chosen provider.

6. **Calibration hygiene**
   - Distinguish settlement_source provenance (e.g. prefix "backfill:nws-..." vs manual).
   - Add a "revert settlement" helper (or at least an audit event) for the rare case a bad backfill is discovered.

Do **not** relax any live gates or allow backfill results to flow into trading decisions.

## Exact Tests To Add

Current coverage (from the three primary test files + cli operator test):
- Happy path: 3 observations → max converted to F, QC captured, raw has settlement_observation + count + selected, signal settled to yes (test_noaa_provider, test_settlement_service, test_dashboard_calibration).
- One "under threshold → yes for <=" case via fake provider.
- Dashboard render contains the backfill section and recent obs table.
- CLI and POST backfill paths can be monkey-patched and produce the expected flash/stdout.
- Forecast side of NOAA still has good settlement_grade coverage.

**High-priority missing cases** (add to existing files first; keep using pytest-httpx style mocks):

**tests/test_noaa_provider.py** (add to existing @patch('httpx.Client') tests):
- `test_noaa_fetch_observation_filters_or_warns_on_bad_qc` (QC='Z' or None present; either filtered out or selected QC is poor and raw reflects it).
- `test_noaa_fetch_observation_unit_conversion_c_to_f_rule` (NWS returns degC, rule.unit='F' → normalized value correct).
- `test_noaa_fetch_observation_respects_station_local_day_bounds` (mock observations with timestamps that cross UTC midnight; assert the selected high belongs to the intended local calendar day once tz logic is added — or document current behavior).
- `test_noaa_fetch_observation_raises_on_unknown_station` (no mapping hit).
- `test_noaa_fetch_observation_low_picks_min_and_its_qc` (symmetric to the high test).
- `test_noaa_fetch_observation_includes_pagination_hint_in_raw_if_present` (or at least does not crash on extra keys).
- Negative: empty features after filter → current "no usable" error (already partially covered).

**tests/test_settlement_service.py**:
- `test_backfill_loads_rule_via_parse_when_no_resolution_rule_row` (seed only market, call backfill, assert rule was parsed and saved).
- `test_backfill_rejects_non_tradable_rule` (rejection_reason present or tradable=False).
- Parametrized operator matrix:
  ```python
  @pytest.mark.parametrize("op,obs,thresh,expected", [
      (">", Decimal("80"), Decimal("80"), False),
      (">=", Decimal("80"), Decimal("80"), True),
      ("<", Decimal("79.9"), Decimal("80"), True),
      ("<=", Decimal("80"), Decimal("80"), True),
  ])
  ```
- `test_backfill_saves_full_observation_row_with_raw_and_quality` (assert DB row has the values from provider + raw_payload contains observation_count and selected_observation['quality_status']).
- `test_backfill_propagates_provider_error` (fake provider raises → backfill raises, no partial settle).

**tests/test_dashboard_calibration.py**:
- `test_calibration_backfill_post_rejects_unknown_market_or_unparseable_rule` (error flash, no signals touched).
- `test_calibration_backfill_post_records_settlement_source_from_observation` (vs manual settle).
- If preview added: test that preview path does not commit and renders the would-be outcome.
- `test_calibration_page_shows_backfill_form_and_recent_observations_with_quality` (already asserted in render test; make the QC/observed value assertions stricter).

**tests/test_cli_operator.py** (or a dedicated settlement CLI test):
- End-to-end `settlement-backfill --market m1` with a real-ish fake that exercises the commit path (already has a smoke test; expand to assert DB side effects using tmp DB).

**tests/test_rules.py** (operator direction):
- Add or extend parametrization:
  - titles containing "below", "under", "less than" → operator == '<='
  - "at least", "above", "exceed", "greater than" → '>=' (current clear rule test is one example)
- Title that produces '<' or '>' (if parse ever emits strict) and confirm _matches_rule in settlement accepts it.

**Additional cross-cutting**:
- A single integration-style test (perhaps in test_settlement_service) that uses `@patch('httpx.Client')` on the real NoaaProvider inside SettlementService.backfill_market and verifies the end-to-end: observation written, signal resolved_outcome set, raw_payload on observation contains 'settlement_observation'.
- Add a test that backfill on a precip/snow rule immediately raises (provider limitation) before any HTTP.

Run the full suite after additions: `uv run pytest tests/test_noaa_provider.py tests/test_settlement_service.py tests/test_dashboard_calibration.py tests/test_cli_operator.py tests/test_rules.py -q`.

This audit is complete and read-only. All analysis derived from the listed files and supporting greps/reads. No production artifacts were modified.