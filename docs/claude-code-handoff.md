# Claude Code handoff: Polymarket Weather Arb

Last updated: 2026-06-02

Read this before changing code. The current main branch already contains a substantial safety, operator, dashboard, and deployment upgrade series. Do not restart from old assumptions.

## Current state

This is still a CLI-first Polymarket weather-market research and trading bot. It now has a safer operator cockpit, beginner browser mode, stronger live-trading gates, order lifecycle controls, HK VPS deployment scaffolding, and an explicit dry-run-to-readiness rehearsal flow.

The project is not ready for a non-technical user to enable live trading blindly. It is ready for a non-technical user to open a local beginner page and run safe dry-run rehearsal actions.

## Repository expectations

- Work on main unless the user explicitly asks for a branch.
- The user prefers direct commits to main for this personal repo; PR ceremony was considered unnecessary.
- Do not commit secrets, .env, SQLite DBs, backups, .run, .venv, caches, or generated local artifacts.
- Do not revert unrelated user changes.
- Keep changes small and commit intentionally.
- Before work in Claude Code, pull/fetch main so you are not working from a stale clone.

Useful commands:

    uv sync --extra dev
    uv run pytest -q
    uv run ruff check src/ tests/ scripts/rehearse_live_readiness.py
    uv run polymarket-weather operator launch
    uv run polymarket-weather live-readiness --no-check-exchange
    python scripts/rehearse_live_readiness.py

## Important recent commits

- 83aae68 Add beginner operator cockpit
  - Adds operator launch.
  - Adds /beginner dashboard page.
  - Adds a browser-safe rehearsal button that loads bundled demo fixture and records a dry-run order intent.
  - Keeps live actions locked in browser beginner mode.

- 87ac92f Add HK live deployment rehearsal docs
  - Adds deploy/env/hk-live.example.env.
  - Adds docs/hk-vps-production-checklist.md.
  - Adds scripts/rehearse_live_readiness.py.
  - Documents dry-run-to-live-readiness rehearsal.

- 7e37f33 Add production ops scaffolding
  - Adds GitHub Actions CI.
  - Adds backup-db CLI and SQLite backup service.
  - Adds systemd templates for daemon, dashboard, and backup timer.

- 12cc1c1 Gate live trades by forecast source grade
  - Live trade rejects signal-only forecasts.
  - Dry-run remains allowed with demo/Open-Meteo style sources.
  - Existing risk and reconciliation rejections still take priority.

- 96dbc05 Add order lifecycle controls
  - Adds open-order refresh and cancel commands.
  - Adds order lifecycle service and repository helpers.

- 87db271 Add live readiness checks
  - Adds live-readiness command.
  - Extends doctor --live.

- c8793e4 Add live compliance kill switch
  - Adds TRADING_DISABLED.
  - Adds geoblock/compliance gate with default allowed country HK.
  - Wires compliance into CLI live trade and daemon live execution.

## Beginner-safe flow

The latest beginner entrypoint is:

    uv run polymarket-weather operator launch

It initializes the DB, starts the local dashboard, and prints a URL like:

    http://127.0.0.1:8765/beginner?lang=zh

The /beginner page shows:

- kill-switch status
- live credential status
- reconciliation status
- recent dry-run count
- safe rehearsal button
- live locked copy

POST /beginner/rehearse:

- loads fixtures/markets/demo-weather-nyc-high-2026-05-08.json
- seeds demo analysis
- runs module workflow dry-run
- records a dry-run order intent
- redirects back to /beginner
- never approves or executes live actions

Do not add live execution to /beginner casually. Its job is safe first use, not live cutover.

## CLI and dashboard map

Main root commands include:

- init-db
- doctor, doctor --live
- live-readiness
- backup-db
- discover-markets
- discover-weather-events
- markets
- candidates
- inspect-market
- refresh-weather
- analyze
- trade
- orders
- reconcile
- risk-report
- dashboard

Subcommand groups:

- operator
- automation
- fixtures
- profiles

Important operator commands:

    uv run polymarket-weather operator launch
    uv run polymarket-weather operator start
    uv run polymarket-weather operator demo --profile dry-run-demo
    uv run polymarket-weather operator go --profile dry-run-demo --propose-only
    uv run polymarket-weather operator daemon --once --profile dry-run-demo
    uv run polymarket-weather operator live-monitor --profile micro-live
    uv run polymarket-weather operator refresh-open-orders
    uv run polymarket-weather operator open-orders
    uv run polymarket-weather operator cancel-order <exchange_order_id>
    uv run polymarket-weather operator positions --nonzero-only
    uv run polymarket-weather operator fills

Dashboard routes worth knowing:

- /beginner
- /
- /actions
- /runs
- /markets
- /candidates
- /orders
- /risk
- /reconciliation
- /open-orders
- /positions
- /fills
- /overrides
- /operator
- /doctor
- /fixtures
- /setup

The dashboard is a stdlib HTTP server, not a JS app. It binds to 127.0.0.1 by default. Do not add a React/Vite frontend unless the user explicitly asks for a larger rewrite.

## Live-trading safety model

Live trading should remain hard to accidentally enable.

Current required gates include:

- live credentials configured: POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER
- TRADING_DISABLED=false
- compliance/geoblock check passes
- allowed country includes HK by default
- fresh successful reconciliation
- limit orders only
- hard risk caps: 25 USDC/order, 100 USDC/day, 50 USDC/market
- daemon live auto only via micro-live profile, --allow-live-auto, explicit --live-market, risk ok, fresh reconciliation by default, no nonzero positions by default, strategy override live_auto_enabled=True
- live trade only enabled for module weather
- live trade rejects non-settlement-grade forecasts

Do not bypass these gates for demos or convenience.

## Forecast source grade gate

Signal-only weather sources are acceptable for research and dry-run, not live orders.

Current behavior:

- Dry-run is allowed with demo/Open-Meteo style forecasts.
- Live trade requires latest forecast raw payload to include source_grade = settlement_grade or official_signal = true.
- Otherwise live trade records a rejected intent with reason: forecast source is not settlement-grade.

Important: existing risk failures such as stale reconciliation must remain higher priority than source-grade rejection. Tests cover this ordering.

## HK VPS deployment state

Files added:

- deploy/env/hk-live.example.env
- deploy/systemd/polymarket-weather-daemon.service
- deploy/systemd/polymarket-weather-dashboard.service
- deploy/systemd/polymarket-weather-backup.service
- deploy/systemd/polymarket-weather-backup.timer
- deploy/systemd/README.md
- docs/hk-vps-production-checklist.md

Expected deployment posture:

- dedicated Hong Kong VPS
- dedicated small Polymarket wallet
- TRADING_DISABLED=true during setup
- tiny first limits: MAX_ORDER_USDC=5, MAX_DAILY_USDC=10, MAX_MARKET_USDC=5
- dashboard exposed only locally or through SSH tunnel/private access
- backup timer enabled before daemon automation

Safe rehearsal:

    python scripts/rehearse_live_readiness.py

Read-only exchange rehearsal after credentials are set:

    set -a
    . /etc/polymarket-weather-arb.env
    set +a
    python scripts/rehearse_live_readiness.py --check-exchange
    uv run polymarket-weather doctor --live
    uv run polymarket-weather live-readiness

## Testing map

Use focused tests while working, then full suite before committing.

Current full suite at handoff: 168 passed.

Focused tests:

    uv run pytest tests/test_dashboard.py -q
    uv run pytest tests/test_cli_operator.py -q
    uv run pytest tests/test_rehearse_live_readiness.py -q
    uv run pytest tests/test_live_readiness_service.py -q
    uv run pytest tests/test_order_lifecycle_service.py -q
    uv run pytest tests/test_trading_service.py -q

Lint:

    uv run ruff check src/ tests/ scripts/rehearse_live_readiness.py

Avoid broad ruff format unless the user explicitly asks; it may create noisy unrelated churn.

## Code structure after refactors

CLI command groups:

- src/polymarket_weather_arb/cli_commands/common.py
- src/polymarket_weather_arb/cli_commands/operator.py
- src/polymarket_weather_arb/cli_commands/automation.py
- src/polymarket_weather_arb/cli_commands/fixtures.py
- src/polymarket_weather_arb/cli_commands/profiles.py

Dashboard renderers:

- dashboard_ui/html.py
- dashboard_ui/i18n.py
- dashboard_ui/overview.py
- dashboard_ui/markets.py
- dashboard_ui/automation.py
- dashboard_ui/exchange.py
- dashboard_ui/admin.py
- dashboard_ui/beginner.py

Repository automation helpers:

- storage/repositories.py
- storage/repository_automation.py

Modules:

- modules/weather.py
- modules/china_temp_bucket.py
- modules/registry.py
- services/module_workflows.py

Keep following these boundaries. Do not collapse everything back into cli.py or dashboard.py.

## Common mistakes to avoid

1. Do not add browser live-trade execution.
   Browser may propose/approve/run non-live actions. Browser must not approve or run trade_live.

2. Do not treat Open-Meteo or demo data as settlement-grade.
   Keep live source-grade gate intact.

3. Do not loosen hard risk caps from profiles.
   Profiles can tighten caps, not bypass hard caps.

4. Do not make the systemd dashboard public.
   Keep 127.0.0.1 and use SSH tunnel/private access.

5. Do not commit secrets, .env, DBs, backups, .run, .venv, or cache files.

6. Do not change live gates just to make demos look green.
   Missing credentials, TRADING_DISABLED=true, and missing reconciliation are expected warnings in offline rehearsal.

7. Do not add a large frontend framework for small dashboard changes.
   Current dashboard is intentionally stdlib and low dependency.

8. Do not run broad network discovery in tests.
   Use fixtures and fakes.

9. Do not use destructive git commands.
   The user may have local work.

## Recommended next design direction

The next phase should make safe dry-run operation beginner-friendly, then gradually expose live-readiness guidance without live execution in the browser.

### Slice 1: Beginner cockpit polish

Goal: make /beginner useful enough that the user can operate dry-run without reading CLI docs.

Implement:

- Show last rehearsal result with timestamp.
- Show a small “what happened” summary after safe rehearsal: fixture loaded, analysis seeded, dry-run intent recorded, live not touched.
- Link directly to the created market detail and order intent.
- Keep all controls non-live.

Tests:

- /beginner renders last dry-run.
- POST /beginner/rehearse creates or updates visible state.
- no trade_live text/action leaks onto page.

### Slice 2: Beginner safe tick

Goal: give one safe automation button in beginner mode.

Implement:

- Button runs one daemon tick with profile=dry-run-demo, dry_run_only=True, allow_live_auto=False.
- Start with discover=False or existing candidates only.
- Display proposed action, auto-executed dry-run IDs, risk status, and notes.

Tests:

- POST cannot set allow_live_auto.
- result is visible in /beginner.

### Slice 3: Guided setup page

Goal: turn doctor, live-readiness, and HK checklist into a UI checklist.

Implement read-only cards:

- database initialized
- SDK installed
- credentials missing/configured
- compliance status
- reconciliation missing/fresh/stale
- backups exist
- latest daemon tick status

Do not add a turn-live-on button. Provide copy-paste CLI snippets for live readiness.

### Slice 4: Settlement-grade source work

Goal: improve the real live path by supporting official/settlement-grade sources.

Implement:

- Clarify NOAA/NWS observation/forecast source handling for US markets.
- Persist source_grade only for official settlement-grade sources.
- Keep Open-Meteo as signal-only.
- Add tests proving live rejects signal-only and accepts official signal only after other risk gates pass.

### Slice 5: Order and position cockpit

Goal: make live safety monitoring easier before any live cutover.

Implement:

- Better /open-orders refresh UX.
- Better cancellation flow with credentials and explicit confirmation.
- Position exposure summary.
- “nonzero positions block live auto” card.

### Slice 6: Production runbook automation

Goal: reduce manual VPS mistakes.

Implement:

- Optional scripts/install_systemd_units.sh or documented make target.
- scripts/backup_restore_check.py.
- Lightweight CI/doc checks for deployment files if useful.

## What production ready still means

Before real live trading, require all of this:

- HK VPS environment confirmed.
- Dedicated small wallet funded only with acceptable test size.
- TRADING_DISABLED=true during setup.
- doctor --live reviewed.
- live-readiness reviewed.
- successful reconciliation stored.
- open orders, positions, and fills inspected.
- several dry-run daemon cycles stable.
- settlement-grade source path tested on a real candidate.
- one whitelisted market only.
- micro-live only.
- tiny caps only.
- backup timer working.
- rollback procedure rehearsed.

Until then, call it safe dry-run/research automation, not fully autonomous production trading.

## User preferences learned during this work

- User prefers direct commits to main for this personal repo.
- User wants practical progress over PR ceremony.
- User wants beginner-safe UI and operations.
- User plans HK server deployment for live scripts.
- User values explicit safety gates and wants fewer confusing UI/config states.

## Final note to Claude Code

Be conservative. This project touches real-money trading. The user wants the system to become easier for a beginner, but that must mean easy to operate safely, not easy to bypass gates.
