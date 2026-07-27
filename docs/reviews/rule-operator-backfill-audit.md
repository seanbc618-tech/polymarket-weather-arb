# ResolutionRule / Operator / Threshold Semantics Audit for Settlement Observation Backfill

**Date**: 2026-06-13  
**Auditor**: Grok (read-only review)  
**Scope**: Read-only audit — no production code, tests, or git modifications.  
**Focus files** (as specified):
- src/polymarket_weather_arb/domain/rules.py (the parser, _extract_*, _extract_operator, high/low, unit, tradable)
- src/polymarket_weather_arb/services/settlement_service.py (_matches_rule, _validate_rule, _load_rule + parse fallback, preview_market, backfill_market)
- tests/test_rules.py
- tests/test_settlement_service.py (including new preview tests and parse-on-missing)
- docs/reviews/settlement-observation-audit.md (prior broader audit, which already flagged operator fragility)

**Audit questions addressed**:
1. below/under/less than 是否稳定映射到 <=
2. above/over/exceed/at least 是否稳定映射到 >=
3. 是否存在“strict > / <”无法从市场文案准确判断的问题
4. temperature_high / temperature_low 的识别是否会错
5. unit F/C 是否可能和市场结算说明不一致
6. parse_resolution_rule 自动 tradable 是否太乐观
7. backfill 对 rule 的信任边界是否应该增加人工确认字段
8. 应该新增哪些 parser fixtures/tests

## Executive Summary

The settlement observation backfill (and the newer `preview_market` path) ultimately derives `resolved_outcome = 'yes' if _matches_rule(observation.value, rule) else 'no'` using only four fields from `ResolutionRule`: `variable`, `operator`, `threshold`, `unit` (plus station/window for the provider fetch). The rule is either loaded from a prior `resolution_rules` row or freshly produced by `parse_resolution_rule(title, description)` (see settlement_service.py:118, 122, _load_rule:96-121, backfill:74, preview:54).

The parser in `domain/rules.py` is a heuristic regex + keyword window machine. It is the single source of truth for auto-derived rules used by backfill when no persisted rule exists. Once `tradable=True` and the four fields are present, `_validate_rule` (settlement_service.py:124-132) allows backfill/preview to proceed and _matches_rule (135-147) performs a literal comparison. No further human review of the *operator/threshold semantics* is required.

**Core conclusion**: The mapping is *mostly stable for common "at least / exceed / above" titles → >= and "below / under" titles → <=*, but it is **not robust enough** for high-stakes settlement backfill without additional guardrails. 

- Parser collapses all "greater-ish" language (including strict `>`) to `>=` and all "less-ish" (including `<`) to `<=`.
- It never emits strict `>` or `<` (even though _matches_rule and some probability/ensemble paths support them).
- High/low detection and unit defaults are crude prefix/regex heuristics.
- `tradable` + confidence is optimistic and does not reject ambiguous operator phrasing.
- Backfill (even with preview) fully trusts the parsed rule for producing calibration ground truth (settled signals that feed Brier/hit-rate scores).

The existence of `preview_market` (which does not persist) and the fact that `raw_text` is stored are positive, but insufficient alone. Real Polymarket weather market titles in the wild (and in the test corpus) frequently use "exceed", "above", "at least" for the >= case and would benefit from explicit below/under test coverage.

This is a **medium-to-high risk** area specifically for the correctness of observation-derived YES/NO that get written into `model_signals.resolved_outcome` and used for calibration.

## Concrete Parser Risks

1. **below/under/less than mapping (question 1)**  
   In `rules.py:155`:
   ```python
   if any(token in window for token in ('below', 'under', 'less than', '<=', '<')):
       return '<='
   ```
   - Stable for the listed tokens → always `<=` (never `<`).
   - The 50-char-before + 80-char-after `window` (line 154) is taken around the *number match position* from the temp/precip/snow regex.
   - "less than or equal" is not specially distinguished (treated same as "less than").
   - If the keyword appears far from the number (e.g. "Will the temperature be 80F or below?"), the window may miss it → falls to default `>=`.
   - No test in `test_rules.py` asserts any "below"/"under"/"less than" title produces `operator == '<='`.

2. **above/over/exceed/at least mapping (question 2)**  
   In `rules.py:157`:
   ```python
   if any(token in window for token in ('at least', 'above', 'over', 'exceed', 'greater than', '>=', '>')):
       return '>='
   ```
   - "at least" and "greater than" correctly → `>=`.
   - "exceed", "above", "over" also → `>=` (default behavior for the "exceed 80F" titles used in the demo fixture and almost every test market: "Will the high temperature in New York exceed 80°F...").
   - "exceed" linguistically implies strict >, but is collapsed to >=. Whether this matches the actual NWS station settlement rule (high temperature "exceeds" the threshold) is not validated by the parser.
   - All real examples in the repo (fixture, test_*.py seeds, dashboard tests, etc.) use this family and expect `>=`.

3. **Strict > / < cannot be accurately derived from text (question 3)**  
   - The temp regex (rules.py:123) *mentions* `|>=|>|` and `|<=|<` in the keyword alternatives, but `_extract_operator` never returns the strict forms — it always returns the inclusive `>=` or `<=` (or None → default `>=` at line 130).
   - `_matches_rule` (settlement_service.py:139-147) *does* implement `>` and `<` branches (and raises only on unsupported).
   - Other consumers also branch on them:
     - ensemble_workflow.py:58 (`if rule.operator in ('>', '>=')` vs `('<', '<=')`)
     - probability.py:55 only handles `>=` and `<=`; strict falls to "unsupported operator".
     - fixture_service.py:111 special-cases only `<=`.
   - Consequence for backfill: an auto-parsed rule can never carry strict semantics. If a market's fine print or settlement source uses strict inequality, the observation value will be compared inclusively, potentially flipping the resolved_outcome for an obs exactly equal to threshold.
   - Manually-seeded rules (via DB or tests) *can* have strict ops, creating an inconsistency between parser output and what backfill can consume.

4. **temperature_high / temperature_low mis-identification (question 4)**  
   - Regex (123) optionally captures `high|max(?:imum)?|low|min(?:imum)?` immediately before `temperature|temp`.
   - Then (128-129):
     ```python
     prefix = text[max(0, temperature.start() - 20) : temperature.start()].lower()
     variable = 'temperature_low' if any(word in prefix for word in ('low', 'minimum', 'min')) else 'temperature_high'
     ```
   - The 20-char lookback is *before the entire match start* (i.e. before the qualifier + "temperature" + ... + number). If the qualifier is earlier ("the low for the day will be... 62"), or the sentence is "Will temperature in NYC reach a minimum of 62", the prefix may not contain the trigger word → defaults to `temperature_high`.
   - "maximum" is recognized in the regex but does *not* flip to low (only low/min/minimum do).
   - All current seeds and the demo fixture use "high temperature" phrasing, so they pass. No negative test for low-temperature titles or tricky word order.
   - Wrong variable means the wrong NOAA period selection in forecast path *and* wrong observation intent for backfill (high vs low on the same date).

5. **unit F/C mismatch risk (question 5)**  
   - Temp always defaults to 'F' (rules.py:131: `unit = _normalize_unit(...) or 'F'`).
   - `_normalize_unit` (162-176) only acts on the captured group after the number in the regex (e.g. "80F", "80 °C", "degrees Celsius").
   - If the title says "80 degrees" or the unit lives only in the description ("... per the official reading in Celsius"), or the market is a non-US market whose title omits the unit, the rule may get 'F' while the actual settlement source (and the observation provider's native unit) is C.
   - `normalize_value` (domain/weather.py) will convert during obs fetch (noaa.py:244-245), but if rule.threshold was parsed under the wrong assumption, the numeric comparison in _matches_rule is against the wrong scale.
   - Current backfill only targets NOAA (US, F-biased). China buckets use separate rules with explicit C. Still a latent risk for any title that mixes units or omits them.
   - No test in test_rules.py exercises a C unit in a temperature rule.

6. **parse_resolution_rule auto-tradable is too optimistic (question 6)**  
   - `tradable = not rejection_reasons and confidence >= 0.75` (rules.py:59).
   - Rejection reasons (43-56) are only structural: multi-location, unsupported var, no thresh/operator, no loc/station, no source, long-cycle. **Ambiguous or potentially-wrong operator language is never a rejection reason.**
   - `_confidence` (213-230) is a simple additive heuristic (0.25 base + loc/src/var/thresh bonuses, -0.1 per rejection). A title with "exceed" or "above" that is 80% clear will easily clear 0.75 even if the operator window logic is on the edge.
   - Once `tradable=True`, `_validate_rule` + backfill will happily derive a definitive yes/no from an observation and stamp `model_signals`. There is no "operator needs review" or "phrasing confidence low" state that blocks backfill while still allowing research paths.
   - Contrast with the explicit `rejection_reason` field, which is only populated for the structural cases.

7. **Backfill's trust boundary over the rule (question 7)**  
   - `backfill_market` (and `preview_market`) call `_load_rule` → possibly `parse...` → `_validate_rule` (which only requires `tradable` + the four fields) → `_matches_rule`.
   - No additional check that the operator/threshold was human-confirmed for settlement semantics.
   - `raw_text` is stored (good), and `SettlementPreviewResult` (and the newer backfill result) now surface `rule_operator` / `rule_threshold`.
   - However, there is still no persisted flag such as `operator_manually_confirmed`, `parser_version`, `matched_operator_snippet`, or `settlement_source_text`. A rule parsed in 2026-05 and later used for backfill in 2026-06 after market text was clarified can silently use stale semantics.
   - The prior settlement-observation-audit.md already called out the blind nature of backfill and recommended preview + stronger provenance; the current code has preview but the parser trust issue remains.
   - Recommendation in this audit: the rule (or a parallel "settlement_rule_review" record) should carry an explicit human-confirmation dimension before it is allowed to drive observation → yes/no for calibration.

8. **Parser fixtures / tests gap (question 8)**  
   See "Tests To Add" section below. In short: test_rules.py only has one happy "exceed" (→ >= high F), one precip, rejects, and broad rejects. No below-family, no low-temperature, no C units, no operator-in-text symbols producing the collapsed <=/>=, no roundtrip assertions against real fixture parsed_rule, no "ambiguous window" cases. Settlement tests mostly use pre-seeded rules or the "at least" title for parse-fallback; they do not stress the parser for the direction that preview/backfill will use to decide outcome.

## Examples Of Ambiguous Market Text

These are synthesized from patterns observed in the demo fixture, test seeds (all of which are "high ... exceed/at least/above 80F" → >=), the prior audit doc, and typical Polymarket weather market phrasing. None of the "below" variants currently exist as parser test cases.

**Common (currently handled, but semantically borderline):**
- "Will the high temperature in New York exceed 80°F on May 8, 2026?" → (via "exceed") operator='>=', variable=high, unit=F (demo fixture + many tests)
- "Will NYC high temperature be at least 80F?" → (via "at least") >= (settlement_service seed)
- "Will NYC high temperature be above 80F?" → (via "above") >= (live launchpad test)

**Below direction (currently untested in parser):**
- "Will the high temperature in New York be below 75F on June 3?"
- "Will the low temperature ... stay under 60?"
- "Will temperature in Boston be less than 70F?"
- "Will it stay below 80F or under the record?"

  Expected by current logic: operator='<=', variable=high or low accordingly. But the window heuristic + lack of tests means we don't know it is stable.

**Ambiguous / strict / negation risk:**
- "Will the high temperature exceed 80F?" (title uses "exceed" but desc says "strictly greater than the reported high")
- "Will the temperature be 80F or above?" ( "or above" may be intended >= , parser sees "above")
- "Will New York not reach a high of 85F or more?" (negation + "more" → may miss operator keywords or mis-set direction)
- "Will the minimum temperature fall below 50 or under 45?" (compound, "minimum" may or may not trigger low correctly depending on position relative to number)
- "Will the high be greater than 80?" ( "greater than" → >= , but strict language)

**Unit / variable risk:**
- "Will the temperature in NYC reach 25 C on the 10th?" (C in title → hopefully captured; otherwise defaults F)
- "Will the low for the day in KNYC be 62 degrees (per NWS)?" (unit after, "low" may be far from number)
- "Will maximum temperature stay below 30°C?" ( "maximum" captured in regex but variable decision only looks for low-words in prefix → would be high, which is wrong)

**Real examples from corpus (all map to >= high F today):**
- Fixture + test titles: "Will the high temperature in New York exceed 80°F on May 8, 2026?"
- "Will NYC high temperature be at least 80F?"
- "Will the high temperature in New York exceed 80F?"

The parser has never been exercised on a title whose correct settlement semantics are "strictly less than" or a C-based US-adjacent market.

## Tests To Add

(These are recommendations only; no files are modified in this audit.)

**In tests/test_rules.py (expand the existing clear rule test and add parametrized cases):**
- Parametrized "below/under/less than/<=/<" titles (with and without "high"/"low") must produce `operator == '<='` and correct `variable`.
- Parametrized "at least/above/over/exceed/greater than/>=/>" titles → `operator == '>='`.
- "exceed" titles still → `>=` (document that this is the current behavior, even if semantically strict in English).
- Low temperature titles: "Will the low temperature ... be at least 60F?" → low + >= ; "Will the minimum ... stay below 55?" → low + <= .
- Unit C: title containing "25 C" or "30°C" or "degrees Celsius" → unit='C' (and still tradable if other fields ok).
- Strict symbol in text: "Will high temp be > 80?" → still `>=` (current collapse behavior).
- Prefix window stress: qualifier far from number, "maximum temperature", "the low side of the high", compound sentences — assert either correct variable or that it produces a rejection_reason (so tradable=False).
- Roundtrip with the demo fixture json's "parsed_rule" expectations.
- Confidence/tradable boundary: a title that has location+source+number but borderline operator keywords still gets tradable=True today (document/accept or decide to add a new rejection for "ambiguous comparison word").

**In tests/test_settlement_service.py (and any CLI/dashboard calibration tests that exercise parse fallback):**
- Add a seeded market whose *title* contains "below 80F" (no pre-existing resolution_rule row). Call `preview_market` and `backfill_market` (with fake provider returning a value) and assert the `would_resolve_outcome` / `resolved_outcome` is correct per _matches with the *parsed* `<=` operator.
- Test that a title producing `temperature_low` + `<=` correctly decides yes when obs is low enough.
- Test C unit rule + observation that normalizes and still matches (or not) correctly.
- Negative: title that the parser gives `operator=None` (should be rejected at validate, never reach _matches).
- Verify that preview never persists the rule, backfill does (already partially tested).
- Add a case where the freshly parsed rule has `tradable=False` (e.g. missing source) → backfill/preview raises the expected ValueError.

**Cross-cutting / fixtures:**
- Add a second fixture json (or extend demo) with a "below" / "low" / "C" example and its expected parsed_rule.
- In test_rules or a new parser regression suite: assert that all the "real" titles from the test corpus (the exceed/at least ones) continue to parse to the exact operator/variable/unit seen in the pre-parsed json blobs.
- Consider a small table of (title, expected_operator, expected_variable, expected_unit, expected_tradable) that both the unit test and any future parser change must satisfy.

These would directly address the "parser fixtures/tests" gap and give confidence that backfill's _matches_rule decisions are stable for the language actually used in Polymarket weather markets.

## Recommended Parser Changes

(For future implementers; this audit makes no edits.)

- Make `_extract_operator` (and callers) able to surface when the text contained a strict symbol vs an inclusive word, perhaps by also storing a `operator_text` or `is_strict` alongside the normalized `operator`. Or decide once and for all that weather settlement rules are *always* inclusive for these markets and drop the strict branches from _matches_rule / probability / ensemble (simplification).
- Improve the high/low prefix logic: search a wider or bidirectional window, or parse the qualifier from the regex group itself rather than a second 20-char slice. Add "maximum"/"minimum" to the variable decision.
- Add a new rejection reason (or lower confidence) when the operator was derived only from a default or from a single ambiguous word ("exceed", "above", "over", "below") without a clearer "at least" / "less than or equal". This would make `tradable` less optimistic for backfill.
- When parsing for settlement purposes, also extract and store the exact matched substring that drove the operator (for human audit and for "re-parse with better rules later").
- Consider a small explicit allow-list or mapping for common Polymarket title verbs instead of the broad `any(token in window)`.
- For unit: if no unit captured, look harder in the full raw_text (title+desc) before defaulting to F. For non-US markets that reach the weather parser, default or require C.
- Expose in ResolutionRule (or a companion) a `parser_notes` or `operator_confidence` so that preview/backfill UI and CalibrationService can surface "this rule's operator came from the word 'exceed' 12 chars before the number; review recommended".

Any change to the collapse of > → >= etc. must be coordinated with _matches_rule, probability.py, ensemble_workflow.py, and the probability pricing paths so that backfill outcomes remain consistent with how the original signals were generated.

## Do Not Change / Safety Invariants

- **Do not allow non-tradable rules into backfill or preview.** `_validate_rule` must continue to reject when `tradable=False` or when any of variable/threshold/operator/unit is missing. This is the current gate that prevents ambiguous parses from producing calibration outcomes. (The optimism of when `tradable` becomes True is the thing to improve, not the enforcement.)
- **_matches_rule must remain the single, simple, auditable definition of "does this observation satisfy the rule for YES".** It should stay a small 4-way if + error. Do not add fuzzy logic, unit conversion, or "approximately" here — that belongs in the parser or in a separate review step.
- **Parser must continue to store full `raw_text`.** This (plus the market title/desc at parse time) is the only forensic trail when a later backfill produces a surprising yes/no.
- **Do not start emitting strict `>` / `<` from the parser unless a deliberate product decision is made about what "exceed", "above", "below" actually mean for NWS settlement, and all downstream probability/ensemble/fixture paths are updated consistently.** Currently the parser's contract is "we only produce inclusive comparisons."
- **Backfill/preview must never bypass the rule entirely.** Even with preview, the would-be outcome is always computed from the (parsed or stored) rule + the observation value. Manual override of outcome should stay a completely separate "manual settle" path (as it is today).
- **tradable + confidence >= 0.75 is the documented bar for automatic rule usage in the weather module.** Changing the 0.75 threshold or adding operator-specific rejections is a policy change that must be reflected in docs, the handoff, and module metadata (`min_rule_confidence` etc.), not a silent parser tweak.
- Cross-reference the prior settlement-observation-audit.md: the operator risks identified there are still present; any preview/dry-run work should also surface the rule_operator/rule_threshold prominently so humans can catch bad parses before settling signals.

This audit is strictly read-only. All observations are derived from the on-disk content of the five specified files plus targeted supporting reads and greps for context. No files outside the audit document were created or modified except the auxiliary directory creation needed to place the deliverable. 

The parser is "good enough" for the current set of demo/"exceed"/"at least" titles that dominate the test corpus, but it is not yet rigorous enough to be the unattended source of truth for observation → resolved_outcome that populates calibration history.