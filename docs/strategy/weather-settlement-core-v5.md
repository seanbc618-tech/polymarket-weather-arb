# Weather Settlement Core V5

## Status and ownership

This policy is the live entry/position-management overlay for
`global-temp-bucket-multimodel-v8`.

- Entry policy: `weather-entry-v5`
- Exit policy: `weather-exit-v3-settlement-only`
- BUY execution remains owned by `TradingService`.
- SELL execution remains owned by `PositionExitService`.
- `ExitGuardianService` recommends; `AutoExitService` only orchestrates.

It supersedes the V4 profit-protection ladder. It does not claim guaranteed
profit.

## Objective

Optimize fee-adjusted event-level settlement value, not the percentage of
campaigns closed for a small cash profit. The production review found that the
old path frequently sold eventual winners early while recording a high
cash-path win rate.

## V5 live entry policy

The quantitative V8 model remains unchanged. V5 adds final live-only gates:

1. Conservative net edge must be at least `0.10`.
2. Executable ask must be at least `0.05`.
3. D0 entries are paused until official-observation/trajectory-lock evidence is
   accepted out of sample.
4. A prior accepted live BUY in the same city/date bucket event blocks any
   scale-in, re-entry, or sibling-bucket entry.
5. Existing reconciliation, circuit-breaker, freshness, idempotency, event
   exposure, and hard risk caps still apply.

Production analysis may use `MIN_EDGE=0.08` so `0.08-0.10` opportunities remain
visible as shadow evidence. The live boundary independently enforces `0.10`.
The sub-`0.05` and D0 bans are release gates, not claims that those opportunity
classes are permanently unprofitable.

## Settlement core

At the current small order sizes, the entire reconciled position is the core.
The policy does not create a dust-sized core/satellite split.

The following are not SELL reasons:

- a small mark-to-market profit;
- recovery of original principal;
- a residual smaller than exchange minimum size;
- entry edge falling below the entry threshold;
- any model direction reversal, however many forecast revisions confirm it;
- an apparently attractive executable SELL bid or negative model hold edge;
- stale, missing, or incomplete forecast evidence.

These states recommend holding the full position for resolution.

## Permitted automatic full exits

### Official impossibility

A reliable settlement-grade observation that irreversibly invalidates the held
bucket may recommend `exit_full`. A reliable observation that locks the bucket
recommends `hold_for_resolution`.

No model-only exit exists during the 2 USDC V5 validation cohort. D0 hourly
contradiction, calibrated TAF conflict, negative hold edge, two or more model
reversals, and executable value dominance are telemetry only and always HOLD.

A market-rule/data-source/contract abnormality or system-level emergency may
justify a risk exit, but only through an explicit evidence-backed guardian path.
Ambiguity itself is not a bearish signal and cannot manufacture a SELL.

## Settlement and automatic redemption

A closed position is never sold. After a successful account reconciliation,
full-live may automatically redeem one winner per capital pulse only when all
of these facts agree:

1. the reconciled account still holds a non-zero position;
2. a fresh official Polymarket market response says `closed`, `resolved`, and
   identifies exactly one winning YES/NO outcome;
3. the held outcome equals that winner and the response contains `conditionId`;
4. the circuit breaker, live credentials, and execution gates are clear;
5. the actual funder wallet has a supported official SDK transaction path.

For a gasless Deposit Wallet, the complete `BUILDER_API_KEY`,
`BUILDER_SECRET`, and `BUILDER_PASS_PHRASE` triple is required. The service
writes a durable `auto_redeem` decision before submitting. A submitted or
ambiguous transaction is never automatically replayed; reconciliation and
operator review must resolve it.

## Replay ledger

Every evaluation must keep these ledgers separate:

- actual fee-adjusted BUY cash and SELL net;
- remaining inventory at executable bid and at final settlement;
- static hold-to-settlement counterfactual;
- V5 settlement-core counterfactual;
- event identity (`city + target date`) rather than market ID alone.

Replay must be chronological and must reapply bankroll, order, market/event,
and daily caps. It may not assume that capital freed by an old early SELL was
also available in a hold-to-settlement path. Horizon and probability must come
from the analysis available at that timestamp; final outcomes may only score
the result.

## Rollout gate

1. Historical A/B/C replay and regression cases must pass.
2. Full-live uses the operator-approved hard caps: `2 USDC` per order and
   `100 USDC` per day. Existing reconciliation, compliance, breaker, and
   hard-risk fail-stops remain mandatory.
3. Scan and rank all persisted eligible weather buckets, but allow only the
   best eligible bucket to receive the event's single accepted BUY.
4. At 20 newly settled real V5 events, finish all eligible winner redemptions
   and compare reconciled account funds with the cohort baseline, adjusted for
   external deposits/withdrawals. The operator's pass condition is a positive
   account-funds change; it is a checkpoint, not a guarantee of future profit.

Maker-first execution is a separate V5b experiment. It must measure post-only
fill rate, time-to-fill, and post-fill markout before any taker fallback is
introduced.
