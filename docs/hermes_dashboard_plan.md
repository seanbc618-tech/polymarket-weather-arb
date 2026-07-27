# Polymarket Hermes Dashboard Automation Plan

## Goal

Build a staged automation layer where Hermes/Discord acts as a read-only UI dashboard first, then grows into candidate review and human-approved operations. Discord messages must never bypass the local Polymarket CLI risk engine.

## Phase 1: Read-only Discord dashboard routing

Status: partially complete.

- Use `agent_dispatch` as the fallback dashboard until dedicated channels are created.
- Support dedicated dashboard channel names for:
  - `scanner_dashboard` -> 扫盘仔
  - `trader_dashboard` -> 盘口小妹
  - `risk_dashboard` -> 守线姐
  - `news_dashboard` -> 快讯姐仔
  - `review_dashboard` -> 复盘哥
  - `captain_dashboard` -> HERMES主鸡
- Keep all notifications read-only with `allowedNow=false` and `requiresHumanApproval=true`.
- Post discovery, dry-run, risk, reconciliation, tests, alerts, and reviews as compact dashboard cards.

Verification:

- `npm --prefix "$HOME/agent-discussion-coordinator" run check`
- `python3 /path/to/polymarket-weather-arb/scripts/notify_dashboard.py tests`
- `python3 /path/to/polymarket-weather-arb/scripts/notify_dashboard.py risk`

## Phase 2: Scheduled read-only status pushes

Status: MVP complete for manual/scheduled ticks.

- Add a scheduler script that can run these jobs:
  - discovery scan every 15-30 minutes while active
  - risk report every scan cycle
  - reconciliation status when credentials are present
  - test result after local test runs
- Post all outputs through coordinator `notify`.
- Add optional reconciliation status cards behind `--include-reconciliation` so scheduled ticks do not unexpectedly use live credentials.
- Add duplicate suppression so the same empty/no-op result does not spam Discord.
- Store last notification hashes locally under ignored `data/`. Done.

Usage:

```bash
python3 scripts/notify_dashboard.py tick --limit 100 --pages 3
python3 scripts/notify_dashboard.py tick --limit 100 --pages 3 --include-tests --force
```

Verification:

- Run one scheduler tick manually.
- Confirm Discord receives discovery, candidate queue, and risk cards.
- Confirm repeated identical tick can be suppressed.

## Phase 3: Candidate queue

Status: MVP complete.

- Persist scan candidates in SQLite with:
  - market id/title/slug via `markets`
  - tradable flag and rejection reason
  - bid/ask/spread
  - freshness timestamps
  - candidate status: `dry_run_ready`, `rejected`, or manually marked statuses such as `reviewed`
- Add CLI commands:
  - `candidates`
  - `candidates --status <status>`
  - `candidate-mark --market <id> --status <status> --notes <notes>`
- Send candidate queue cards to the scanner dashboard through scheduled `tick` notifications.

Verification:

- Discovery creates candidate rows.
- Rejected broad markets explain why they are rejected.
- Fixture/demo markets can appear as dry-run-ready.

## Phase 4: Position reconciliation gate

Status: MVP complete; needs live credential validation before real use.

- Use authenticated CLOB reads for balances, open orders, and trades.
- Use Polymarket data API wallet positions as the position source.
- Persist positions/fills/open orders into SQLite.
- Only mark reconciliation `ok` if balances, orders, trades/fills, and positions all sync successfully.
- Keep live trading blocked when credentials are absent, reconciliation is stale, or any adapter returns partial data.

Verification:

- `reconcile` records positions.
- `risk-report` includes reconciled exposure.
- Live trade remains blocked if reconciliation is stale or partial.

## Phase 5: Semi-automated operations interface

Status: MVP complete for read-only proposal cards.

- Add action proposal envelopes, still read-only by default:
  - `propose_dry_run`
  - `propose_refresh_weather`
  - `propose_analyze`
  - `propose_trade_review`
- Discord can produce proposal cards, but execution requires local CLI or explicit human approval.
- Add approval log entries with user id, timestamp, command, and risk decision before any Discord-originated execution is implemented.

Verification:

- Agent can propose an action card.
- No action executes from Discord without approval.
- Approved action still passes local CLI risk checks.

## Current next tasks

1. Commit the read-only proposal card changes.
2. Validate `reconcile` against a dedicated small Polymarket wallet when credentials are available.
3. Keep Discord-triggered execution disabled until approval logging exists.
