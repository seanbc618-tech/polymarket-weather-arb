# D0 Hourly Conditioning And Calibrated Weather Strategy

## Objective

Complete the intended weather trading loop:

1. use multi-source forecasts to enter undervalued D2/D1 temperature buckets;
2. condition D0 probabilities on the exact settlement station's observations,
   local time, observed trajectory, and remaining hourly forecast;
3. manage scale-ins, bucket switches, and exits from the same updated evidence;
4. settle source-level predictions and gradually calibrate numerical weather
   source weights by city and forecast horizon.

This task must improve expected-value estimation. It must not add a second
strategy engine, BUY path, SELL path, scheduler, persistent mode, or safety gate.
No real order may be submitted during implementation or verification.

## Confirmed Problems

### P0: D0 observation coverage is incomplete

- `NoaaProvider.fetch_observation()` uses `api.weather.gov`, which is appropriate
  for supported US stations but cannot cover global ICAO stations such as ZSPD,
  ZUUU, ZHHH, ZSQD, RKSI, and EGLC.
- Polymarket descriptions already contain the exact Wunderground settlement
  station. The system must use that station rather than guess a nearby airport.
- AWC METAR/SPECI and NWS station observations frequently redistribute the same
  ASOS observation. They must be merged as one station evidence stream, not
  counted as independent model votes.

### P0: D0 conditioning is only a floor clamp

- Current pricing clamps daily ensemble values below the observed maximum.
- It does not represent local time, remaining warming hours, recent temperature
  trend, remaining hourly peak, peak timing, cloud cover, solar radiation, or
  wind.
- A bucket can therefore retain too much probability after the plausible warming
  window has passed, or too little probability while a strong warming trajectory
  remains.

### P0: Held-position evidence refresh is too slow for D0

- The loop runs every five minutes, but analyses younger than 30 minutes are
  reused.
- D0 observations and exit evidence can therefore remain stale across several
  ticks.
- Faster D0 evidence must not turn into repeated daily ensemble, geocoding,
  Google Weather, or LLM calls.

### P1: Hourly forecast evidence is absent

- Open-Meteo currently stores daily high/low only.
- D0 needs a compact local-day hourly trajectory: temperature, cloud cover,
  shortwave radiation, and wind speed.
- The compact context must be auditable and cached with the group forecast. Raw
  vendor payload retention rules must remain respected.

### P1: Base weather sources never learn from outcomes

- Aggregate analyses and LLM votes are settled, but ECMWF, GFS, ICON, GEM,
  NOAA/Open-Meteo reference, and Google reference do not persist their own bucket
  probabilities as source-level signals.
- Base sources therefore remain effectively equal-weighted regardless of city,
  horizon, and historical accuracy.

### P1: Calibration must avoid correlated-sample inflation

- Sibling buckets from one city/date are one event, not independent events.
- Repeated ticks and restarts are forecast revisions, not extra resolved events.
- A source needs enough distinct resolved events before its weight may depart
  from 1.0.
- Weighting must be bounded and shrunk toward equal weight to avoid overfitting.

### P1: Weighted consensus must preserve entry semantics

- At least three independent quantitative sources must remain mandatory.
- The existing two-thirds support, 40% median probability floor, robust
  dispersion, fee-aware net edge, and minimum edge remain entry requirements.
- Calibration may alter bounded source influence; it may not let one source or
  the LLM dominate the decision.

### P1: D0 reasons and UI data are not sufficiently auditable

- Analysis reasons must expose station, local time, observation age, observed
  maximum, current temperature, recent trend, remaining forecast peak/time,
  warming hours, source weights, and whether degraded evidence was used.
- Missing hourly or AWC data must degrade to the current proven model, never
  manufacture values or silently turn a rejection into a trade.

## Reuse Map

| Concern | Existing owner to extend |
| --- | --- |
| Exact station parsing | `GlobalTemperatureBucketRule.station` |
| NWS observations | `NoaaProvider.fetch_observation()` |
| AWC read adapter | `adapters/weather/`, implementing the existing observation contract |
| Forecast acquisition/cache | `MarketWorkflowService` and `weather_forecasts` |
| Observation persistence | `Repository.save_observation()` and `weather_observations` |
| Probability calculation | `domain/global_bucket_pricing.py` |
| Source signals | existing `model_signals` table |
| Settlement labels | `ResolutionAuditService` / existing signal settlement |
| Calibration | `CalibrationService` |
| Autonomous scheduling | existing `AutopilotService.tick()` only |
| BUY/SELL | existing `TradingService` / `PositionExitService` only |

## Implementation Plan

### Slice 1: Exact-station D0 evidence

1. Add a small AWC METAR adapter under `adapters/weather/`.
2. Query only the exact parsed ICAO station and bounded target-day window.
3. Parse decoded temperature plus observation time and retain raw METAR text for
   audit. Prefer precise hourly/SPECI temperature data when available.
4. Merge NWS and AWC records by station and timestamp. Do not treat them as two
   votes.
5. Use NWS as the US primary and AWC as cross-check/fallback; use AWC for global
   ICAO stations unsupported by `api.weather.gov`.
6. Enforce station match, timezone, freshness, and explicit data-quality reasons.

### Slice 2: Compact hourly context

1. Extend the existing Open-Meteo forecast request to include local hourly
   temperature, cloud cover, shortwave radiation, and wind speed.
2. Retain only the target day's compact hourly records in the pricing payload.
3. Derive local current time, hours remaining, recent observed trend, remaining
   hourly maximum, expected peak time, post-peak flag, cloud/radiation/wind
   summaries, and evidence age.
4. Cache this context with the city/date forecast group; do not add a table.

### Slice 3: D0 conditional probability v2

1. Keep the observed-maximum hard floor and source tolerance.
2. Add a bounded hourly-trajectory source probability rather than rewriting or
   mutating ensemble members.
3. Reduce uncertainty as the remaining warming window closes, but never below a
   documented floor.
4. Eliminate buckets made impossible by verified observations.
5. Fall back to current multimodel v6 when hourly evidence is missing; entry must
   still satisfy the normal consensus gates.
6. Persist full D0 context and the exact probability contribution in reasons.

### Slice 4: D0 refresh cadence

1. Preserve the five-minute Autopilot scheduler.
2. Allow D0 held positions and actionable D0 candidates to refresh observation
   and hourly evidence every tick when the cached evidence is older than five
   minutes.
3. Keep daily ensemble/Google/LLM forecast revisions on their existing cache and
   call limits.
4. D1/D2 retain the 30-minute analysis freshness window.

### Slice 5: Source-level signal persistence

1. For each source probability used in pricing, write a deduplicated
   `model_signals` row with:
   - exact event identity;
   - city and target date;
   - horizon (`D2`, `D1`, or `D0`);
   - forecast revision;
   - provider/model name;
   - bucket probability;
   - source role and evidence timestamp.
2. Reuse existing signal settlement so Polymarket's resolved winner labels every
   sibling source signal.
3. Count distinct city/date events, not rows or revisions, for calibration
   eligibility.

### Slice 6: Bounded city/horizon calibration

1. Calculate each source's Brier score for the matching city and horizon.
2. Keep weight `1.0` until at least 20 distinct resolved events exist.
3. Derive relative inverse-Brier skill only among eligible peer sources.
4. Shrink weights toward `1.0` based on sample size and bound each source to
   `[0.5, 1.5]`.
5. If peer coverage is incomplete or malformed, use equal weights.
6. Compute weighted median probability and weighted two-thirds support while
   preserving the minimum model count, probability floor, dispersion, fees, and
   net-edge gates.
7. LLM retains its separate calibrated fractional vote and maximum influence.

### Slice 7: Entry, rebalance, and exit integration

1. Keep the existing D2 scout and D1/D0 staged caps.
2. Rank sibling buckets from the new weighted net-edge analysis.
3. Keep one active bucket per city/date.
4. Reuse existing `0.15` switch hysteresis and exit-before-entry lifecycle.
5. Refresh held-position D0 evidence before `ExitGuardianService` evaluates
   recover-principal, bucket switch, negative hold edge, or settlement hold.
6. Never SELL because a provider is missing, rate-limited, or degraded.

## Acceptance Tests

### D0 evidence

- Exact station from Wunderground URL is used for KLGA, KORD, ZSPD, ZUUU, RKSI,
  and EGLC fixtures.
- AWC response for a different station is rejected.
- Duplicate NWS/AWC timestamps do not become duplicate votes.
- Global AWC fallback yields a verified observation when NWS is unsupported.
- Stale, malformed, or missing observations block a new D0 entry but do not force
  a position SELL.

### Conditional probability

- Observed maximum above a bucket still makes that bucket impossible.
- Rising observations plus a warmer remaining hourly peak retain plausible upper
  buckets.
- Post-peak, low-radiation, cooling trajectory reduces implausible upper buckets
  without overriding ensemble consensus by itself.
- Missing hourly context produces the prior multimodel result exactly.
- D0 and D1/D2 retain fee-aware net edge and two-thirds support.

### Calibration

- Restarts/retries do not create duplicate source signals.
- Ten sibling buckets count as one distinct resolved event.
- Fewer than 20 events leaves all base weights at `1.0`.
- Eligible source weights are bounded to `[0.5, 1.5]` and normalized around 1.
- Unknown city/horizon falls back to equal weights.
- A poor source cannot single-handedly veto or authorize a trade.

### Runtime and execution

- D0 evidence can refresh at five-minute cadence without repeated Ensemble,
  Google, geocoding, or LLM calls.
- D1/D2 caching and 429 stale-if-error behavior remain intact.
- Reconciliation fail-stop, event exposure, idempotency, staged entry, switch
  hysteresis, principal recovery, and full-exit paths remain unchanged.
- No real BUY, SELL, cancel, or background process is executed by tests.

## Verification Gates

```bash
uv run ruff check src/ tests/
MAX_ORDER_USDC=1 MAX_DAILY_USDC=5 MAX_MARKET_USDC=2 uv run pytest -q
git diff --check
git status --short
```

## Stop Conditions

Stop for review instead of continuing if implementation requires:

- a second strategy, scheduler, BUY path, SELL path, or reconciliation path;
- discarding or rewriting existing live audit history;
- treating AWC and NWS copies of the same station observation as independent
  evidence;
- relaxing the existing fee-aware consensus or execution invariants;
- a real-money transaction to verify correctness.
