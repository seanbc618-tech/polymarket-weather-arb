# NWS Observation Local-Day / Query Window / Data Completeness Policy Audit

**Date**: 2026-06-13  
**Auditor**: Grok (read-only review)  
**Scope**: Strictly read-only audit. No modifications to any src/, tests/, or other files. Only the mandated new document is created. No git commits.  
**Primary files reviewed** (per task):
- src/polymarket_weather_arb/adapters/weather/noaa.py (fetch_observation window logic + raw_payload + warnings)
- src/polymarket_weather_arb/noaa_station_mapping.json (station metadata)
- docs/reviews/settlement-observation-audit.md (prior broad audit)
- docs/reviews/rule-operator-backfill-audit.md (prior rule semantics audit)
- tests/test_noaa_provider.py (observation test mocks and date handling)

**Audit questions** (directly addressed):
1. Current UTC query window risks for KNYC/KLAX/KORD etc.
2. Does noaa_station_mapping.json contain timezone or derivable-tz fields?
3. Minimal safe implementation options (tz mapping, IANA, ±12h + local filter, warning-only first)?
4. NWS /observations API pagination or sample gaps? Is current compact raw_payload sufficient for re-review?
5. Next minimal implementation slice (avoid over-engineering)?

## Executive Summary

The `NoaaProvider.fetch_observation` path (used by `SettlementService.preview_market` / `backfill_market`, the CLI `settlement-backfill`, and the dashboard `/calibration/backfill` button) computes the NWS historical observation query window exclusively from `rule.window_start` (a bare YYYY-MM-DD, usually from market title) as a rigid UTC calendar day:

```python
# noaa.py:201-203
target_date = (rule.window_start or datetime.now(timezone.utc).date().isoformat())[:10]
start = f'{target_date}T00:00:00Z'
end = f'{target_date}T23:59:59Z'
... GET /stations/{station}/observations?start=...&end=...
```

All returned temperature-bearing features in that UTC slice are collected; the max (high) or min (low) is selected; the result (plus a compact list of *all* usable observations, the exact query bounds, and a few warnings) is placed in `raw_payload` and used to drive `_matches_rule(...)` for a definitive YES/NO that settles historical `model_signals`.

This is **not** the same as the station's local calendar day. Official NWS/ASOS "daily high temperature" for settlement purposes is defined over 00:00–23:59 **local time** at the station (5-min moving averages, etc.). Prior audits (settlement-observation-audit.md High Risk #1 and referenced in rule-operator-backfill-audit.md) already identified this as the top correctness threat for backfill-derived calibration ground truth.

**Current state (improvements since first audit)**:
- Mapping still contains zero timezone information (only city/aliases/station/coords/gridpoint/notes).
- Code now stores `query_start`, `query_end`, a full `observations` compact list of every usable record (not just the selected one), and `warnings` for low coverage (<12) or non-'V' QC (noaa.py:279-293, 268-274).
- Observation tests use KNYC + 2026-06-03 window + UTC timestamps inside that slice; no crossing-boundary or tz-aware cases.

**Risk level**: High for any market whose local-day extrema occur near the UTC day edges (common for US eastern/western stations). A mis-selected max/min directly flips the backfilled `resolved_outcome`, poisoning Brier/hit-rate scores used for model trust decisions. The improved raw_payload makes post-facto detection possible but does not prevent the wrong data from being written.

**Minimal safe next slice recommendation**: 
1. Augment the mapping.json (non-code change) with an explicit `"timezone": "America/New_York"` (IANA) per entry for the ~dozen supported cities/stations.
2. In a future tiny slice, either (a) compute proper local-day UTC bounds using zoneinfo + the tz, or (b) query a safe widened window (target-1 12Z to target+1 12Z) and filter features by converting their `timestamp` to the station's local date, or (c) warning-only mode that always emits a clear audit warning and stores the assumed UTC window.
3. Keep (and expand) the existing warnings + full observations list in raw_payload.
4. Add targeted tests with boundary-crossing timestamps for at least KNYC/KLAX.

Do **not** over-engineer (no new deps, no full daily-summary products yet, no automatic "fix the rule" logic).

## Current Behavior

**Query construction (noaa.py:195-214)**:
- Only supports `temperature_high` / `temperature_low` (else ValueError).
- `station = _resolve_station(...)` (uses mapping by city/alias or explicit Kxxx source/station; no tz consulted).
- `target_date` taken verbatim from `rule.window_start` (or today) as YYYY-MM-DD.
- Hard UTC slice: `T00:00:00Z` to `T23:59:59Z`.
- `GET https://api.weather.gov/stations/{station}/observations` with `params={'start':..., 'end':...}` (User-Agent + geo+json).
- Response parsed as GeoJSON `features`; each `properties.temperature.value` (if present) + unit + `timestamp` + `qualityControl` collected.
- Max or min selected (by value only).
- Observation normalized to rule.unit.
- `raw_payload` now contains:
  - `target_date`, `query_start`, `query_end`
  - `observation_count`
  - `selected_observation`
  - `observations`: full list of every usable (timestamp, value, unit, quality_status)
  - `warnings`: [] or entries for `<12` records or non-V/unknown QC on the selected one.

**Mapping (noaa_station_mapping.json)**:
- Flat `mappings` object keyed by lower city or station id (KNYC, KLAX, KORD, KMIA, KSFO, KBFI/KSEA, KBOS, KDCA, KDEN, KPHX, etc.).
- Each entry: `city`, `aliases`, `noaa_station`, `station_name`, `coordinates` (lat/lon), `noaa_gridpoint`, `notes`.
- **Zero timezone, offset, iana_tz, or tz-derived fields**.
- Coords are present (usable in principle to guess tz via a lookup, but code never does this for observations; only for station resolve + gridpoint).
- Usage notes mention only station vs gridpoint, coords for "Open-Meteo etc."

**Forecast path contrast**: `_select_period` (297+) also uses bare date match on `startTime[:10]`, with daytime/nighttime logic, but that is a different (forecast periods) concern and not the focus of this local-day obs query audit.

**Tests (test_noaa_provider.py)**:
- Observation happy path uses KNYC + window_start='2026-06-03' + three UTC features on 2026-06-03 (13:00Z, 18:00Z, 22:00Z) → max selected and converted F.
- Newer tests assert presence of `observations`, `query_start`/`query_end` in raw, and warning emission for low count or non-V QC.
- No test uses a window_start whose UTC slice would straddle a local day boundary for the station's tz.
- Forecast tests use startTimes with offsets (e.g. -04:00), but obs path always forces Z in the query.

**Prior audit cross-refs**:
- settlement-observation-audit.md (High Risk #1, lines ~38-46): explicitly quotes the exact 00Z-23:59Z code, explains the EDT offset problem for KNYC ("local June 3 high can span different UTC windows"), notes absence of tz handling or lookup despite the mapping file, and recommends "Use station tz (or US default) or lookup from mapping to expand the query window to the local calendar day."
- rule-operator-backfill-audit.md references the above and lists the date-alignment hazard among items that must be addressed before treating backfill results as authoritative calibration ground truth.

## Concrete Failure Examples

**KNYC (New York / America/New_York, UTC-4 in summer EDT)**:
- Market window_start = "2026-06-03".
- Query: 2026-06-03T00:00:00Z – 2026-06-03T23:59:59Z.
- Local time covered: approximately 2026-06-02 20:00 EDT – 2026-06-03 19:59 EDT.
- True local "high on June 3" (00:00–23:59 local) can include temperatures from 20:00–23:59 EDT on June 3 (i.e. 00:00Z–03:59Z on June 4), which fall **outside** the query. Conversely, late June 2 local temps (after 20:00 local June 2) are included.
- If the actual daily max occurs in the evening (common in heat waves), it is missed → selected "high" is too low → _matches_rule can flip from the correct YES to NO (or vice versa for a "below" market).

**KLAX (Los Angeles / America/Los_Angeles, UTC-7 PDT)**:
- Larger shift (~7h). June 3 00Z-23:59Z ≈ local June 2 17:00 – June 3 16:59. Misses the last 7 hours of the local day.

**KORD (Chicago / America/Chicago, UTC-5 CDT)**:
- ~5h shift. Similar truncation risk on the evening side.

**Edge case with exact equality**:
- Observation exactly at a boundary timestamp that is the true local max. Depending on whether the API includes the endpoint and how the market resolution rule phrases "at least" vs "above", an inclusive vs exclusive error can occur on top of the day misalignment.

**Low-coverage interaction**:
- If the API returns fewer records near the "real" local max window (sensor gaps, the 7-day historical limit on api.weather.gov, etc.), the <12 warning fires but the wrong-day max is still used.

These are not theoretical: the prior audits already called this out, and real Polymarket NYC weather markets use KNYC/Central Park (explicitly noted as the non-airport exception in some sources).

## Recommended Minimal Implementation

**Do the smallest thing that materially reduces the risk while preserving the audit trail and preview safety model.**

1. **Augment mapping (data-only, no src change yet)**  
   Add an IANA `timezone` field to every entry, e.g.:
   ```json
   "new_york": { ..., "noaa_station": "KNYC", "timezone": "America/New_York", ... }
   "los_angeles": { ..., "timezone": "America/Los_Angeles", ... }
   "chicago": { ..., "timezone": "America/Chicago", ... }
   ```
   (Use standard IANA names. This is the single source of truth; coords can stay for other uses.)

2. **First slice options (pick one, keep tiny)**  
   - **Preferred minimal-correct**: In `fetch_observation`, if mapping supplies a `timezone`, use `zoneinfo.ZoneInfo(tz)` (stdlib, Python 3.9+) to compute the proper local 00:00–23:59 bounds for `target_date` in that tz, convert those local datetimes to UTC ISO strings with Z, and query the (slightly wider) range. Still collect all features, but the window is now the intended local day. Store the chosen `local_start`/`local_end` (in addition to the UTC query strings) in raw_payload.
   - **Safer "expand then filter" (less tz math in query)**: Always query a fixed safe pad, e.g. `(target-1 day)T12:00:00Z` to `(target+1 day)T12:00:00Z`. After receiving features, for each parse its `timestamp`, convert to the station tz (if known), and keep only those whose local date == target_date. This tolerates mapping gaps and makes the "all observations we considered" list even more useful for audit.
   - **Warning-only first (lowest risk of new bugs)**: Keep the current UTC query exactly as-is. Always append a warning such as:
     ```
     "UTC calendar-day window used for target_date; station local day (America/New_York) alignment not enforced. Review selected_observation and full observations list."
     ```
     Store `assumed_tz` (from mapping or "unknown") and a `local_day_alignment` flag in raw_payload. This makes every backfill/ preview emit an explicit audit marker while the team decides on the filter logic. Existing low-coverage / QC warnings continue to fire.

3. **Raw payload & preview hygiene (already partially done – extend)**  
   - The current inclusion of the full `observations` list + `query_start`/`query_end` + `warnings` is excellent for re-review. Keep it; also surface the chosen window and any tz used in `SettlementPreviewResult` (so the calibration UI can show "queried UTC 00-23Z for local 2026-06-03 America/New_York (offset -04:00)").
   - In preview (which does not persist), always surface the window details + any warnings so a human can eyeball before approving a backfill.

4. **Avoid over-engineering**  
   - No new third-party tz libs.
   - Do not yet switch to NWS "daily" summary products or NCEI (different API surface, historical depth, attribution).
   - Do not auto-"correct" an already-settled rule or mutate past signals.
   - Do not change the forecast path or `_select_period` in the same slice.
   - Mapping change is data; the code change that consumes the new field can be a one-function addition + a couple of tests.

5. **Phased rollout**  
   - Phase 0 (this doc): document.
   - Phase 1: mapping.json + warning-only + test that the warning appears and query bounds are still recorded.
   - Phase 2: implement widened query + local-date filter (or direct local-bound computation) using the tz field; update warnings to be informational only when alignment succeeded.
   - Phase 3 (later): consider also fetching the station's own 24h max/min fields from the API (when present) as a cross-check, or storing the full original response.

This keeps the blast radius tiny, preserves every previous audit invariant, and directly attacks the "High Risk #1" item called out in the two prior reviews.

## Tests To Add

(Recommendations only; no test files are modified here.)

**tests/test_noaa_provider.py** (add to the existing @patch observation tests):
- `test_noaa_fetch_observation_uses_local_day_bounds_when_tz_present` (provide a rule with window_start for a KNYC entry that now has tz in mapping; supply mock features with timestamps on both sides of the UTC day boundary; after impl, assert the selected max is the one that belongs to the *local* target date, and that query_start/end reflect the adjusted UTC range or that filtering happened).
- `test_noaa_fetch_observation_emits_tz_alignment_warning_when_mapping_lacks_tz` (or when using the warning-only path).
- `test_noaa_fetch_observation_stores_full_query_window_and_compact_list_for_audit` (already partially present – make stricter: assert that even with a boundary-crossing mock, the stored 'observations' contains the raw timestamps so a reviewer can see "the max at 02:30Z next day was excluded because it is local June 4").
- Negative coverage: station with no mapping tz still works (falls back to current behavior + warning).
- Add a KLAX or KORD test case with larger offset to prove the shift is handled.

**Integration / calibration tests** (future):
- A preview/backfill test that seeds a market whose title date would have been misaligned under pure UTC, uses a fake provider returning boundary values, and asserts the would_resolve / resolved_outcome matches the local-day expectation (once the slice is implemented).

Keep using the existing MagicMock style; no real network.

Also consider adding a tiny unit for a helper `_local_day_utc_bounds(target_date: str, iana_tz: str) -> (start_z, end_z)` if extracted.

## Risks To Defer

- True historical backfills beyond the ~7-day window that api.weather.gov /observations currently serves (use NCEI daily-summaries or global-hourly instead; different contract).
- Non-temperature variables for obs (precip/snow still unsupported in fetch_observation).
- Automatic "use the station's reported 24h max/min fields when non-null" cross-check (the MADIS note in NWS docs suggests they are sometimes null outside central tz anyway).
- Full SensorThings/IoT pagination handling (`@iot.nextLink`) – for a 1–2 day padded query on a typical ASOS station the single response is expected to be complete.
- Non-US stations or exotic tzs (current mapping is US-centric).
- Changing how the *market* "window_start" itself is parsed (that's a rules.py concern, already audited).
- Storing or comparing against official NWS Daily Climatological Report (CLI) products – valuable future ground truth but separate ingestion path.

Any implementation must not remove the ability to see exactly what window was queried and every observation considered (current raw_payload contract).

## Do Not Change / Safety Invariants

- **Do not remove or weaken the `warnings` array and the full `observations` list from raw_payload**. These are the only practical way a human (or future calibration UI) can detect "we queried the wrong day and picked the wrong extremum." The prior settlement audit already praised the move toward faithful payload; this audit reinforces it.
- **Do not assume every station is Eastern Time or UTC**. The mapping must be the source of truth.
- **Keep the preview path side-effect free** (it already does `persist_parsed=False`). Any local-day logic must be observable in the preview result so operators can see the window before a backfill mutates signals.
- **Do not let an observation-derived outcome bypass the existing `tradable` + `_validate_rule` gate** (cross-ref rule-operator-backfill-audit.md). A bad tz-derived window is still better than letting a non-tradable rule produce a settled signal.
- **Query parameters must remain visible in the stored payload** (`query_start`/`query_end` or equivalent). This allows later re-execution or forensic comparison against NCEI data.
- **The change is obs-only**. Forecast period selection (`_select_period`) and the broader market workflow paths are out of scope for this slice.
- **No silent "fix" of already-settled signals**. If a previous backfill used the old UTC window, the new logic must not retroactively rewrite `resolved_outcome` without explicit human re-approval (separate from this minimal slice).
- Cross-reference the two prior reviews: any implementation of the "Correct day bounds" item they both recommended must also address (or at minimum warn about) the other high/medium risks they listed (QC filtering, raw fidelity, blind execution, operator semantics).

This audit is complete and read-only. All statements are derived from direct inspection of the five mandated files, greps, the two previous audit documents, and publicly documented NWS API behavior (times are ISO-8601/UTC in queries and responses; daily max/min semantics are local station time per ASOS climatology; 1-day observation ranges are normally returned in a single FeatureCollection). No production files were edited.

The only file created by this task is the required `docs/reviews/nws-local-day-query-window-audit.md`.