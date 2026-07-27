---
reviewer: codex
kind: implementation-review
window_start: 2026-07-24
window_end: 2026-07-25T13:29:40+08:00
verdict: PASS_FULL_LIVE_RUNTIME_ACCEPTANCE
live_verdict: FULL_LIVE_ARMED_2_USDC_SETTLEMENT_ONLY
real_trading_mutation: autonomous_paths_armed_no_new_order_at_acceptance
---

# Weather Settlement Core V5 — implementation and deployment

## Outcome

Weather V5 is implemented, tested, pushed, and deployed. The original
2026-07-24 shadow-only verdict and subsequent micro-live cohort were superseded
by explicit full-live approval on 2026-07-25. Production is now running
`full_live` with:

- `TRADING_DISABLED=false`;
- effective daemon command `autopilot start --full-auto`;
- an open full-live whitelist and no legacy global strategy override;
- `MAX_ORDER_USDC=2`, `MAX_DAILY_USDC=100`, and `MAX_MARKET_USDC=10`;
- `AUTO_EXIT_ENABLED=true`;
- research visibility floor is `MIN_EDGE=0.08`;
- code-level live entry floor remains `0.10`.
- exit policy is `weather-exit-v3-settlement-only`: no model-only SELL;
- full-live automatic winner redemption uses the existing capital pulse and
  official SDK, gated by the actual wallet path and durable no-replay audit.

Automatic BUY therefore uses the existing
`AutopilotService -> TradingService` path, while automatic SELL uses the
settlement-core `ExitGuardianService -> AutoExitService ->
PositionExitService` path. This is operational approval for full-live
measurement, not evidence of profitability. Maker-first execution remains a
separate V5b experiment.

## Policy delivered

- Entry policy: `weather-entry-v5`.
- Exit policy: `weather-exit-v3-settlement-only`.
- Model: `global-temp-bucket-multimodel-v8`.
- Every small weather position is a 100% settlement core.
- Profit recovery, principal recovery, dust liquidation, and a direction change
  alone cannot sell the core.
- Official settlement-grade impossibility may recommend a full exit.
- Model reversal, negative hold edge, D0/TAF contradiction, repeated forecast
  revisions, and executable value dominance never authorize a SELL.
- Missing, stale, or incomplete evidence holds.
- Only settlement-grade official impossibility is currently an automatic
  strategy SELL signal. Explicit contract/data/system emergency paths remain
  narrower risk exceptions; ambiguity alone holds.
- Live entry requires edge at least `0.10`, ask at least `0.05`, a non-D0
  horizon, and no prior accepted live BUY in the same city/date event.
- A prior accepted BUY freezes scale-in, re-entry, and sibling rotation for the
  event.
- In `full_live`, legacy V4/V8 calibration remains recorded as shadow telemetry
  but no longer multiplies an otherwise accepted V5 order below the exchange
  minimum. Current V5 gates and configured hard caps determine executable
  headroom.
- Candidate refresh scans up to 5,000 current weather buckets before applying
  the existing fair city/date rotation. This removes the former 300-row
  recently-updated bias without creating a second scheduler or order path.
- Cached repricing now reserves one of three slots for held/open events and two
  for new opportunities. Capital maintenance and auto-exit still refresh held
  positions independently every three minutes.
- A resolved winner may be redeemed only after successful reconciliation plus
  a fresh official Polymarket winner check. The service writes `prepared`,
  `submitted`, and `redeemed`/`submitted_unverified` into the existing
  `autopilot_decisions` ledger and never automatically replays an ambiguous
  transaction.
- Deposit Wallet redemption requires the complete `BUILDER_API_KEY`,
  `BUILDER_SECRET`, and `BUILDER_PASS_PHRASE` triple. Order credentials alone
  do not open this gate.

## Ownership and reuse

No parallel engine, service, table, or order path was added:

- `ExitGuardianService -> AutoExitService -> PositionExitService` remains the
  only automatic SELL chain.
- `AutopilotService -> TradingService` remains the only automatic BUY chain.
- `Repository` and the existing SQLite schema remain the persistence boundary.
- `GammaPolymarketClient` remains the authenticated wallet adapter and now owns
  the official SDK redemption call; no second scheduler or redeem service was
  introduced.

## Replay evidence

The read-only production replay used exact intent-attempt-fill linkage and
entry-time V8 analyses:

| Path | Resolved set | PnL |
| --- | ---: | ---: |
| Actual V4 cash path | 13 markets | `-$0.410063` |
| All V4 held to settlement | 13 markets | `+$11.935137` |
| Old exits on V5-selected shares | 6 events | `+$0.140727` |
| V5 entry plus settlement core | 6 events | `+$13.558520` |

The six-event counterfactual is directional evidence, not profitability proof.
It remains below the required 20 newly resolved real V5 events.

## Verification

- Local Ruff: passed.
- Local full pytest after full-live sizing and universe rotation:
  `1055 passed, 1 skipped`.
- VPS Ruff before release: passed.
- VPS full pytest for the main V5 release: `1050 passed, 1 skipped`.
- VPS focused pulse suite after the runtime correction: `33 passed`.
- VPS focused full-live/pulse verification for the latest release: `89 passed`.
- VPS focused opportunity-rotation verification after the final change:
  `3 passed`.
- `git diff --check`: passed.
- Existing skipped test was not introduced by V5.

## Git and deployment

- Production start HEAD: `05511a6`.
- Existing local documentation commit included in the fast-forward:
  `a18dbdf`.
- Main V5 commit: `e9334c9`.
- Shadow scheduler correction: `ca8ba75`.
- Deployment evidence commit: `4027280`.
- Full-live 2 USDC sizing and complete candidate scan: `8c736ce`.
- New-opportunity reprice priority: `7e3c9be`.
- V5.1 settlement-only exit and guarded automatic redemption:
  `dee37728c4997f216148af4ddf9863ae43857279`.
- VPS worktree after deployment: clean.

The desktop sandbox denied writes to the original repository's `.git`
directory, so the commits were created and pushed from a clean verified release
clone. The original working files were byte-compared with final GitHub main and
match, but its local HEAD remains `a18dbdf`, so it displays the release files as
modified/untracked. Existing untracked review and worker-task files were
preserved and no local cleanup or reset was attempted.

The production database and environment were backed up before pulling:

- `/opt/polymarket-weather-arb/data/backups/polymarket_weather-pre-v5-20260724T115240Z.db`
  (`2522914816` bytes, SQLite `integrity_check=ok`);
- `/etc/polymarket-weather-arb.env.pre-v5-20260724T115240Z.bak`.

## Initial shadow runtime correction

The installed unit originally invoked `--full-auto`. The CLI correctly refused
that command while `TRADING_DISABLED=true`, so the failed start was stopped
without weakening the lock. A systemd drop-in now overrides only `ExecStart`:

`/etc/systemd/system/polymarket-weather-autopilot.service.d/10-v5-shadow.conf`

It started `autopilot start --host 127.0.0.1 --port 8765 --live`. This retained
authenticated read-only reconciliation while `TRADING_DISABLED=true` blocks
BUY, SELL, cancel, and automatic exits before execution.

That first shadow run also exposed a pulse bug: an execution blocker backed off
the capital clock but left the exit clock due, repeatedly reconciling and
starving slow refresh. Commit `ca8ba75` backs off both clocks and adds a
regression test. After deployment, production completed one normal
reconciliation and then a slow refresh with 55 analyses.

## Shadow acceptance checkpoint (2026-07-24)

- Service: active, final restart counter `0`.
- HTTP root: `302`; `/app`: `200`.
- Runtime mode: `live / micro_live`, globally execution-disabled.
- Latest observed reconciliation: `5988 / ok`, zero new fills and zero open
  orders.
- Circuit breaker: clear.
- New analyses after restart: 94, including the first 55-analysis slow refresh.
- Baseline versus checkpoint order intent cursor: `637 -> 637`.
- Baseline versus checkpoint fill cursor: `306 -> 306`.
- No ERROR/CRITICAL/Traceback/Exception log lines after the final start.
- Manual BUY, SELL, cancel, redeem, or reconcile: not executed.

## Micro-live activation checkpoint (2026-07-25)

The later explicit micro-live approval superseded the shadow execution lock.
Before arming production, the live BUY submission audit was strengthened so an
exchange response is accepted only when it is a mapping, does not report
`ok=false`, and contains `order_id`, `orderID`, or `id`. Rejected or malformed
responses fail the persisted attempt and intent without retrying or recording a
ghost submitted order.

- P0 correction commit: `3d0cd6a`.
- Local verification: `1053 passed, 1 skipped`; Ruff and
  `git diff --check` passed.
- VPS focused verification in an isolated dev environment: `50 passed`; Ruff
  and `git diff --check` passed.
- Production HEAD: `3d0cd6a7b29c35385729621a725075fb6186e777`.
- Service: `active`, PID `69029`, restart counter `0`.
- Runtime: `live / micro_live`, tick interval `300s`,
  `auto_exit=micro-live`.
- Global override row `51`: `market=*`, profile `micro-live`,
  `live_auto_enabled=1`.
- Effective micro-live limits are at most `$4` per order, `$10` per day, and
  `$5` per market, with entry edge at least `0.10`.
- Post-arm reconciliation `6099`: `ok`, no open orders, 13 positions, no new
  fills.
- Circuit breaker: clear.
- Post-arm capital maintenance completed `ok`. The exit guardian safely
  deferred one legacy position because no best bid existed; it did not create a
  SELL attempt.
- Acceptance-window cursors remained intent `637`, attempt `436`, fill `306`,
  open orders `0`. No qualifying V5 entry or settlement-core exit appeared in
  that window.
- Manual BUY, SELL, cancel, redeem, or reconcile was not executed. Future
  qualifying BUY and SELL actions are authorized only through the running
  autonomous paths and their persisted gates.

## Full-live activation checkpoint (2026-07-25)

The operator subsequently approved direct V5 full-live execution and set the
per-order hard limit to 2 USDC. The deployment removed the active shadow
override, so the base systemd unit now supplies `--full-auto`.

- Production HEAD: `7e3c9bea77e644a0247c78050d0f4b51ab83d4ec`.
- Service: `active`, PID `77479`, restart counter `0`; `/app` returned `200`.
- Runtime: `live / full_live`, tick interval `300s`, `auto_exit=full-live`,
  whitelist open.
- Effective limits: 2 USDC/order, 100 USDC/day, 10 USDC/market; V5 live edge
  remains at least `0.10`.
- Environment backup:
  `/etc/polymarket-weather-arb.env.pre-full-live-20260725T034417Z.bak`.
- Disabled shadow drop-in:
  `/etc/systemd/system/polymarket-weather-autopilot.service.d/10-v5-shadow.conf.disabled-20260725T034417Z`.
- Post-deploy reconciliations through `6284` were `ok`; circuit breaker
  remained clear.
- The first new slow refresh logged `scanned_buckets=1199` and
  `universe_groups=109`, compared with the removed 300-row input cap.
- Taipei market `3076705` with edge `0.16101` passed candidate sizing and
  reached final live quote validation. Submission was correctly rejected
  before order creation because sibling market `3076700` had a stale book.
- After one held-event slot, the next cached-reprice slot moved to the new
  Ankara 2026-07-26 event, confirming the new-opportunity rotation in
  production.
- Acceptance cursors remained intent `637`, attempt `436`, fill `306`, open
  orders `0`. The running process is authorized to create future BUY/SELL
  actions autonomously; no manual order was forced during deployment.
- Final post-restart checks found zero ERROR/CRITICAL/Traceback/Exception log
  lines.

## Settlement-only and auto-redeem checkpoint (2026-07-25)

The operator explicitly approved holding through settlement, full-live
`2 USDC` orders within a `100 USDC` daily cap, an open all-weather universe,
continued BUY under losses without disabling safety gates, an account-funds
checkpoint after 20 settled V5 events, and automatic winner redemption.

- Runtime code commit:
  `dee37728c4997f216148af4ddf9863ae43857279`.
- Local full suite: `1063 passed, 1 skipped`; Ruff and `git diff --check`
  passed.
- VPS full suite: `1063 passed, 1 skipped`; VPS Ruff passed.
- Production service: `active`, PID `80707`, restart counter `0`; `/app`
  returned `200`.
- Runtime state after restart: `live / full_live`, `last_error=NULL`,
  `TRADING_DISABLED=false`.
- Effective limits: `2 USDC` per order, `100 USDC` per day,
  `10 USDC` per market, research `MIN_EDGE=0.08`, code live edge `0.10`.
- Exit policy loaded by production:
  `weather-exit-v3-settlement-only`.
- Post-restart reconciliation `6310` was `ok`; circuit breaker was clear.
- Production wallet type is `DEPOSIT_WALLET`. One official Builder credential
  triple was generated and stored in the root-owned environment file without
  exposing secret material. Readiness was
  `gasless-builder-ready`.
- There were no cached redeemable winners and no `auto_redeem` decision during
  acceptance. No manual BUY, SELL, cancel, redeem, or reconcile was executed.
- Pre/post acceptance cursors stayed at order intent `639`, order attempt
  `437`, and fill `307`; 14 non-zero positions remained. The only decision
  cursor movement was normal autonomous maintenance `18692 -> 18693`.
- The full-live whitelist is open. The first new-process slow refresh scanned
  `1,311` weather buckets across `120` city/date groups, selected `11` groups
  for this batch, and left `902` buckets in the fair rotation backlog. The
  selected batch is not a fixed universe.
- V5.1 cohort cash baseline at the checkpoint is `81.926584 USDC`, with
  intent/attempt/fill cursors `639/437/307`. After 20 newly settled V5 events
  and all eligible winner redemptions, compare reconciled collateral against
  this baseline and adjust for external deposits or withdrawals.
- Recovery backups:
  `/opt/polymarket-weather-arb/backups/pre-v5-1-dee3772-20260725T052336Z.sqlite`
  and
  `/opt/polymarket-weather-arb/backups/pre-v5-1-dee3772-20260725T052336Z.env`.

## Remaining evidence gate

Full-live is operationally armed by explicit approval, but profitability remains
unverified. At 20 newly settled real V5 events, the operator's primary pass
condition is positive reconciled account-funds change from the recorded
baseline after eligible redemptions and external-flow adjustment. Continue to
report fee-adjusted event-level net EV, drawdown, and settlement-core retention
as diagnostics; one positive checkpoint is not proof of stable future profit.
Maker-first must still measure post-only fill rate, time-to-fill, and post-fill
markout before separate deployment approval.
