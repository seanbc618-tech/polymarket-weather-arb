# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Mandatory Worker Standard

Before planning or editing, read `docs/agent-worker-standards.md`. It is the
authoritative product objective, ownership map, reuse policy, LLM boundary, and
handoff contract for Claude Code and every other worker. If it conflicts with an
older handoff or plan, the worker standard wins.

## Current Handoff

Before changing code, read docs/claude-code-handoff.md. It summarizes the Codex optimization work through 2026-06-02, current live-trading safety gates, beginner cockpit behavior, HK VPS deployment plan, common pitfalls, and recommended next slices.

## Project Overview

CLI-first MVP for researching and trading Polymarket weather markets. Discovers weather-related markets, parses settlement rules, fetches forecasts, estimates probabilities, detects mispricing, and enforces risk caps before live orders. All decisions persisted in SQLite.

## Development Commands

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest

# Run single test file
uv run pytest tests/test_rules.py

# Run single test
uv run pytest tests/test_rules.py::test_parse_resolution_rule -v

# Lint
uv run ruff check src/ tests/

# Format
uv run ruff format src/ tests/

# Type check (if configured)
uv run mypy src/

# Initialize database
uv run polymarket-weather init-db

# Health check
uv run polymarket-weather doctor
```

## Architecture

### Entry Point & CLI

`src/polymarket_weather_arb/cli.py` — Typer-based CLI with subcommand groups:
- Root commands: `init-db`, `doctor`, `discover-markets`, `markets`, `candidates`, `analyze`, `trade`, `orders`, `reconcile`, `risk-report`, `dashboard`
- `operator` subcommands: guided automation console (`start`, `go`, `daemon`, `queue`, etc.)
- `profiles` subcommands: strategy presets (`list`, `show`)
- `fixtures` subcommands: market fixture import/load
- `automation` subcommands: human-approved action queue

### Domain Layer (`domain/`)

Pure data models and business logic, no I/O:
- `markets.py` — `Market`, `MarketSnapshot` dataclasses; weather classification via keyword matching
- `rules.py` — `ResolutionRule` parsing from market title/description; extracts location, source, variable, threshold, operator, window
- `pricing.py` — Conservative probability interval estimation
- `probability.py` — Probability distribution helpers
- `risk.py` — Hardcoded risk caps (25/order, 100/day, 50/market USDC); exposure validation
- `execution.py` — Order intent construction
- `weather.py` — `ForecastSnapshot` model
- `china_temperature_bucket.py` — China temperature bucket market domain
- `china_bucket_pricing.py` — China bucket pricing logic

### Adapters (`adapters/`)

External API clients (all behind Protocol base classes for testability):
- `polymarket/client.py` — Gamma API (market discovery), CLOB API (order books, trading)
- `polymarket/translator.py` — CLOB token/market ID translation
- `weather/open_meteo.py` — Open-Meteo forecast provider
- `weather/noaa.py` — NOAA/NWS provider
- `weather/china_official.py` — China official weather station data

### Services (`services/`)

Orchestration layer:
- `discovery_service.py` — Market scanning, rule parsing, candidate persistence
- `market_workflow_service.py` — End-to-end workflow: snapshot → forecast → analysis → trade
- `trading_service.py` — Order placement with risk gates
- `automation_service.py` — Human-approved action queue (propose → approve → execute)
- `operator_daemon.py` — Continuous automation daemon with risk guard, reconciliation, notifications
- `reconciliation_service.py` — CLOB balance/order/position reconciliation
- `fixture_service.py` — JSON fixture import for offline testing
- `china_bucket_discovery_service.py` — China temperature bucket market discovery

### Storage (`storage/`)

- `db.py` — SQLite schema, `Database` class with `init_schema()` and connection management
- `repositories.py` — `Repository` class wrapping all SQL operations

### Profiles (`profiles.py`)

Strategy presets: `balanced`, `conservative`, `research-only`, `dry-run-demo`, `micro-live`. Each caps order/day/market limits and sets default action kinds. Profiles cannot loosen hardcoded caps.

### Dashboard (`dashboard.py`)

Built-in HTTP server (no dependencies beyond stdlib) with:
- Read-only views: actions, runs, open orders, positions, fills, overrides, discovery, markets, candidates
- i18n: English/Chinese via cookie
- Binds to `127.0.0.1` only

### Modules (`modules/`)

Pluggable market type registry. Currently: `weather` (standard) and `china_temp_bucket` (China temperature bucket markets).

## Configuration

Settings via `.env` file (pydantic-settings). Key groups:
- **Polymarket APIs**: `POLYMARKET_GAMMA_API_BASE`, `POLYMARKET_CLOB_API_BASE`, `POLYMARKET_DATA_API_BASE`
- **Live trading credentials**: `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`, `POLYMARKET_SIGNATURE_TYPE`
- **Risk caps**: `MAX_ORDER_USDC`, `MAX_DAILY_USDC`, `MAX_MARKET_USDC`, `MIN_EDGE`, `SLIPPAGE_BUFFER`
- **Weather**: `WEATHER_PROVIDER`, `WEATHER_API_KEY`, China station URLs

## Testing

- pytest with `pytest-httpx` for HTTP mocking
- Test files mirror source: `tests/test_rules.py` tests `domain/rules.py`
- Fixtures in `fixtures/` and `data/` directories
- `--demo-analysis` flag seeds fixture-only data for offline pipeline testing

## Safety Model

- No market orders; limit orders only
- Ambiguous settlement rules rejected
- Live trading requires: credentials + fresh reconciliation + risk guard `ok` + market whitelist + strategy override with `live_auto_enabled=True`
- Daemon live auto is default-off; `micro-live` profile + explicit gates required
- All decisions logged to SQLite before execution
