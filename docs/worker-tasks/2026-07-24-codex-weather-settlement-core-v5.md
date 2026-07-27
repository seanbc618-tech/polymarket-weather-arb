---
worker: codex
kind: task
window_start: 2026-07-24
verdict: COMPLETE
real_trading_mutation: not_executed
---

# Weather Settlement Core V5 implementation

## Objective

Replace the high-win-rate/low-profit early-exit path with a settlement-oriented
policy that preserves rare winning buckets, bounds new exposure, and measures
fee-adjusted event-level results. Deploy the change without issuing any manual
BUY, SELL, cancel, or redeem.

## Reuse map

- Existing exit owner: `ExitGuardianService`.
- Existing automatic orchestration: `AutoExitService`.
- Existing and only live SELL path: `PositionExitService`.
- Existing entry owner: `AutopilotService`.
- Existing and only live BUY path: `TradingService`.
- Existing persistence boundary: `Repository` and current SQLite tables.
- Existing model remains `global-temp-bucket-multimodel-v8`.

Files to change:

- `domain/strategy_versions.py`: advance entry and exit policy identifiers.
- `services/exit_guardian_service.py`: settlement-core recommendation policy.
- `services/auto_exit_service.py`: allow only evidence/value full exits.
- `services/autopilot_service.py`: V5 live entry gates and scale-in freeze.
- `storage/repositories.py`: read-only query for prior accepted BUY in an event.
- Existing tests and strategy/runbook documentation.

No new Service, database table, persistent mode, BUY path, SELL path, or LLM
decision path will be created. Maker-first is deliberately excluded from this
release because it requires a separate fill-rate/markout experiment.

## Required behavior

1. Every small weather position is a 100% settlement core.
2. `recover_principal`, profit-taking, and dust-driven full exits cannot sell it.
3. Official evidence that makes the held bucket impossible may recommend a full
   exit.
4. Model-only exits require executable net SELL value to exceed the held
   probability upper bound plus a fixed margin across two fresh, distinct
   forecast revisions.
5. Missing/stale/incomplete evidence holds and escalates; it never becomes a
   bearish SELL signal.
6. V5 live entry requires edge at least `0.10`, ask at least `0.05`, non-D0
   horizon, and no prior accepted live BUY in the same city/date event.
7. Analyses may continue from `MIN_EDGE=0.08` for shadow evidence.

## Verification and rollout

- Add regression tests for Dallas/Manila-style winner retention and
  Cape Town/Wuhan/Ankara-style value recovery.
- Run targeted tests, Ruff, full pytest, `git diff --check`, and status review.
- Keep production `TRADING_DISABLED=true` after deployment while the service
  runs for shadow/reconciliation visibility.
- Do not restore full live execution until at least 20 new V5 resolved events
  show positive fee-adjusted event-level net EV with bounded loss.
- Commit, push, deploy, restart, then verify HEAD, service health, runtime mode,
  reconciliation, breaker, and zero new live intents/fills during acceptance.

## Completion evidence

- Implemented `weather-entry-v5` and
  `weather-exit-v2-settlement-core` through the existing BUY/SELL owners.
- Read-only production replay: actual V4 `-$0.410063`; hold-V4
  `+$11.935137`; V5-selected old path `+$0.140727`; V5 settlement core
  `+$13.558520` across six resolved events.
- Local final verification: Ruff passed, `1051 passed, 1 skipped`, and
  `git diff --check` passed.
- Runtime code deployed through `ca8ba75`; GitHub main and VPS include the
  deployment evidence; VPS worktree clean.
- Pre-deploy SQLite backup passed `integrity_check=ok`; environment backup
  preserved.
- Service runs via the V5 shadow systemd drop-in with `TRADING_DISABLED=true`,
  `MIN_EDGE=0.08`, and code live-entry floor `0.10`.
- Production completed two normally spaced reconciliations through `5988/ok`
  and 94 shadow analyses after restart; circuit breaker was clear.
- Order intent and fill cursors remained `637` and `306`.
- No manual BUY, SELL, cancel, redeem, or reconcile was executed.
- Deployment and evidence details:
  `docs/reviews/weather-settlement-core-v5-implementation-2026-07-24.md`.
