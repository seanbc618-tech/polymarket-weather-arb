# Global Temperature Bucket V8 / Weather Entry V5

## Objective

`global-temp-bucket-multimodel-v8` estimates the probability of each mutually
exclusive daily-temperature bucket. `weather-entry-v5` turns that estimate into
a bounded live entry size and `weather-exit-v3-settlement-only` manages the
position. The objective is calibrated event-level expected value after fees,
not a high headline win rate and not guaranteed profit. The complete entry/exit
overlay is specified in `docs/strategy/weather-settlement-core-v5.md`.

## Evidence Model

Raw weather feeds are not independent votes. V8 collapses related feeds into
source families before quorum and dispersion checks:

| Family | Typical members |
| --- | --- |
| `ecmwf` | ECMWF, AIFS |
| `ncep` | GFS, GEFS, NOAA/NWS model guidance |
| `dwd` | ICON |
| `eccc` | GEM/CMC |
| `consumer-reference` | Google Weather, reference Open-Meteo |
| `aviation-taf` | AWC TAF |
| `d0-hourly` | D0 hourly trajectory |

Several feeds inside one family may improve that family's estimate, but they
never create additional independent votes. Uncalibrated AWC TAF starts at
weight `0.35` and is advisory. D0 hourly context conditions the distribution
but is not an independent quorum vote.

## Calibration

Weather-source signals are persisted as `global-temp-source-v2`. Calibration
is isolated by:

- city and, where available, settlement station;
- source;
- horizon (`D0`, `D1`, `D2`);
- local forecast phase (`D0_early`, `D0_pre_peak`, `D0_near_peak`,
  `D0_post_peak`, `D1_early`, `D1_late`, `D2_early`, `D2_late`);
- target event and latest forecast revision.

Bias correction is local: station history is preferred, city history is the
fallback, and pooled all-city additive bias is forbidden. Learned uncertainty
uses robust residual sigma with unit-specific floors and caps.

Source skill uses one coherent multiclass score per resolved event. Exactly one
bucket must be the winner. Missing probability mass is represented as an
explicit `other` outcome, so an event with more listed buckets cannot
mechanically accumulate a worse score. A source needs at least 20 distinct
resolved events in the matching scope before it can receive a learned weight.

## Pricing Decision

For every event group V8:

1. Builds a probability for every bucket from each source.
2. Applies local bias and uncertainty calibration.
3. On D0, conditions on the observed maximum and the remaining hourly warming
   trajectory. An observation that makes a bucket impossible sets it to zero.
4. Aggregates correlated sources into independent families.
5. Uses the family median as the central estimate and the family 25th
   percentile as the conservative decision probability.
6. Requires at least three eligible families and exact two-thirds family
   support at the executable ask after fees and slippage.
7. Rejects excessive family dispersion, illiquid spread, stale evidence,
   contradictory D0 evidence, and non-top-ranked sub-`0.10` buckets.
8. Trades only when conservative fee-aware net edge clears `MIN_EDGE`.

There is no separate absolute probability floor. A lower-probability bucket can
be attractive at a sufficiently low executable price, but it still must pass
family quorum, uncertainty, liquidity, fee, and low-price top-rank checks.

The market midpoint is persisted as a benchmark. It does not alter V8 pricing
until at least 20 resolved V8 events exist and an out-of-sample test supports a
defined blend. This prevents circularly treating the price being evaluated as
independent weather evidence.

## Entry Sizing

`weather-entry-v5` starts with remaining horizon/event headroom and then only
reduces it:

- configured order, market, and daily caps remain hard ceilings;
- weak edge, exact-boundary quorum, dispersion, model-risk haircut, stale
  weather, and sub-`0.10` prices reduce size;
- a 20% fractional-Kelly cap uses the conservative probability and executable
  price;
- sub-`0.10` buckets remain capped at `1 USDC`;
- live submission additionally requires edge `>=0.10`, ask `>=0.05`, a
  non-D0 horizon, and no prior accepted BUY in the same event;
- historical performance is reduction-only and remains `1.0` until V5 has 20
  resolved events. Older entry-policy versions are excluded.

All new order intents record `entry_policy_version=weather-entry-v5`.

## LLM Boundary

Production Autopilot calls the batch workflow with `allow_llm=False`. The LLM
remains available for explicit research and continues to have no production
pricing weight or token spend. Re-enabling it requires a new policy version and
out-of-sample evidence; changing an environment flag is not sufficient.

## Operational Invariants

- Limit orders only; submit-time order-book and D0 evidence revalidation stay
  mandatory.
- Global-bucket candidate selection and live submission require the exact
  current model version. Legacy, guard, switch, and `-entry-gated` analyses may
  support position review but can never authorize a new entry.
- Official settlement observations and Polymarket resolution audits remain
  separate from forecast evidence.
- Entry logic never bypasses reconciliation, circuit breaker, idempotency, event
  exposure, or configured risk caps.
- Every small position is a settlement core. Profit recovery and dust do not
  authorize SELL. Official impossibility or a twice-confirmed executable SELL
  value above the held probability upper bound may authorize a full exit.

## Honest Readiness State

V8/V5 deliberately starts with no inherited entry-policy advantage. V4 entries
are excluded from learned V5 entry calibration. Production remains trading
disabled for shadow collection and must not return to full-live sizing until at
least 20 newly resolved V5 events pass a fee-adjusted event-level review split
by horizon, phase, city/station, source family, and entry-price band.
