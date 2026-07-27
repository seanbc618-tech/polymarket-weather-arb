# Grok 4.5 Task: Implement Complete Profit-Protection Automatic Exit

## Read First

Start from `origin/main` at or after commit `35b6363`. Before planning or editing,
read:

1. `AGENTS.md`
2. `CLAUDE.md`
3. `docs/agent-worker-standards.md`
4. `docs/worker-tasks/2026-07-12-profit-protection-exit-strategy.md`
5. `docs/runbooks/full-auto-micro-live.md`

The worker standard is authoritative. Run `git status --short` first and preserve
the pre-existing untracked `audit_report.md`.

This is one complete assignment, but implementation must be split into separately
reviewable commits. Do not stop after a read-only model: complete the full ladder
through the existing automatic SELL path, UI, runbook, and offline tests.

Do **not** run a real BUY, SELL, cancel, redeem, backfill, or any account mutation.
All exchange mutations in tests must be mocked. Do not use `git push --force`.

## Product Objective

Upgrade the current binary exit policy:

```text
hold_position OR position_at_risk -> sell all
```

to this complete automatic ladder:

```text
reconcile
-> settlement routing
-> strong full-exit evidence
-> recover verified principal with a partial SELL
-> hold the remaining runner
-> near-settlement decision
-> execute only through PositionExitService
```

The purpose is to preserve high-upside weather positions while converting enough
profit into realized cash to protect the original stake. It does not guarantee
profit and must not use headline ROI alone.

## Mandatory Ownership And Reuse

Extend these existing owners:

- `ExitGuardianService`: all exit policy and recommendation types.
- `AutoExitService`: thin orchestration and action audit only.
- `PositionExitService`: the **only** automatic/manual SELL mutation path.
- `ReconciliationService`: exchange truth for fills, positions, orders, and fees.
- `Repository`: existing queries and ledger reads.
- `domain/fees.py`: taker fee calculation and rounding.
- `domain/order_constraints.py`: tick, size, minimum, and dust constraints.
- `domain/market_eligibility.py`: local weather day/timezone behavior.
- Existing rules, observations, forecasts, analyses, and settlement markers.
- Existing `/app` Cockpit read model and position/decision panels.
- Existing Telegram submitted/fill/material-failure events.

Expected new services, database tables, schedulers, execution adapters, and
persistent modes: **zero**.

One small pure domain helper/dataclass module is acceptable only if keeping all
inventory math in `ExitGuardianService` would make it materially harder to test.
If added, justify ownership and delete any duplicated calculation from services.

Do not create `ProfitService`, `ExitStrategyService`, another lifecycle
controller, another PnL table, or another SELL command.

## Canonical Inventory Campaign

Every recommendation must be derived from the current open position's verified
exchange history, not from intent notional or UI values.

For each `market_id + outcome/token`:

1. Read reconciled fills in chronological order.
2. Match fills to the held outcome using exact order IDs from existing intents and
   the account leg/token/outcome in raw fill payloads.
3. Ignore fills for the opposite outcome and unrelated historical order IDs.
4. Identify the current inventory campaign after the most recent verified
   zero-position crossing. Do not mix an old completed roundtrip with a later
   re-entry in the same market.
5. Compute:

```text
verified_buy_cost = sum(BUY price * size + BUY fee)
verified_sell_proceeds = sum(SELL price * size - SELL fee)
unrecovered_cash = max(0, verified_buy_cost - verified_sell_proceeds)
net_fill_size = BUY size - SELL size
```

6. Require `net_fill_size` to agree with the current reconciled position within
   the existing precision tolerance. If evidence is incomplete or disagrees,
   mark accounting `unverified` and do not perform principal-recovery SELL.
7. A model reversal may still request a normal full risk-reducing exit when cost
   accounting is incomplete, because `PositionExitService` independently verifies
   the real position and prevents oversell.

Reuse existing exact order/fill linkage from roundtrip/PnL code where possible.
Do not copy a second parser for `order_id/orderID/orderId/id`.

## Recommendation Model

Extend `ExitRecommendation` narrowly with fields needed by execution/UI, such as:

- `policy_stage`;
- `recommended_size`;
- `actual_position_size`;
- `verified_buy_cost`;
- `verified_sell_proceeds`;
- `unrecovered_cash`;
- `runner_size_after`;
- `best_bid` / executable value / expected fee;
- `accounting_verified` and evidence reason;
- `time_to_window_end` / settlement state when applicable.

Use these action names consistently:

- `recover_principal`;
- `hold_runner`;
- `exit_full`;
- `hold_for_resolution`;
- `settlement_pending`;
- existing `review_no_analysis` for stale/missing evidence.

Keep compatibility aliases only where existing tests/callers truly require them;
remove superseded `position_at_risk` branching from AutoExit once callers use the
new explicit actions.

## Policy Priority

Apply this order exactly:

1. **Settlement/closed routing**: `settlement-route-v1`, closed, resolved, or
   non-orderable markets remain `settlement_pending`; never SELL.
2. **Evidence freshness**: stale/missing analysis or quote returns
   `review_no_analysis`; provider timeout alone never causes SELL.
3. **Strong full exit**: model direction reverses, held outcome no longer has a
   quantitative trade decision, or fresh net Edge is at/below the existing exit
   threshold. Recommend the entire actual reconciled residual.
4. **Near-settlement policy**: when within the defined local observation window,
   compare official evidence, remaining physical possibility, executable price,
   fees, and maximum payout.
5. **Principal recovery**: when model support remains strong and executable net
   proceeds can recover verified unrecovered cash, recommend the minimum valid
   partial size.
6. **Runner hold**: principal already recovered and model support remains strong.
7. **Ordinary hold**: positive Edge remains but principal recovery cannot yet be
   executed safely.

A model reversal must take priority over waiting to recover principal. A fixed
profit percentage must never override model/settlement evidence.

## Slice 1: Verified Cost Basis And Read-Only Recommendations

1. Add repository/read helpers for the current market/outcome fill campaign using
   existing tables only.
2. Derive all accounting from reconciled fills and fees on every tick; do not
   persist a fragile `principal_recovered=true` boolean.
3. Build the recommendation ladder in `ExitGuardianService`.
4. Keep this commit read-only: AutoExit must not yet execute
   `recover_principal` or `exit_full` under the new model.
5. Add deterministic replay fixtures based on July 11-12 patterns:
   - `0.01 x 100` entry;
   - mixed maker/taker partial fills;
   - prior partial SELL;
   - same market with an older completed campaign;
   - missing/unmatched fill evidence.
6. Compare new recommendations against the old all-or-nothing behavior in tests
   or a test-only replay helper. Do not read the user's live DB.

Commit this slice separately before continuing.

## Slice 2: Automatic Principal Recovery And Runner

For a fresh best bid `p`, size `q`, and taker fee per share `f(p)`:

```text
net_proceeds(q) = q * p - q * f(p)
q_required = ceil_to_exchange_step(unrecovered_cash / (p - f(p)))
```

Use the accepted fee helper and its five-decimal fee rounding for the final exact
quantity/proceeds check. Do not introduce an approximate second fee formula.

Requirements:

1. Recommend/submit the minimum valid `q_required` that recovers principal after
   expected SELL fee.
2. Never exceed actual reconciled position, configured auto-exit count, valid bid
   depth when available, or PositionExit limits.
3. Respect `orderMinSize`, size step, price tick, fresh bid, and max slippage.
4. Do not create a dust residual. If partial recovery would leave nonzero dust:
   - full exit only when the full-exit policy independently applies;
   - otherwise hold and explain `principal recovery would create dust`.
5. If top-level depth is available in the existing raw book, cap the submitted
   quantity to protected depth only when that capped size still recovers principal.
   If depth metadata is unavailable, a best-bid limit order may be submitted for
   `q_required`; partial fill remains safe because it cannot execute below limit.
6. Submit partial SELL exclusively through `PositionExitService.close_live()`.
7. After partial fill/reconciliation, recompute unrecovered cash and residual.
8. Once net verified SELL proceeds cover verified BUY cost, return `hold_runner`
   and never repeat principal recovery for unchanged evidence.
9. Existing action/intent idempotency must prevent duplicate partial SELLs while
   an order is open, submitted, or unverified.

Commit this slice separately before continuing.

## Slice 3: Full Exit And Near-Settlement Policy

### Full Exit

`exit_full` submits the entire current reconciled residual through
`PositionExitService` when fresh quantitative evidence strongly invalidates the
held outcome. Preserve all existing safeguards:

- no SELL on stale/missing analysis;
- fresh reconciliation and token-specific bid;
- no oversell or naked SELL;
- limit-only execution and slippage bound;
- durable intent/attempt before post-submit verification;
- breaker/compliance/signing behavior already owned by PositionExitService;
- partial fill remains partial and is recalculated next tick.

Do not sell merely because price rose or because unrealized ROI crossed a fixed
percentage.

### Near Settlement

Define near-settlement from the parsed rule's observation window and confirmed
IANA local timezone. If the exact rule window is absent, use the local event-day
end only when timezone is confirmed. Unknown timezone remains ordinary hold/review,
not UTC inference.

Use existing official observation/forecast data only. Do not call settlement
backfill, resolution audit, LLM, or mutate observations from the exit tick.

Decision outputs:

- `hold_for_resolution`: held outcome remains strongly supported and the
  conservative expected settlement value exceeds net executable proceeds;
- `recover_principal`: evidence supports the outcome but principal remains
  unrecovered and a valid partial SELL exists;
- `exit_full`: official evidence/model direction invalidates the held outcome and
  the market remains executable;
- `settlement_pending`: trading/observation window closed or market resolving;
  never SELL.

For exact temperature buckets, respect monotonic daily-extrema logic: a daily high
already above a bucket's upper bound cannot later fall back into the bucket. Do
not claim an outcome is locked without sufficient observation coverage/quality.

Commit this slice separately before continuing.

## Slice 4: Connect Full Automation, `/app`, Telegram, And Runbook

1. `AutoExitService` executes only `recover_principal` and `exit_full`.
2. `hold_runner`, `hold_for_resolution`, `settlement_pending`, ordinary hold, and
   review actions never call SELL.
3. Keep `MAX_AUTO_EXITS_PER_TICK`; at most one action per position and no duplicate
   residual action.
4. Automatic execution is active in existing `full_live` when
   `AUTO_EXIT_ENABLED=true`. Do not add another enable flag or mode.
5. Preserve current micro-live behavior unless tests demonstrate the new ladder is
   compatible with its 1 USDC bounds. Do not silently loosen any cap.
6. Persist complete policy rationale and calculations in existing automation
   action/intent audit payloads.
7. Telegram stays material-only:
   - partial principal-recovery SELL submitted;
   - full-exit SELL submitted;
   - exchange fill;
   - submitted-unverified/material failure.
   Do not notify every runner hold or recommendation.
8. Extend the existing `/app` position/exit panel, not a new page. Show:
   - policy stage/action;
   - verified original/current-campaign cost;
   - net proceeds recovered;
   - unrecovered cash;
   - current position and runner size;
   - fresh bid, executable value, expected fee;
   - model probability, market price, net Edge;
   - maximum possible payout at `1.00`;
   - realized recovery versus unrealized runner value;
   - concise reason/recovery path when accounting is unverified.
9. Update `docs/runbooks/full-auto-micro-live.md` in place with the new policy,
   examples, stop/recovery behavior, and audit commands. Do not create another
   runbook.

Commit this slice separately.

## Mandatory Tests

At minimum cover all of these offline cases:

1. `0.01 x 100`, verified cost/fees, bid `0.02`: compute the minimum fee-adjusted
   partial SELL needed to recover principal.
2. Price doubles while net Edge stays high: recover principal only, never full exit.
3. Price rises but cannot yet recover principal: hold.
4. Prior SELL already recovered principal: `hold_runner`, no new SELL.
5. Model direction reverses before recovery: `exit_full` wins priority.
6. Net Edge crosses exit threshold: full residual SELL.
7. Stale/missing analysis or quote: no SELL.
8. Unverified/mismatched fill ledger: no partial recovery; reason visible.
9. Two campaigns in the same market: old completed fills excluded.
10. Opposite-outcome and unrelated fills excluded.
11. Maker/taker fees, rounding, and partial fills recompute correctly.
12. Insufficient depth/min size/dust residual: no invalid partial SELL.
13. Existing open/submitted SELL prevents duplicate action.
14. Exchange accepts partial SELL but verification fails: durable audit retained.
15. Partial principal SELL fills only partly: next tick recomputes exact remainder.
16. Runner never becomes `position_at_risk` solely due to realized ROI.
17. Chicago, Los Angeles, London, and Asia local near-settlement boundaries.
18. Unknown timezone does not invoke near-settlement/date inference.
19. Closed/resolved/settlement-route position never calls SELL.
20. Exact bucket already exceeded by observed daily high: correct full-exit
    recommendation only while market remains executable and evidence is reliable.
21. Poor observation coverage/quality cannot claim locked resolution.
22. Full Autopilot tick calls `PositionExitService` exactly once for
    `recover_principal` and once for a separate `exit_full` fixture.
23. Full tick sends no SELL for runner/hold/settlement/review fixtures.
24. Restart plus reconciliation derives the same campaign/recovery state.
25. `/app` renders all accounting/stage fields in English and Chinese and does not
    call unrealized runner value realized profit.
26. Telegram remains silent for hold/runner and sends material partial/full SELL.
27. Existing full-live, fee, settlement, dust, reconciliation fail-stop, and
    roundtrip tests remain green.

Do not satisfy these with isolated mocks of the method under test only. Include
repository-backed integration tests and full Autopilot tick tests through the
production owners.

## Verification And Commit Contract

After each slice run its targeted tests. Before completion run exactly:

```bash
uv run ruff check src/ tests/
MAX_ORDER_USDC=1 MAX_DAILY_USDC=5 MAX_MARKET_USDC=2 uv run pytest -q
git diff --check
git status --short
```

Expected commit sequence:

1. verified campaign accounting and recommendation model;
2. automatic principal recovery and runner behavior;
3. full exit and near-settlement policy;
4. full-live integration, `/app`, Telegram, tests, and runbook.

Push normally to `origin/main` only after all gates pass. Never force-push.

## Stop Conditions

Stop and report to Codex before continuing if:

- a new table or second service/execution path appears necessary;
- exact current-campaign fill linkage cannot be proven from existing data;
- near-settlement logic would need to mutate observations or call an LLM;
- a policy requires market orders, overselling, stale quotes, or bypassing
  reconciliation;
- implementation requires a real-money transaction;
- current database evidence cannot distinguish outcomes or multiple campaigns.

## Completion Report

Follow `docs/agent-worker-standards.md`. Include:

- objective completed per slice;
- exact existing components reused;
- campaign accounting identity and fee math;
- every new file/class/field/setting with justification;
- duplicate/superseded code removed;
- production action matrix showing which recommendations do/don't SELL;
- targeted and full test results;
- four commit hashes and final `origin/main` hash;
- remaining working-tree changes;
- unresolved risks and shadow/replay evidence;
- explicit statement whether any real trading mutation ran.
