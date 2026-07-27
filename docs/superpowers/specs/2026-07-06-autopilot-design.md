# Autopilot Mode Design

Date: 2026-07-06

## Goal

Transform the CLI-first operator console into a single-command autonomous trading app:

- `uv run polymarket-weather autopilot start`
- One page: `/app` (status, start/stop, recent decisions, blockers)
- No human approval queue for routine dry-run/live cycles
- Preserve hard risk caps, compliance, reconciliation gates for live mode

## Phases

### Phase 1 (this implementation)

- AutopilotService background tick loop
- SQLite `autopilot_state` + `autopilot_decisions`
- `/app` replaces `/` and `/beginner` as default entry
- Default mode: `dry_run`; `--live` enables real orders when gates pass

### Phase 2

- LLM advisor layer (OpenAI / Anthropic / local)
- Structured JSON decisions; code still enforces risk

### Phase 3

- HK VPS one-click deploy, micro caps, remote monitoring

## Architecture

```
autopilot start
  ├─ init DB + enable autopilot_state
  ├─ background thread: AutopilotService.tick() every N seconds
  └─ HTTP server: /app only (legacy routes hidden)

AutopilotService.tick()
  1. collect blockers (kill switch, compliance, reconciliation for live)
  2. discover weather markets (events page + gamma scan)
  3. research/analyze top weather candidates
  4. pick best edge >= MIN_EDGE
  5. execute dry_run or live trade via existing TradingService
  6. persist autopilot_decisions row
```

## Safety

- LLM (phase 2) cannot bypass TradingService / RiskEngine
- Live mode requires: credentials, compliance pass, fresh reconciliation, weather module only
- `TRADING_DISABLED=true` blocks all execution
- Old dashboard routes remain reachable by direct URL for debugging

## User preference

- Direct live trading when gates pass (user on Vietnam IP locally; HK VPS later)
- Phase 1 ships dry-run default with `--live` opt-in