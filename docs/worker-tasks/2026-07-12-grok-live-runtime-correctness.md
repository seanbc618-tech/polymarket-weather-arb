# Grok 4.5 Task: Full-Live Runtime And Economic Correctness

## Dispatch Order

You are the **primary implementation worker** for this incident. Start from
`main` at or after commit `3253840`. Finish, commit, and push this task before the
Gemini task begins. Do not force-push or rewrite shared history.

Before planning or editing, read:

1. `AGENTS.md`
2. `CLAUDE.md`
3. `docs/agent-worker-standards.md`
4. `docs/runbooks/full-auto-micro-live.md`

Run `git status --short` first. Preserve the pre-existing untracked
`audit_report.md`. Do not touch real exchange state and do not run a real BUY,
SELL, or cancellation.

## Objective

Correct the defects found during the July 11-12 full-live run so the existing
Autopilot loop selects executable markets, records net economics accurately, and
keeps BUY/SELL lifecycle state truthful.

This task must extend the existing owners. Expected new services, schedulers,
execution adapters, database tables, and product modes: **zero**.

## Incident Evidence To Reproduce In Tests

The stopped run produced 83 decisions:

- 38 `order book is stale` rejections;
- 32 failures for `forecast does not include target day 2026-07-11`;
- 7 reconciliation fail-stop cycles;
- 3 submitted live BUY decisions;
- 2 transient geoblock failures;
- 1 SDK rejection because `$0.9996` was below the `$1` marketable BUY minimum.

Concrete ledger defects:

- closed Seoul market `2854442` remained `dry_run_ready` and monopolized selection;
- intent `53` remained `submitted` although its only attempt failed;
- Qingdao `2867214` sold only `8.48` of `10`, leaving `1.52` shares and five
  orphan `pending` auto-exit actions;
- weather markets reported `feesEnabled=true`, `feeType=weather_fees`, but all
  reconciled `fills.fee` values were zero despite balance movements showing fees.

Use synthetic fixtures based on these facts. Do not depend on the user's live DB.

## Mandatory Reuse Map

- `Repository.best_weather_candidate_by_edge()` and existing candidate queries:
  eligibility filtering and fill persistence.
- `DiscoveryService` and existing market/candidate upserts: lifecycle updates.
- `AutopilotService._select_market()` / `_prepare_market()`: selection and fresh
  quote preparation.
- `TradingService`: the only automatic/live BUY submission path.
- `PositionExitService`: the only SELL submission path.
- `AutoExitService`: automatic exit orchestration only.
- `ReconciliationService`: exchange truth and the only normal fill ingestion path.
- Existing pricing/analysis helpers: fee-aware executable Edge.

Do not add a fee service, lifecycle controller, candidate engine, or second order
preflight path.

## Slice 1: Closed And Expired Candidate Lifecycle

1. Make candidate selection exclude markets when any authoritative evidence says
   they cannot accept a new order:
   - Gamma/raw market `closed=true`;
   - `acceptingOrders=false`;
   - close/end time is in the past;
   - parsed target date is before the current relevant day.
2. Apply the same eligibility policy to both `best_weather_candidate_by_edge()`
   and fallback/ranked selection. Extract one small shared policy/helper if needed;
   do not duplicate loosely different checks.
3. During discovery/upsert, transition an existing candidate out of
   `dry_run_ready` when the latest market payload is closed/expired. Reuse an
   existing terminal status if one exists. If no suitable status exists, use one
   clearly named status without adding a table or state machine.
4. Preserve historical analyses and audit rows. Never delete a closed market to
   make it disappear from selection.
5. Before a live execution decision, obtain a fresh token-specific book even when
   an old snapshot exists. Persist it through the existing snapshot API. A failed
   refresh must produce a concrete rejection and must not fall back to a stale
   snapshot.
6. Current/future open markets must continue to rank by executable net Edge.

## Slice 2: Weather Fee Accounting And Net Edge

1. Inspect the installed official client and current Polymarket fee documentation
   before coding. Determine the exact weather fee formula, exponent behavior, and
   maker/taker applicability. Record the source and assumptions in the completion
   report; do not guess from field names alone.
2. Reuse each market's persisted raw payload (`feesEnabled`, `feeType`,
   `feeSchedule`) and the reconciled trade payload/account leg to determine role.
3. Persist the actual or deterministically computed account fee in `fills.fee`.
   Preserve the raw payload and enough role/schedule evidence for audit. Repeated
   reconciliation must remain idempotent and may correct an earlier zero fee.
4. Maker fills that genuinely have no fee must remain zero. Unknown role or
   incomplete fee metadata must be surfaced as unknown/conservative, not silently
   treated as free.
5. Update the existing quantitative analysis/pricing path so live ranking and
   execution use **net executable Edge** after expected entry fee and a
   conservative exit-fee assumption. Keep gross probability/market-price fields
   visible; add no strategy engine.
6. Verified realized PnL must automatically become correct by consuming the fixed
   `fills.fee` ledger. Do not create a PnL table.
7. Add an offline correction/backfill path only if the existing reconciliation
   upsert cannot safely repair prior zero-fee rows. Prefer reconciliation repair;
   do not create a one-off ledger outside `Repository`.

## Slice 3: Order Preflight And Truthful Intent State

1. Before calling the SDK, validate/normalize tick size, size precision,
   `orderMinSize`, and marketable minimum notional from existing market/book
   metadata.
2. Quantization must not turn an accepted configured amount into `$0.9996` when
   the exchange minimum is `$1`. If satisfying the minimum would exceed the
   configured order cap, reject locally with a clear reason; never loosen the cap.
3. Keep an auditable rejected/failed intent and attempt using existing tables.
4. If `place_limit_order()` raises, the corresponding intent must transition from
   `submitted` to `failed` (or the existing precise terminal status). A failed
   attempt must never leave a duplicate-blocking active intent.
5. Preserve exchange-accepted durability: once the exchange accepts an order, a
   later roundtrip or verification failure must not erase the submitted audit.

## Slice 4: Partial Exit, Dust, And Action Terminality

1. Continue to reconcile partial SELL fills normally. Never infer that an intended
   size was fully sold.
2. Before creating/submitting a follow-up auto-exit, compare the actual reconciled
   residual with exchange `orderMinSize`, valid precision, available bid, and
   configured limits.
3. A residual that cannot be sold because it is below exchange minimum is
   `dust/residual`, not an endlessly retryable failure. Surface it in existing
   position/exit output and skip creating a new action each tick.
4. Every created auto-exit action must reach a truthful terminal or active state.
   Any exception after action creation must append an audit event and mark the
   action failed/skipped with the concrete recovery reason. No orphan `pending`
   actions are allowed.
5. Add idempotent suppression so the same residual and unchanged quote/analysis
   cannot create another action every tick.
6. Keep `PositionExitService` as the only SELL mutation path. Do not implement a
   direct CLOB SELL inside `AutoExitService`.

## Required Offline Tests

At minimum prove:

1. a closed high-Edge market cannot beat an open lower-Edge market;
2. `acceptingOrders=false`, past close time, and past target date are excluded;
3. discovery transitions a previously ready candidate without deleting history;
4. a pre-existing stale snapshot is refreshed before live evaluation;
5. refresh failure never executes using the stale snapshot;
6. weather taker BUY and SELL fees match the official formula;
7. maker fee, unknown role, repeated reconciliation, and zero-fee correction;
8. Verified PnL uses corrected fees and net Edge is fee-aware;
9. `$0.9996` minimum-order regression is rejected or normalized before SDK call;
10. SDK failure makes both attempt and intent non-active/failed;
11. accepted exchange orders retain durable submitted audit if later work fails;
12. partial SELL leaves the exact reconciled residual;
13. below-minimum residual creates no repeated live SELL and no pending action;
14. exceptions after action creation leave a terminal failed/skipped action;
15. repeated ticks are idempotent for the same residual.

Use mocked HTTP/SDK mutations only. Do not read or modify `.env` or the live DB.

## Commits And Completion Gate

Keep objectives separable, preferably as these commits:

1. closed/expired selection and fresh quote;
2. weather fee ledger and net Edge;
3. order preflight and truthful intent state;
4. partial-exit dust/action lifecycle.

Before reporting completion run:

```bash
uv run ruff check src/ tests/
MAX_ORDER_USDC=1 MAX_DAILY_USDC=5 MAX_MARKET_USDC=2 uv run pytest -q
git diff --check
git status --short
```

Push normally to `main`. Do not use `git push --force`.

The report must follow `docs/agent-worker-standards.md` and include the exact fee
formula/source, files reused, any new abstraction with justification, removed
duplicate code, tests/results, commit hashes, remaining worktree state, and the
explicit statement that no real trading mutation ran.

