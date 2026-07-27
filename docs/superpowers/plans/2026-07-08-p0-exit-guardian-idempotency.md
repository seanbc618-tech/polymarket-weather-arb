# P0 Exit Guardian And Idempotency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dry-run exit recommendation layer and prevent duplicate live order submission for the same active market side.

**Architecture:** Keep the first P0 slice defensive: generate cancel/reduce/hold recommendations without executing them, and add live-order idempotency before any CLOB write. Reuse existing `open_orders`, `positions`, `analyses`, and `order_intents` tables; add only minimal schema needed for order idempotency.

**Tech Stack:** Python, SQLite, Typer service layer, pytest, ruff.

---

## File Structure

- Create `src/polymarket_weather_arb/services/exit_guardian_service.py`
  - Defines `ExitRecommendation` and `ExitGuardianService`.
  - Reads open orders, positions, and latest analyses.
  - Emits dry-run recommendations only.
- Modify `src/polymarket_weather_arb/domain/execution.py`
  - Add `idempotency_key` to `OrderIntent`.
  - Generate deterministic live idempotency keys.
- Modify `src/polymarket_weather_arb/storage/db.py`
  - Add nullable unique `idempotency_key` column to `order_intents`.
  - Add an index for active live intents.
- Modify `src/polymarket_weather_arb/storage/repositories.py`
  - Persist idempotency keys.
  - Add lookups for active live intents and active open orders by market/side/token.
- Modify `src/polymarket_weather_arb/services/trading_service.py`
  - Reject duplicate live orders before placing a CLOB order.
- Test `tests/test_exit_guardian_service.py`
- Test `tests/test_trading_service.py`

## Task 1: Exit Guardian Dry-Run Recommendations

**Files:**
- Create: `src/polymarket_weather_arb/services/exit_guardian_service.py`
- Test: `tests/test_exit_guardian_service.py`

- [x] **Step 1: Write failing tests**

Add tests that seed open orders, positions, and latest analysis rows, then assert:
- stale order -> `cancel_stale`
- open order with latest decision not trade -> `cancel_edge_gone`
- position with latest edge below threshold -> `position_at_risk`
- healthy position -> `hold_position`

- [x] **Step 2: Run focused tests and verify failure**

Run: `UV_CACHE_DIR=/private/tmp/pwa-uv-cache uv run pytest tests/test_exit_guardian_service.py -q`

- [x] **Step 3: Implement `ExitGuardianService`**

Use existing repository reads only. Do not call `cancel_order`, `place_limit_order`, or any CLOB write method.

- [x] **Step 4: Run focused tests and verify pass**

Run: `UV_CACHE_DIR=/private/tmp/pwa-uv-cache uv run pytest tests/test_exit_guardian_service.py -q`

## Task 2: Order Idempotency And Duplicate Live Guard

**Files:**
- Modify: `src/polymarket_weather_arb/domain/execution.py`
- Modify: `src/polymarket_weather_arb/storage/db.py`
- Modify: `src/polymarket_weather_arb/storage/repositories.py`
- Modify: `src/polymarket_weather_arb/services/trading_service.py`
- Test: `tests/test_trading_service.py`

- [x] **Step 1: Write failing tests**

Add tests asserting:
- a second live trade for the same market/side is rejected when an active submitted intent exists.
- a live trade is rejected when an exchange open order already exists for the same market/token/side.
- rejected duplicate live orders do not call `client.place_limit_order`.
- dry-run orders still work as before.

- [x] **Step 2: Run focused tests and verify failure**

Run: `UV_CACHE_DIR=/private/tmp/pwa-uv-cache uv run pytest tests/test_trading_service.py -q`

- [x] **Step 3: Add schema and repository support**

Add `order_intents.idempotency_key`, a unique index over non-null keys, and repository helpers:
- `active_live_order_intent(market_id, side)`
- `active_open_order(market_id, token_id, side)`

- [x] **Step 4: Add TradingService guard**

Before live submission:
- Build an idempotency key from `market_id`, `side`, and `token_id`.
- If active live intent or active exchange open order exists, save a rejected intent and return a duplicate-protection reason.
- If no duplicate exists, save intent with idempotency key and proceed as before.

- [x] **Step 5: Run focused tests and full verification**

Run:
- `UV_CACHE_DIR=/private/tmp/pwa-uv-cache uv run pytest tests/test_trading_service.py tests/test_exit_guardian_service.py -q`
- `UV_CACHE_DIR=/private/tmp/pwa-uv-cache uv run ruff check src/ tests/ scripts/`
- `UV_CACHE_DIR=/private/tmp/pwa-uv-cache uv run pytest -q`

## Self-Review

- Coverage: This plan implements the first P0 slice only. It does not implement automatic close orders, resolution audit, global circuit breakers, phase-split autopilot, LLM rule review, PnL UI, retry policy, WebSocket orderbooks, or replay.
- Safety: Exit recommendations are dry-run only. Duplicate guards affect live trading only and should not reduce dry-run observability.
- Test expectation: All existing tests must continue passing.
