# Profit Protection Exit Strategy Plan

## Status And Dependency

Design approved by the operator. Do not implement or enable live behavior until
the July 12 runtime-correctness acceptance blockers are fixed and Codex has
accepted the corrected baseline.

This plan improves realized trading returns. It must extend the existing exit
path instead of creating another controller:

- `ExitGuardianService`: decides hold, recover principal, or fully exit.
- `AutoExitService`: orchestrates at most the configured number of exits.
- `PositionExitService`: remains the only live SELL mutation path.
- `ReconciliationService` and existing fills/positions/intents: exchange truth.
- Existing forecasts, analyses, settlement state, and fee helpers: valuation.

Expected new services, execution adapters, schedulers, and database tables:
**zero**. Any proposed schema migration requires Codex review before editing.

## Objective

Replace the current binary `hold_position` / `position_at_risk -> sell all`
policy with a position-aware exit ladder that can protect principal while
preserving high-upside weather positions:

1. recover principal when executable proceeds can cover verified cost;
2. hold the remaining runner while the model and official forecast support it;
3. fully exit when net probability advantage disappears or reverses;
4. make a separate decision near observation/settlement using time, official
   data, liquidity, and maximum realizable value.

Do not use headline ROI alone. A move from `0.01` to `0.02` is not an automatic
sell when fair probability remains much higher.

## Accounting Preconditions

All decisions must use reconciled, token-specific evidence:

- exact BUY fills and BUY fees for the currently held outcome;
- exact prior SELL fills and SELL fees;
- current reconciled position size;
- fresh executable best bid and available bid depth;
- expected taker fee from the accepted weather fee helper;
- no unrelated historical roundtrip fills.

Derive remaining unrecovered cost as:

```text
remaining_cost = verified_buy_cost_with_fees
               - verified_net_sell_proceeds
```

Never substitute order intent notional, current position value, or an unfilled
order for verified cost basis. If cost linkage is incomplete, fall back to the
existing conservative full-exit/hold policy and explain the missing evidence.

## Stage 1: Recover Principal

Emit `recover_principal` only when all conditions hold:

1. latest forecast/analysis is fresh and still supports the held outcome;
2. latest net Edge remains positive after expected exit fee;
3. fresh bid depth can execute enough shares to recover `remaining_cost`;
4. the proposed partial SELL respects tick, minimum size, slippage, and depth;
5. the residual remains either zero or above exchange minimum size;
6. no active SELL intent/order already exists for the token.

Solve the minimum SELL quantity using executable proceeds, including the
price-dependent fee and quantity precision. Round upward to recover principal,
but never exceed reconciled position size or available protected depth.

Submit that exact partial size through `PositionExitService.close_live()`. Persist
the rationale and verified calculation in the existing intent/action audit.

Do not repeat principal recovery after cumulative verified net SELL proceeds have
already covered verified BUY cost.

## Stage 2: Hold The Runner

After principal is verified as recovered, classify the remaining position as
`runner` and hold it while:

- model direction still matches the held outcome;
- net Edge remains above the normal hold threshold;
- forecast and quote are fresh;
- no material settlement-rule or data-source warning exists;
- market remains open, or the position is intentionally held for settlement.

Runner status must be derived from fills and current position, not from an
unverified local boolean. Reconciliation corrections must automatically change
the classification.

The UI must show original cost, net proceeds recovered, remaining size, current
executable value, model probability, market bid, net Edge, and why the runner is
held. Do not label unrealized value as realized profit.

## Stage 3: Full Exit When Advantage Disappears

Emit `exit_full` for the actual reconciled residual when any strong condition
holds:

- quantitative decision no longer supports the held outcome;
- model direction reverses;
- fresh **net** Edge is at or below the exit threshold;
- official forecast materially contradicts the position;
- source quality degrades below the level permitted for live holding;
- executable liquidity is deteriorating and waiting has worse conservative EV;
- circuit breaker/compliance permits risk reduction but blocks new risk.

Keep stale/missing analysis fail-closed: do not sell solely because a provider
timed out. Surface `review_no_analysis` and retry data refresh. Once a full exit
is chosen, use the existing fresh-bid, slippage, no-oversell, idempotency, and
durable audit guarantees.

## Stage 4: Near-Settlement Decision

Use a distinct `near_settlement` policy window based on the market's station or
city local timezone, never UTC calendar date alone.

Within that window combine:

- time remaining until observation window closes and market resolves;
- official forecast versus observed intraday high/low;
- observation coverage and quality warnings;
- remaining outcomes still physically possible;
- best bid, depth, spread, expected fee, and maximum payout of `1.00`;
- resolution-rule confidence and source mapping.

Possible outputs:

- `hold_for_resolution`: outcome is strongly supported and selling sacrifices
  excessive expected value;
- `recover_principal`: only principal remains unrecovered and liquidity permits;
- `exit_full`: evidence has reversed or executable sale dominates settlement EV;
- `settlement_pending`: trading window is closed; stop forecasting/exiting and
  route to existing observation/resolution/redeem visibility.

No market order and no automatic redeem implementation belong in this slice.

## Policy Priority

Evaluate in this order:

1. reconciliation and execution invariants;
2. closed/resolved/settlement routing;
3. strong full-exit evidence;
4. principal recovery eligibility;
5. runner hold;
6. ordinary hold/review.

A strong model reversal must not be blocked merely because principal recovery has
not happened. A profit target must not override stale-data protections.

## Delivery Slices

1. **Read-only decision model:** enrich `ExitRecommendation` and `/app`; no SELL
   behavior change.
2. **Offline replay:** run the policy against captured July 11-12 fills, positions,
   forecasts, and books; compare with current all-or-nothing policy.
3. **Dry-run live shadow:** calculate recommendations during live ticks but do not
   submit partial exits; collect at least one full market-day of evidence.
4. **Micro-live principal recovery:** enable only partial cost recovery with the
   existing 1 USDC entry cap and one exit per tick.
5. **Full-live ladder:** enable runner/full-exit/near-settlement actions after
   reconciliation and PnL evidence pass acceptance.

Each slice must be separately revertible. Do not ship all four behaviors in one
commit.

## Required Tests

At minimum cover:

1. `0.01 x 100`, bid `0.02`: sell the fee-adjusted minimum needed to recover cost;
2. price doubled but model Edge remains high: no unconditional full take-profit;
3. prior partial SELL already recovered principal: no second recovery;
4. insufficient depth/minimum-size conflict: no invalid partial SELL;
5. principal recovery would create dust: choose valid full exit or hold explicitly;
6. model reversal before recovery: full exit takes priority;
7. stale/missing analysis: no automatic SELL based only on stale state;
8. partial fill: recompute remaining cost and residual from reconciliation;
9. maker/taker fees and fee rounding in cost recovery;
10. Chicago/Los Angeles/Asia local-day near-settlement boundaries;
11. closed market routes to settlement pending and never attempts SELL;
12. exact existing `PositionExitService` path is called once with no oversell;
13. restart/reconciliation produces the same runner/principal classification;
14. `/app` distinguishes realized recovery, unrealized runner value, and maximum
    possible payout.

All tests and replay are offline. A real SELL requires a new exact order
confirmation from the user after Codex acceptance.
