# Historical Replay Baseline - 2026-07-20

## Scope

This is a read-only historical replay baseline, not a claim that the current
strategy was run unchanged against years of point-in-time order books. The
source is a consistent VPS SQLite backup copied to the local workstation:

- Snapshot: `data/backtests/polymarket_weather-20260719T170848Z.sqlite3`
- SHA-256: `357647777839ed1c7ccfeea1f5166a49ad141d0a62130ef8e897f98f47326b43`
- SQLite quick check: `ok`
- Analysis window: 2026-07-11 07:53 UTC through 2026-07-19 17:04 UTC
- Fill window: 2026-07-10 10:33 UTC through 2026-07-19 16:03 UTC

The local hash matches the VPS source hash exactly. No production tables were
queried while generating this report.

## Data Coverage

| Dataset | Rows |
|---|---:|
| Markets | 1,277 |
| Market snapshots | 238,303 |
| Weather forecasts | 43,713 |
| Weather observations | 19,136 |
| Analyses | 72,833 |
| Model signals | 128,941 |
| Resolved model signals | 103,203 |
| Live order intents | 445 |
| Reconciled fills | 134 |
| Resolution audits | 1,570 |

## Method

The report deliberately reuses production-owned calculations:

1. Verified realized PnL uses the existing roundtrip-to-intent-to-order-to-fill
   linkage and fee-aware calculation from `cockpit_service.py`.
2. Model Brier scores and signal hit rates use `CalibrationService`.
3. Entry horizon uses each market's settlement timezone and target local date.
4. Event Top-1 accuracy takes one probability distribution per city/date and
   checks whether the highest-probability bucket was the resolved winner.
5. Fixed-cutoff checks use the latest signal available by local D1 midnight,
   D0 noon, or D0 15:00. Later information is never used in an earlier cutoff.

## Verified Trading Results

Only closed, fill-linked campaigns are included in realized PnL.

| Metric | Result |
|---|---:|
| Closed markets | 32 |
| Profitable / losing / flat | 15 / 16 / 1 |
| Win rate, excluding flat | 48.39% |
| Gross profit | +10.7833 USDC |
| Gross loss | -9.9889 USDC |
| Fees | 3.1201 USDC |
| Verified realized PnL | **+0.7944 USDC** |
| Profit factor | 1.0795 |
| Median market PnL | -0.0135 USDC |
| Maximum chronological drawdown | 4.4603 USDC |

Open campaigns have an estimated mark of +0.9078 USDC in this snapshot. That
number is not realized and is excluded from every result above.

### By Entry Model Version

The nearest persisted analysis at or before each first live BUY is used to
identify the entry model cohort.

| Entry model | Markets | Wins | Realized PnL |
|---|---:|---:|---:|
| `global-temp-bucket-multimodel-v6` | 11 | 7 | **+3.4486** |
| `global-temp-bucket-multimodel-v5` | 13 | 4 | -1.4662 |
| `global-temp-bucket-normal-v1` | 8 | 4 | -1.1880 |

The v6 result is encouraging, but 11 closed markets are not enough to establish
stable profitability.

### By Entry Horizon

| Local entry horizon | Markets | Wins | Realized PnL |
|---|---:|---:|---:|
| D2 | 9 | 7 | **+3.2063** |
| D1 | 10 | 2 | **-3.5683** |
| D0 | 13 | 6 | +1.1563 |

D1 is the weak cohort in this sample. It should be monitored as a separate
out-of-sample cohort; ten trades are too few to justify a permanent policy
change by themselves.

## Forecast Selection Quality

Per-bucket hit rate is not sufficient: a weather event has many losing buckets,
so predicting NO repeatedly can produce a high hit rate without selecting the
winner. Event-level Top-1 is the more relevant measure.

### Latest Resolved Snapshot

| Model | Events | Top-1 | Top-2 |
|---|---:|---:|---:|
| v6 multi-model | 27 | **77.78%** | 85.19% |
| v5 multi-model | 33 | 36.36% | 66.67% |
| LLM weather vote | 39 | 51.28% | 61.54% |
| Open-Meteo normal v1 | 59 | 25.42% | 45.76% |

The latest resolved snapshot may contain late-D0 observations, so it measures
final selection quality rather than entry-time tradability.

### Point-in-Time Cutoffs

| Model and cutoff | Events | Top-1 | Top-2 |
|---|---:|---:|---:|
| v6 at D1 midnight | 16 | 18.75% | 62.50% |
| v6 at D0 noon | 22 | 54.55% | 77.27% |
| v6 at D0 15:00 | 22 | 54.55% | 81.82% |
| v5 at D1 midnight | 28 | 28.57% | 64.29% |
| v5 at D0 noon | 33 | 30.30% | 60.61% |
| v5 at D0 15:00 | 33 | 33.33% | 63.64% |

This supports the value of D0 observation conditioning and shows a material v6
improvement. It does not prove that every Top-1 bucket offered positive net
edge at its contemporaneous ask.

## Conclusions

1. The complete observed trading period is slightly profitable after fees, but
   gross wins and losses nearly cancel. The edge is not yet robust.
2. v6 is the only profitable entry cohort in the closed-campaign sample and is
   materially better than v5 in event-level selection.
3. D0 observation conditioning is adding useful information. Its later Top-2
   accuracy is substantially higher than the pre-D0 snapshot.
4. D1 entries are the main economic weakness in this sample. Keep them visible
   as a separate cohort before changing thresholds.
5. Signal hit rates around 90% must never be shown as winner-selection accuracy.
   The event-level Top-1 measure is much lower and more honest.

## Limitations

- The snapshot covers roughly eight days, including several strategy releases
  and bug fixes.
- Historical full-depth order books are not available for every decision time.
- Current code cannot reconstruct every upstream API response from older runs.
- Some newest markets were unresolved when the snapshot was created.
- A closed trading campaign can profit by exiting before settlement even when
  its bought bucket ultimately resolves NO; realized trading PnL and forecast
  accuracy are intentionally reported separately.

## Next Replay Slice

Do not add a second strategy engine. Extend the existing persistence boundary
so each forecast revision retains the exact model distributions, D0 observation
state, token-specific BBO/depth, fee quote, and strategy version needed to call
the production pricing and exit functions deterministically. Then evaluate v6+
cohorts out of sample by city, D2/D1/D0 horizon, entry price band, and exit
reason. A useful promotion gate is at least 50 closed v6 campaigns across 20 or
more city/date events before raising risk from evidence alone.
