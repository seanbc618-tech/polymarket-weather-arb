# Live Weather Bucket Loss Retrospective - 2026-07-18

## Scope

This review reconstructs every closed market with a real BUY fill in the local
SQLite audit trail, then drills into the losing buckets visible in the operator's
Polymarket screenshot. Polymarket's cached resolved outcome is treated as payout
truth. Fills include recorded fees.

No live order, cancel, or exchange mutation was executed during this review.

## Portfolio Result

| Entry generation | Closed campaigns | Positive | Negative | Net PnL |
|---|---:|---:|---:|---:|
| `global-temp-bucket-normal-v1` | 25 | 4 | 21 | +68.28 USDC |
| `global-temp-bucket-multimodel-v5` | 12 | 2 | 10 | -4.33 USDC |
| `global-temp-bucket-multimodel-v6` | 3 | 0 | 3 | -3.15 USDC |
| **Total** | **40** | **6** | **34** | **+60.80 USDC** |

The +94.28 USDC Chengdu winner is valid system profit and remains in the total.
It also means the remaining closed campaigns net approximately -33.48 USDC.
The historical ledger is profitable, but repeatability is not yet demonstrated.

## Screenshot Markets

| Market | Result / state | Entry evidence | Root cause | Current disposition |
|---|---|---|---|---|
| Miami 90-91F, Jul 15 (`2916005`) | -1.23; winner 92-93F | p50 0.5003; raw disagreement 0.9032; robust dispersion 0.3399 | v5 accepted a highly split model set | Current robust-dispersion gate rejects it |
| Qingdao 25C, Jul 17 (`2930884`) | -1.05; winner 26C | Observed max was already 26C before the 0.001 BUY | Source tolerance reopened a mathematically passed bucket | Strict observed-boundary rejection now returns probability zero |
| Dallas 90-91F, Jul 16 (`2929453`) | -1.05; winner 88-89F | Merged feed selected NWS 91.4F; settlement-aligned METAR report was 89.06F | Max-merging NWS five-minute samples with METAR created false settlement evidence; late 0.001 entry ignored post-peak bucket identity | AWC/METAR is now primary, NWS fallback only; post-peak bucket and extreme-price agreement gates added |
| Atlanta 90-91F, Jul 16 (`2929463`) | -1.05; winner 92-93F | Exact station observation was already 91.94F | A one-degree source tolerance let a passed 90-91F bucket remain tradable | Strict observed-boundary rejection now blocks it |
| Chengdu 35C, Jul 16 (`2917167`) | -0.59; winner 38C | p50 0.3333; four of five models supported; no D0 observation at entry | Early v5 D0 path lacked station observation and station-centered trajectory evidence | Current D0 path requires fresh station evidence; this remains a normal forecast-risk example after those gates |
| Qingdao 29C, Jul 18 (`2942654`) | Open in screenshot; entry 0.19, later near zero | Ask 0.19 vs bid 0.06; one model at zero; observed 29C near forecast peak | Crossed a severely illiquid spread and overtrusted city/grid trajectory | Entry-spread, station-coordinate, trajectory, and post-peak gates now cover it |
| Seoul 26C, Jul 18 (`2942490`) | Correct bucket; most shares sold early | Reliable 26C observation and post-peak trajectory supported held bucket | Principal recovery and rebalance marker outranked winner evidence | D0 winner lock now outranks recovery/rebalance; rebalance marker is no longer a sell signal |

The two Miami Jul 18/19 positions in the screenshot were still unresolved at the
review cutoff and are not classified as wins or losses.

## Complete Negative Ledger

The 34 negative closed campaigns are grouped by the code generation that opened
them. This is a loss ledger, not a claim that every individual loss was avoidable.

### Legacy normal-v1

- Chengdu 33C Jul 11: -1.03
- Chicago 78-79F Jul 11: -1.02
- London 30C Jul 12: -0.25
- Qingdao 29C Jul 12: -0.07
- London 27C Jul 13: -0.59
- London 28C Jul 13: -0.03
- Seoul 34C+ Jul 13: -1.52
- Shanghai 32C Jul 13: -2.09
- Wuhan 35C Jul 13: -2.09
- Qingdao 28C Jul 13: -1.69
- New York 80-81F Jul 13: -0.42
- New York 82-83F Jul 13: -0.07
- Miami 90-91F Jul 13: -0.36
- Dallas 88-89F Jul 13: -2.10
- Dallas 90-91F Jul 13: -2.10
- Atlanta 78-79F Jul 13: -2.10
- Atlanta 80-81F Jul 13: -2.10
- Miami 84-85F Jul 13: -2.10
- Miami 86-87F Jul 13: -2.10
- London 31C Jul 14: -1.55
- Miami 90-91F Jul 14: -1.31

Dominant defects: no mandatory D0 observations, no one-bucket-per-event exposure,
and repeated 0.001 lottery entries across sibling buckets. The current execution
path has D0 evidence, event-sibling exposure, staged sizing, and fee-aware pricing.

### Multimodel v5

- Dallas 86-87F Jul 15: -1.08
- Atlanta 82-83F Jul 15: -1.00
- Miami 90-91F Jul 15: -1.23
- Chengdu 35C Jul 16: -0.59
- Atlanta 86-87F Jul 16: -0.59
- Miami 90-91F Jul 16: -0.03
- Chengdu 34C Jul 17: -0.09
- Atlanta 88-89F Jul 16: -0.94
- Miami 92-93F Jul 16: -0.09
- Atlanta 86-87F Jul 17: -0.89

Dominant defects: high model dispersion was still tradable, forecasts were not
consistently centered on the exact settlement station, and D0 trajectory evidence
was incomplete. Current v6 gates cover those structural defects, although ordinary
forecast error remains possible.

### Multimodel v6 before this review

- Atlanta 90-91F Jul 16: -1.05
- Dallas 90-91F Jul 16: -1.05
- Qingdao 25C Jul 17: -1.05

All three were avoidable D0 evidence errors. Named replay tests now cover their
specific observed temperatures and price conditions.

## Root Causes And Fixes

1. **Wrong observation aggregation.** NWS five-minute extrema and METAR reports
   were merged by maximum value even though Wunderground does not necessarily use
   every NWS five-minute sample. AWC/METAR is now the primary exact-station proxy;
   NWS is only an availability fallback for US stations.
2. **Passed buckets could be reopened by source tolerance.** Observed maxima above
   the settlement bound now produce a hard zero before model, LLM, or hourly
   overlays can run.
3. **Late D0 neighbor bets.** Once the forecast daily peak has passed, a new entry
   must be the bucket containing the settlement-aligned observed maximum. The
   strategy may hold an existing position under separate exit logic, but it cannot
   open an adjacent bucket merely because stale daily models still vote for it.
4. **Extreme 0.001 contrarian bets used robust median dispersion only.** At asks of
   0.005 or lower, every model must assign at least 0.40 probability to the bucket.
   This preserves genuinely unanimous cheap opportunities while blocking a weak
   dissenting source hidden by robust statistics.
5. **Sibling event overexposure.** Legacy runs bought two or three mutually
   exclusive buckets for the same city/day. `TradingService` now permits only one
   active live sibling market per event.
6. **Winner sold for principal recovery.** Reliable D0 winner evidence now outranks
   principal recovery and bucket-switch review. A rebalance target alone cannot
   trigger a SELL.
7. **Illiquid spread crossing.** A dynamic spread allowance now prevents crossing
   books such as Qingdao 0.06 bid / 0.19 ask.
8. **City/grid versus station bias.** Forecast providers now resolve the exact ICAO
   station coordinates, and D0 hourly trajectories anchor to current station
   observations.

## Remaining Production Gate

The system should not be called production-ready solely because the historical
ledger is net profitable. Before restoring larger sizing:

1. Run the complete test suite and retain these replay tests.
2. Run at 1 USDC maximum order size for another D0/D1 cycle.
3. Confirm every live candidate records AWC-primary provenance, post-peak bucket
   consistency, spread status, and extreme-price agreement status.
4. Require at least one resolved v6 campaign that passes the new path and matches
   Polymarket's final winner before increasing size.
