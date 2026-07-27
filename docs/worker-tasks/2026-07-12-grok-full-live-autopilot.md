# Grok Task: Unlock And Complete `/app` Full-Live Autopilot

## Read First

Read these files before planning or editing:

1. `AGENTS.md`
2. `CLAUDE.md`
3. `docs/agent-worker-standards.md`
4. `docs/runbooks/full-auto-micro-live.md`
5. `docs/runbooks/auto-exit-enablement-checklist.md`

The worker standard is authoritative. The product objective is an autonomous
weather trader that discovers, evaluates, enters, manages, and exits positive-EV
positions. Do not add policy gates, dashboards, services, tables, or modes that do
not directly improve that loop.

Baseline for this task: `main` at or after commit `4c7a496`. Run
`git status --short` first and preserve all user work.

## Objective

Make the existing **Full live** mode in `/app` a real, selectable, executable,
unattended trading mode.

After this task, one existing command must be sufficient:

```bash
uv run polymarket-weather autopilot start --full-auto
```

That command and the equivalent `/app` controls must run this existing loop:

```text
discover -> quote/forecast/analyze -> rank -> reconcile
-> manage stale orders -> automatically exit invalid positions
-> submit the best eligible live BUY -> persist/notify -> repeat
```

This is not a request to remove capital-integrity checks. Keep idempotency,
duplicate-order prevention, fresh reconciliation, real-position/oversell checks,
limit-only execution, compliance, circuit breaker, wallet signing, configured
risk caps, and durable exchange audit. Remove only redundant mode locks and
micro-live-only restrictions that prevent the already-built lifecycle from
operating in `full_live`.

Do not claim guaranteed profit.

## Current Defects To Remove

1. `AutopilotService.collect_blockers()` hard-blocks every `full_live` tick with
   `formal full-live mode is locked`.
2. `first_run_checks()` always presents Full live as locked.
3. `dashboard_ui/app.py` renders the Full live mode card as permanently locked
   and provides no selection button.
4. `autopilot start --full-auto` persists `app_mode=micro_live`, not
   `app_mode=full_live`.
5. `AutopilotService._execute_live()` always uses the `micro-live` profile,
   including its `5/10/5` caps and `MIN_EDGE >= 0.10`, even when the operator
   explicitly selected Full live.
6. `_maybe_auto_exit()` only runs for `micro_live`.
7. `AutoExitService` rejects every profile except `micro-live`.
8. `AUTO_EXIT_MAX_POSITION_USDC` can block a SELL that would reduce an existing
   reconciled position. Full live must not become trapped in a position merely
   because the position grew above an entry-sized cap.
9. `/app` does not run the existing stale-order lifecycle management as part of
   every live cycle.
10. The CLI full-auto plan, `/app` mode, profile/override lookup, UI labels, and
    runbook describe different operating modes. They must become one coherent
    path.

## Mandatory Reuse Map

Reuse these owners. Do not create replacements:

- `/app`, `dashboard.py`, and `AutopilotService`: mode controls and scheduler.
- `MarketWorkflowService`: forecast, probability, pricing, and analysis.
- `TradingService`: the only automatic/live BUY mutation path.
- `PositionExitService`: the only SELL mutation path.
- `AutoExitService`: automatic SELL orchestration only.
- `ExitGuardianService`: exit recommendation policy.
- `OrderLifecycleService`: stale/open order management.
- `ReconciliationService`: balances, positions, orders, fills, and intent status.
- `CircuitBreakerService`: global mismatch breaker.
- `Repository`: current SQLite persistence and override lookup.
- `TelegramNotifier`: submitted/fill/material-failure notifications.
- `full_auto_service.py`: resolve and describe full-live arming; do not create a
  second plan/service.

Expected new services: **zero**.

Expected new database tables: **zero**.

Expected new scheduler or execution adapter: **zero**.

## Required Implementation

### Slice 1: Make Full Live A Real Mode

1. Remove the unconditional `full_live` blocker from `collect_blockers()`.
2. Full live must still report concrete blockers already owned by the system:
   - `TRADING_DISABLED=true`;
   - missing wallet/signing credentials;
   - compliance failure;
   - missing or stale reconciliation;
   - tripped resolution circuit breaker.
3. Change `first_run_checks()` so `full_live` is `ready` when those real checks
   pass. Do not keep a decorative lock check.
4. In `/app`, make the Full live card selectable using the existing `/app/mode`
   route. Selecting a mode must stop the loop, persist `mode=live` and
   `app_mode=full_live`, then require the existing Start button to run. This
   existing select-then-start sequence is sufficient confirmation; do not add a
   modal framework or another approval queue.
5. Update Chinese and English labels. They must say that Full live uses configured
   live limits and automatically manages entry and exit. Remove all “locked”,
   “unavailable”, and “future slice” copy.

### Slice 2: Give Full Live Its Own Existing-Profile Policy

Add one `full-live` entry to the existing `BUILTIN_PROFILES` map. This profile is
just policy data, not a new engine.

Required behavior:

- `dry_run=False`
- `default_action_kind="trade_live"`
- use operator-configured `MAX_ORDER_USDC`, `MAX_DAILY_USDC`,
  `MAX_MARKET_USDC`, and `MIN_EDGE`, still clamped by the existing hard caps;
- do not silently inherit the tighter `micro-live` caps or its `MIN_EDGE=0.10`;
- keep one new live entry per tick for this slice. Do not rewrite Autopilot into a
  batch engine while fixing mode activation.

Refactor the smallest existing helper so `_execute_live()` selects:

- `micro-live` profile for `app_mode=micro_live`;
- `full-live` profile for `app_mode=full_live`.

The matching strategy override must use the selected profile name. Full live with
an empty `LIVE_MARKET_IDS` must use the existing profile-wide wildcard override
`*:full-live`; a non-empty `LIVE_MARKET_IDS` must remain a real narrowing filter.

Do not copy the BUY gates into `AutopilotService`. Continue to call
`TradingService.trade()`.

### Slice 3: Unify CLI Full Auto With `/app` Full Live

Update the existing `full_auto_service.py` and `autopilot start --full-auto` path:

1. `--full-auto` must persist `app_mode=full_live`, not `micro_live`.
2. Resolve the plan with the new `full-live` profile.
3. Arm `*:full-live` when `LIVE_MARKET_IDS` is empty; otherwise arm the existing
   per-market full-live overrides.
4. Keep `TRADING_DISABLED=false`, credentials, compliance, reconciliation, and
   breaker requirements.
5. Keep the live whitelist open when `LIVE_MARKET_IDS` is empty. Do not quietly
   reintroduce a mandatory per-market whitelist.
6. Update `describe_full_auto_plan()` and CLI output to say `full-live`, not
   `full-auto-micro-live`.
7. Remove superseded micro-full-auto branching/copy instead of preserving two
   nearly identical full-auto plans.

### Slice 4: Complete Automatic Position And Order Management

Every `full_live` tick must use the existing components in this order:

1. discovery and candidate analysis;
2. reconciliation and durable commit;
3. stale/open order lifecycle review and cancellation when the existing
   `OrderLifecycleService` policy says cancellation is required;
4. refresh analysis for current positions before exit decisions;
5. `AutoExitService` for `position_at_risk` positions;
6. select and submit at most one new eligible BUY.

Requirements:

- `AutoExitService` must accept both `micro-live` and `full-live` profiles.
- Full live must require `AUTO_EXIT_ENABLED=true`; the repository currently has
  this explicitly enabled in `.env`, and the CLI must fail visibly if it is off.
- For `micro_live`, preserve the existing `AUTO_EXIT_MAX_POSITION_USDC` behavior.
- For `full_live`, do not reject a risk-reducing SELL merely because its notional
  exceeds `AUTO_EXIT_MAX_POSITION_USDC`. The SELL must still be no larger than the
  actual reconciled position, use a fresh token-specific bid, obey configured
  slippage, use a limit order, and pass compliance/breaker/signing gates.
- Never auto-sell an unresolved amount larger than the actual position.
- Never convert an expired/resolving market with no executable bid into a market
  order. Record the explicit skip and leave it for settlement/redeem handling.
- A stale-order cancellation failure must be recorded and surfaced, but must not
  erase prior exchange audit rows.
- After any exchange mutation, persist the intent and submitted attempt before
  verification, preserving commit semantics from commit `508659c` and the
  incident fix in `4c7a496`.

Do not add a second lifecycle controller. If `OrderLifecycleService` lacks one
small method needed by Autopilot, extend it narrowly.

### Slice 5: Operator Visibility And Recovery

The primary `/app` page must show, using the existing snapshot/read models:

- `Full live` selected/running/stopped;
- configured order/day/market caps and minimum edge;
- live whitelist `OPEN` or the configured IDs;
- automatic exit armed/not armed;
- latest successful reconciliation age;
- open orders and nonzero positions counts;
- most recent material failure and the existing recovery action/reason.

Do not add another dashboard section if the existing command, safety, blocker,
or stats panels can hold the information.

Telegram behavior remains material-only:

- live BUY submitted;
- live SELL submitted;
- exchange fill confirmed;
- submitted-unverified/reconciliation failure;
- circuit breaker trip or background-loop fatal/recovered event.

Do not send idle, discovery, no-candidate, low-edge, or heartbeat messages.

### Slice 6: Runbook And Startup Contract

Update the existing `docs/runbooks/full-auto-micro-live.md` in place. Renaming it
is allowed only if every repository link is updated in the same commit. Do not
create a competing full-live runbook.

Document this exact local production startup:

```bash
uv run polymarket-weather reconcile
uv run polymarket-weather live-readiness
uv run polymarket-weather autopilot start --full-auto
```

Document Stop as:

1. use `/app` Pause or Ctrl-C;
2. set `TRADING_DISABLED=true` for an execution kill switch;
3. inspect `/orders`, `/positions`, and the latest reconciliation before restart.

State clearly that process liveness is not Autopilot liveness. `/app` must show a
stale last-tick warning when the last successful/failed cycle is older than two
configured tick intervals.

Update README only where it still describes Full live as locked or says the only
autonomous mode is micro-live.

## Tests Required

All tests must be offline with mocked exchange mutations. Do not submit, cancel,
or sell a real order during implementation.

At minimum add or update tests proving:

1. `full_live` has no decorative lock blocker when real readiness checks pass;
2. each real blocker still blocks Full live with a concrete reason;
3. `/app` renders Full live as selectable in Chinese and English;
4. selecting Full live persists `mode=live`, `app_mode=full_live`, and
   `enabled=false`;
5. pressing Start enables Full live without silently changing it to micro-live;
6. `autopilot start --full-auto --once` resolves Full live and arms the
   `*:full-live` override when the whitelist is open;
7. non-empty `LIVE_MARKET_IDS` still narrows eligible markets;
8. Full live uses configured env caps/min-edge, while micro-live retains its
   tighter profile;
9. a quantitative trade signal calls the existing `TradingService` BUY path once;
10. an existing position is not re-entered or averaged into;
11. stale orders are managed through `OrderLifecycleService`, not a new cancel
    implementation;
12. Full live calls AutoExit for `position_at_risk` and submits through
    `PositionExitService` once;
13. a full-live exit above `AUTO_EXIT_MAX_POSITION_USDC` is allowed only as a
    reduction of the actual reconciled position;
14. oversell, stale reconciliation, stale/missing bid, excess slippage, breaker,
    and compliance still block SELL;
15. exchange-accepted BUY/SELL remains durably audited when post-submit checking
    fails;
16. partial fills, no-open-order state, and intent lifecycle remain correct;
17. one tick performs at most one new BUY and the configured number of exits;
18. idle/no-candidate ticks send no Telegram message;
19. background cycle failure does not kill the next cycle;
20. stale last-tick status is visible on `/app`.

Run:

```bash
UV_CACHE_DIR=/tmp/pwa-uv-cache uv run pytest <targeted files> -q
UV_CACHE_DIR=/tmp/pwa-uv-cache uv run ruff check src/ tests/
UV_CACHE_DIR=/tmp/pwa-uv-cache uv run pytest -q
git diff --check
git status --short
```

## Commit Structure

Use small commits on `main` or the current branch, in this order:

1. `Unlock full-live app mode and profile`
2. `Wire full-live entry exit and order lifecycle`
3. `Update full-live UI runbook and regression tests`

Do not force-push. Do not rewrite unrelated history. Do not commit `.env`, the
live database, backups, secrets, attachments, or design prototypes.

## Stop Conditions

Stop and report before proceeding if any implementation would:

- create a second BUY, SELL, reconciliation, scheduler, strategy, or lifecycle
  service;
- require a schema migration or new table;
- discard or rewrite live order/fill history;
- weaken idempotency, oversell protection, durable submitted-attempt recording,
  compliance, circuit breaker, or limit-order-only execution;
- execute a real BUY, SELL, or cancel.

Real-money mutation is a separate operator action after Codex reviews the code.
Do not treat this task document as an exact order confirmation.

## Completion Report Required

Report all of the following:

- objective completed;
- existing components reused;
- exact obsolete locks/branches removed;
- every new file/class/table/setting and justification (expected: one profile,
  zero services, zero tables, zero settings);
- production behavior changed for discover/analyze/reconcile/cancel/exit/entry;
- targeted and full test commands with exact results;
- commits and push status;
- remaining `git status --short` output;
- explicit statement that no real exchange mutation was executed.

End with a concise Codex acceptance checklist mapping each of the 20 required
tests/behaviors to its implementation and test name.
