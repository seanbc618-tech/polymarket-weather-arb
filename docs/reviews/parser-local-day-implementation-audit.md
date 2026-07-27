# Parser + Local-Day Implementation Audit (Omissions & Gaps)

**Date**: 2026-06-13 (follow-up)  
**Auditor**: Grok (read-only)  
**Scope**: Read-only review of the *current on-disk implementation* of parser improvements + NWS local-day query window handling. No code changes. Focus on whether the combined changes fully address (or leave gaps relative to) the recommendations and risks identified in:
- docs/reviews/rule-operator-backfill-audit.md
- docs/reviews/nws-local-day-query-window-audit.md
- Earlier settlement-observation-audit.md

**Key files inspected (current state)**:
- src/polymarket_weather_arb/domain/rules.py (parser / _extract_threshold_event, qualifier, _extract_operator)
- src/polymarket_weather_arb/adapters/weather/noaa.py (fetch_observation local-day logic, _resolve_timezone, ZoneInfo usage, raw_payload, warnings)
- src/polymarket_weather_arb/noaa_station_mapping.json (tz fields added)
- src/polymarket_weather_arb/services/settlement_service.py (preview/backfill result warnings propagation)
- CLI (settlement-backfill --preview) and dashboard calibration preview warnings
- tests/test_noaa_provider.py (new local-day specific tests)

**Goal of this audit**: Identify *omissions, residual risks, incomplete coverage, and interactions* between the parser work and the local-day work that may still affect settlement observation backfill correctness.

## Executive Summary

The recent implementation has made **substantial, targeted progress on the local-day / query window problem** (the #1 High Risk from the nws-local-day audit and settlement-observation audit):

- Mapping.json now carries explicit IANA `"timezone"` for every supported station.
- `_resolve_timezone` + ZoneInfo logic in `fetch_observation` computes proper local 00:00–23:59:59 for the target_date, converts the bounds to UTC, and queries the NWS API with the shifted window.
- Rich audit fields (`timezone`, `local_start`, `local_end`, `query_start`, `query_end`) + existing full `observations` list + `warnings` are persisted in raw_payload.
- Fallback to old UTC calendar behavior + explicit warning when tz is unknown in mapping.
- Warnings (including the tz fallback) are now propagated through `SettlementPreviewResult` / `SettlementBackfillResult`, printed by CLI (`settlement-backfill --preview` and normal backfill), and surfaced in the dashboard calibration preview section.
- Several new targeted tests were added for KNYC/KLAX local windows, fallback, raw_payload fields, and high/low selection under the new window.

The **parser side** received only a modest incremental improvement (named capture groups in the temperature regex + qualifier-or-prefix logic for high/low detection). The deeper semantic and "tradable optimism" issues raised in the dedicated rule-operator-backfill-audit remain largely unaddressed.

**Overall verdict**: Local-day alignment is now correctly implemented for stations that have a `timezone` entry (most of the current mapping). This is a high-quality, low-over-engineering realization of the "compute local bounds then query" option recommended in the nws-local-day audit.

However, several **omissions and residual risks** remain when viewing "parser + local-day" as a combined system for trustworthy observation → YES/NO backfill:

- Parser semantic gaps (operator collapse, lack of ambiguity signals) can still feed a bad rule into an otherwise correctly windowed observation query.
- The "max/min of returned samples inside the (now correct) local window" is still not the official NWS 5-min moving-average daily extremum (original High Risk from settlement-observation audit).
- Test coverage for the new local-day path exists but does not stress DST transitions, unknown-tz runtime safety, or parser-induced wrong-variable + correct-window interactions.
- Minor robustness issues (bad tz string in mapping, zoneinfo import, boundary inclusivity).
- No combined "rule quality" warning surfaced alongside tz warnings for backfill users.

The implementation is **much safer than before** for local-day correctness, but it is not yet a complete "set and forget" foundation for high-confidence calibration data from NWS observations.

## What Was Implemented vs. Prior Recommendations

**Local-day (nws-local-day audit + settlement-observation High Risk #1)** — largely addressed:
- tz added to mapping + lookup helper.
- Proper local-day UTC query bounds using zoneinfo (instead of fixed 00Z-23Z).
- Fallback + warning (exactly as suggested for the "warning-only first" or graceful path).
- Audit fields in raw_payload + warnings tuple on results (CLI + dashboard integration).
- New tests exercising the tz path and fallback.

**Parser (rule-operator-backfill audit recommendations)** — only lightly touched:
- Regex modernized with named groups (`qualifier`, `threshold`, `unit`).
- High/low detection now prefers explicit qualifier group (small robustness win).
- Everything else ( `_extract_operator` still forces `<=` / `>=` only; no new rejections for "exceed"/"above" ambiguity; tradable still purely structural + 0.75 heuristic; no operator snippet provenance) is unchanged from the state critiqued in the prior parser audit.

**Cross-cutting**:
- Warnings are now first-class citizens for the obs path (good).
- Preview remains non-mutating and now shows more useful info.

## Concrete Omissions & Gaps

### 1. Parser semantics still lag (major interaction risk with the new local-day window)
- `_extract_operator` (rules.py:154-160) and the calling sites in `_extract_threshold_event` continue to map every "greater-ish" token (including bare `>`) to `>=` and every "less-ish" (including `<`) to `<=`. The regex now *captures* the strict symbols via the old pattern, but they are ignored for the operator value.
- No new rejection_reason or lowered confidence when the operator came from "exceed" / "above" / "over" / "below" (the exact cases called out as fragile in the rule-operator audit).
- High/low qualifier improvement helps, but the 20-char prefix fallback + qualifier logic can still be fooled by unusual title phrasing; a mis-detected `temperature_low` will now get a *correctly bounded local-day window* but will still fetch/return the wrong extremum type.
- Result: a perfectly aligned local-day observation can still be compared with `_matches_rule` using a semantically wrong operator or variable that originated in the parser.

### 2. "Sample max inside correct window" ≠ official daily high (residual from first audit)
Even with perfect local-day query bounds, the code does:
```python
selected = max(observations, key=...)   # or min
```
The NWS official daily maximum for settlement (per ASOS climatology reports and the research cited in the nws-local-day audit) is typically the max of 5-minute moving averages over the local day, not the max of whatever observation records the /observations endpoint happened to return for the time range.

The local-day fix eliminates the *day misalignment* error but leaves the *reduction method* error intact. This was explicitly listed as a separate High Risk in the original settlement-observation-audit and is not mitigated by the current changes.

### 3. Test coverage for the new logic is present but narrow
New tests were added (e.g. `test_noaa_fetch_observation_knyc_local_day_window`, `klax...`, fallback, raw_payload fields, high/low still correct).

Gaps:
- The mocks in the new tests still tend to place all observation timestamps comfortably inside a single calendar day. They do not appear to include a case where an observation timestamp's *parsed local time* would fall outside the target local day (to verify that the query bounds are what actually protected correctness).
- No DST transition day test (spring forward / fall back on the target_date). `time.min` / `time.max` + ZoneInfo on a fold/ambiguous day can produce surprising instants; this is a realistic edge for real market dates.
- No test that a mapping entry with a *bad* timezone string causes a clear, handled failure (or at least a documented exception) rather than an uncaught ZoneInfoNotFoundError deep in the observation path.
- No combined parser + local-day test: e.g. a rule parsed from a title that triggers the old high/low heuristic bug, fed to a tz-aware station, asserting that the warning or the selected value makes the problem visible.
- Parser-only tests (test_rules.py) appear unchanged — still no "below/under" parametrized cases asserting `<=`, no ambiguous-operator tests, etc.

### 4. Robustness / runtime safety small misses
- `from zoneinfo import ZoneInfo` is now a top-level import in noaa.py. Any environment on Python < 3.9 (without the backport package) will fail to import the whole module even if it only ever calls forecast paths.
- In the success path: `datetime.fromisoformat(target_date).date()` — if `window_start` ever contains a time component or is malformed, this can raise. Not wrapped.
- `ZoneInfo(tz_name)` from a value that exists in the JSON but is not a valid IANA name will raise at observation time (no validation at mapping load).
- The end of the local day uses `time.max` (23:59:59.999999). The resulting UTC instant is sent as the `end` param. If the NWS API treats the end bound as exclusive in some cases, the very last observations of the local day could theoretically be excluded on certain days. Minor, but worth a comment or microsecond adjustment.
- No normalization / validation that the timestamps returned by the API, once interpreted in the station tz, actually have local date == target_date. The code trusts the query bounds completely.

### 5. Incomplete provenance & combined warnings for users of backfill
- The rich `local_*` / `timezone` / `warnings` are in the raw_payload and in the Python result objects. CLI prints warnings; dashboard shows preview warnings.
- However, there is no single "rule_quality" or "parser_ambiguity" warning that gets merged with the tz warning. A user doing a backfill sees tz-related messages but nothing that says "this rule was parsed with operator derived only from the word 'exceed' and confidence 0.78".
- The parser still does not store (or return) the matched text snippet that drove the operator / variable decision (a recommendation from the rule audit). This would be especially valuable now that the *window* for the observation is trustworthy — the remaining uncertainty is often in the rule itself.

### 6. Mapping data quality / maintenance
- tz values look correct for the listed stations (America/New_York for eastern cities including Miami, America/Los_Angeles, America/Chicago, America/Denver, America/Phoenix).
- One small oddity: "last_updated" in the JSON header was not bumped, and some notes are unchanged.
- If a new station/city is added in the future without a "timezone" key, it will silently hit the fallback warning path. No load-time validation or required-field check.

### 7. Scope / blast radius notes
- The local-day logic is isolated to `fetch_observation` (temp high/low only). Forecast path and other providers are untouched — correct.
- The changes are visible to preview (non-mutating) — good for the "blind backfill" concern from earlier audits.

## Residual Risks (ranked)

1. **Parser-induced wrong rule fed into correct window** (high, because the two pieces are now asymmetric in quality).
2. **Reduction method (max of samples) still not official daily summary** (high, original risk not touched by this work).
3. **DST / boundary / malformed input edge cases** (medium; low probability but would produce wrong settlement data silently except for existing low-coverage warning).
4. **zoneinfo import & bad-tz-in-mapping runtime crashes** (medium; affects module load or first obs call).
5. **Insufficient stress in the new tests for the exact scenarios the local-day fix was meant to solve** (medium; the existence of tests is good, but their narrowness is an omission).

## Recommended Polish / Next Minimal Slice (read-only suggestions)

- Add a couple of high-value tests: one with a DST-transition target_date for an eastern station, one that exercises a mapping entry whose tz string would be invalid, and one that combines a "questionable parser rule" (e.g. "exceed" title) with a tz-aware observation and asserts that both the rule-derived warning (if added) *and* the obs result are visible together.
- In `_resolve_timezone` or at mapping load time, validate that any present "timezone" value can be turned into a ZoneInfo (or at least document that bad values will explode later).
- Consider a tiny helper `_safe_local_day_bounds(target_date, tz_name)` that never raises and always returns a (bounds, warnings) tuple so the main path stays defensive.
- Surface a combined "effective_rule_quality" or at least the original `rule.confidence` + `rejection_reason` (if any) into the preview/backfill result alongside the obs warnings. This would make the parser + local-day system feel coherent to operators.
- (Future, lower priority) Re-visit the "official daily max" question using the station's own daily summary data or by also requesting the `temperature` `max` / `min` 24h fields when the API provides them, as a cross-check against the max-of-samples value.

Do the smallest defensive tests + error handling first; the core local-day correctness win is already in place.

## Do Not Change / Safety Invariants

- Keep the current "if tz present → proper local bounds, else UTC + warning" structure. It is exactly the graceful degradation recommended in the nws-local-day audit.
- Do not remove the full `observations` list or the `local_*` / `query_*` fields from raw_payload — these are the forensic record that lets someone later verify "we really did query the correct local day and these were the samples".
- Warnings must continue to flow to CLI, results, and the dashboard preview UI.
- The local-day logic must remain isolated to observation fetching for temperature high/low. Do not accidentally apply it to forecast period selection.
- Parser changes (if any future work) must not start emitting strict `>` / `<` without a coordinated decision and updates to `_matches_rule`, probability, ensemble, etc. (per previous parser audit).
- Preview must remain side-effect-free (already true).
- Any future parser robustness work should still respect the `tradable` gate before any backfill/preview is allowed to call the provider.

This implementation successfully closed the most dangerous local-day alignment bug while adding excellent auditability. The remaining omissions are mostly in the parser half (which was only lightly updated) and in the "samples vs official daily summary" reduction method (pre-existing and out of scope for the local-day slice). The system is now materially safer for NWS-backed settlement observation backfill, provided users pay attention to the surfaced warnings and understand that parser-derived rules are still heuristic.

All analysis is based on direct file reads and greps of the current workspace. No files were modified. Only this review document will be added.