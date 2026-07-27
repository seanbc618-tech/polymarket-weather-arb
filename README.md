# Polymarket Weather Arb

[简体中文说明](README.zh-CN.md) | English

> [!NOTE]
> This is the MIT-licensed community edition, exported from the final
> pre-Anchor V5.1 checkpoint on 2026-07-25. Later private strategy development
> is intentionally not included. Issues, pull requests, reviews, and research
> feedback are welcome.

> [!WARNING]
> This is experimental research software, not financial advice and not a claim
> of profitability. Review the code, settlement rules, jurisdictional
> restrictions, and risk controls before enabling any live mode.

Beginner-first local app for researching and automating Polymarket weather markets. The original CLI/operator tools remain available as advanced mode.

## Quick Start

```bash
# 1. Install dependencies
uv sync

# 2. Initialize database
uv run polymarket-weather init-db

# 3. Start the beginner app
uv run polymarket-weather autopilot start

# 4. Open the app
# http://127.0.0.1:8765/app?lang=zh

# 5. Optional: check live readiness before any live mode
uv run polymarket-weather live-readiness
```

## Key Features

- **Autopilot App**: Beginner-first `/app` with startup checks and four user-facing modes
- **Beginner Cockpit**: Legacy safe rehearsal screen kept as advanced mode
- **Live Launchpad**: Readiness preview with blockers and CLI commands
- **NOAA Data Source**: Official US weather data for accurate forecasting
- **Ensemble Forecast**: equal-weight GFS/ECMWF/ICON/GEM local-day probability estimates
- **Module Credibility**: Clear promotion criteria for each market type
- **Order Lifecycle**: Stale detection, position exposure, fill summaries

## Scope

- Discover weather-related Polymarket markets.
- Parse simple single-location threshold settlement rules.
- Fetch normalized weather forecast data from official sources.
- Estimate conservative probability intervals.
- Detect mispricing after spread/slippage buffers.
- Enforce hard risk caps before live limit orders.
- Persist all decisions, risk checks, and order attempts in SQLite.

## Safety Defaults

- No secrets are committed. Copy the minimal `.env.example` to `.env` locally.
- Advanced/internal overrides are documented separately in `.env.advanced.example`; copy
  individual keys only when needed.
- Market orders are not supported.
- Ambiguous settlement rules are rejected.
- Unsupported weather variables are rejected.
- Live trading requires configured credentials and fresh reconciliation state.
- Position reconciliation uses authenticated CLOB reads plus the Polymarket data API before live orders can pass the freshness gate.
- Safe defaults: 1 USDC/order, 5 USDC/day, 2 USDC/market.
- Live execution is fail-closed by default: `TRADING_DISABLED=true` blocks all live orders
  until the operator explicitly enables them.
- Live execution also requires a passing geoblock check in `COMPLIANCE_ALLOWED_COUNTRIES` (default: `HK`); unknown or failed checks are blocked.

## Autopilot App Modes

Start here unless you are debugging the lower-level operator flow:

```bash
uv run polymarket-weather autopilot start
```

Open `http://127.0.0.1:8765/app?lang=zh`. The app exposes four modes:

- **Observe**: scan and analyze only; no order intent is created.
- **Paper trading**: scan, analyze, and record dry-run orders. This is the recommended default.
- **Micro live**: uses the existing `micro-live` path and still requires live credentials, compliance, fresh reconciliation, optional `LIVE_MARKET_IDS`, a live_auto override, and hard risk caps.
- **Full live**: selectable unattended mode using configured live caps/min-edge (not micro-live 5/10/5), automatic entry + exit, stale-order lifecycle, and existing capital-integrity gates. Start with `uv run polymarket-weather autopilot start --full-auto` after `reconcile` and `live-readiness`.

Advanced pages remain available from `/app`: `/live`, `/calibration`, `/actions`, `/overrides`, and `/beginner-legacy`.

## Advanced Operator Quickstart

Use the guided operator console when you need the original cautious workflow. It prints real IDs and next commands instead of placeholder examples.

```bash
uv run polymarket-weather operator launch
uv run polymarket-weather operator start
uv run polymarket-weather profiles list
uv run polymarket-weather operator go --profile dry-run-demo --propose-only
uv run polymarket-weather operator queue
uv run polymarket-weather dashboard --port 8765
```

Safe demo flow when live discovery has no usable market:

```bash
uv run polymarket-weather operator demo --profile dry-run-demo
uv run polymarket-weather operator queue-detail act_...
uv run polymarket-weather operator queue-timeline act_...
# Approve the printed act_... in Discord: /wufu action-approve action-id:act_...
# Or approve the latest pending action locally:
uv run polymarket-weather operator approve-latest --actor local-operator
uv run polymarket-weather operator run-approved --limit 1
uv run polymarket-weather operator queue-summary
```

The legacy beginner rehearsal is still available at `/beginner-legacy`.

Open the local read-only dashboard when you want a browser view of the queue, run log, and exchange-state snapshots:

```bash
uv run polymarket-weather dashboard --port 8765
# Open http://127.0.0.1:8765
# Useful pages: /actions, /runs, /open-orders, /positions, /fills, /overrides
```

Daily semi-automation flow:

```bash
uv run polymarket-weather discover-markets --limit 50 --pages 1
uv run polymarket-weather operator next --profile balanced
uv run polymarket-weather operator propose-next --profile balanced --reason "operator flow review"
# Approve in Discord or locally.
uv run polymarket-weather operator run-approved --limit 1
```

Discord approval only changes the local action queue. Execution still happens locally through `operator run-approved`, which reuses the allowlisted automation executor and the existing trading risk gates.

## Operator Daemon

The local daemon is the single automation core. It can scan, propose, auto-run `dry_run` actions, run risk guard checks, optionally reconcile, and post Hermes dashboard notifications. Live auto execution is default-off and only available through the `micro-live` profile plus explicit market whitelist gates.

```bash
# One safe tick: no long-running process.
uv run polymarket-weather operator daemon --once --profile dry-run-demo

# Continuous dry-run automation loop with risk guard each tick.
uv run polymarket-weather operator daemon --profile dry-run-demo --tick-seconds 300 --dry-run-only

# Use existing candidates only, useful for local testing without live discovery.
uv run polymarket-weather operator daemon --once --no-discover --profile dry-run-demo

# Include reconciliation and send phase cards to Hermes role dashboards.
uv run polymarket-weather operator daemon --once --profile dry-run-demo --include-reconciliation --notify-dashboard
```

Daemon notifications route phases by role: discovery to scanner, proposals to captain, dry-run execution to trader, risk anomalies to risk, and tick summaries to reviewer. Notifications are sent only after the SQLite tick commit succeeds, and unchanged daemon cards are suppressed unless `--notify-force` is passed.

Live auto remains off unless every gate is explicit:

```bash
uv run polymarket-weather operator daemon --once \
  --profile micro-live \
  --allow-profile-kind \
  --allow-live-auto \
  --live-market <market_id> \
  --include-reconciliation \
  --notify-dashboard
```

The live auto path still uses the allowlisted automation executor and existing `trade` command. It requires `micro-live`, `--allow-live-auto`, a whitelisted `--live-market`, risk guard status `ok`, fresh successful reconciliation by default, live credentials, hard risk caps, no nonzero positions by default, and a strategy override with `live_auto_enabled=True`.

## Audited Live Smoke Test

> [!WARNING]
> This is a controlled operator workflow for testing production endpoints. It requires live credentials and explicit human authorization.
> It does **not** mean automation is ready. Treat smoke tests as manual rehearsal before full-live autopilot.

The audited live smoke test submits a real, small limit order (e.g. `$0.50`) and optionally cancels it immediately. All results are audited in local SQLite via reconciliation. 
See `docs/runbooks/audited-live-smoke.md` for full steps.

**Prerequisites:**
- `live-readiness` passing
- `reconcile` fresh
- Circuit-breaker status `ok`
- `TRADING_DISABLED=false`

**What it tests:** 
CLOB token translation, limit order submission, order ID capture, immediately cancelling (optional), and exchange state reconciliation.

**What it does NOT test:**
Fill latency, profitability, closing positions, and Polymarket frontend visibility (which often hides orders <$5).

## Operator Exchange State

Reconciliation stores open orders, fills, and positions in SQLite. Use these read-only commands to inspect the latest local exchange snapshot before approving or enabling automation:

```bash
uv run polymarket-weather operator refresh-open-orders
uv run polymarket-weather operator open-orders
uv run polymarket-weather operator cancel-order <exchange_order_id>
uv run polymarket-weather operator positions
uv run polymarket-weather operator positions --nonzero-only
uv run polymarket-weather operator fills
```

The daemon includes the same counts in each tick result as `open_orders_count`, `positions_count`, `nonzero_positions_count`, and `fills_count`. Nonzero positions block live auto by default; `--allow-live-with-positions` is an explicit escape hatch, but it does not bypass `micro-live`, market whitelist, reconciliation freshness, strategy override, credentials, or trade risk checks.

## Strategy Overrides

Strategy overrides are market/profile-specific safety tightenings stored in SQLite. They can raise `min_edge`, lower order/day/market caps, and explicitly enable live auto for a market/profile pair. They cannot loosen profile caps or bypass hardcoded caps.

```bash
uv run polymarket-weather operator overrides
uv run polymarket-weather operator override-set --market <market_id> --profile micro-live --min-edge 0.12 --max-order-usdc 3 --live-auto --notes "tiny live test"
uv run polymarket-weather operator override-delete --market <market_id> --profile micro-live
```

Override precedence is exact market/profile first, then market wildcard, profile wildcard, then global wildcard. Missing `live_auto_enabled=True` means daemon live auto stays disabled even if every other live gate passes.

## Strategy Profiles

Profiles are safe workflow presets. They can choose defaults and tighten caps, but they cannot bypass credentials, reconciliation, or hard risk limits.

```bash
uv run polymarket-weather profiles list
uv run polymarket-weather profiles show conservative
uv run polymarket-weather operator go --profile conservative --propose-only
```

Built-in profiles are `balanced`, `conservative`, `research-only`, `dry-run-demo`, and `micro-live`. `micro-live` is the only profile eligible for daemon live auto execution and keeps order/day/market caps at 5/10/5 USDC before hard caps are applied.

## Queue Console

Use the queue commands when you need lifecycle detail instead of only the latest next step:

```bash
uv run polymarket-weather operator queue --status pending --kind dry_run
uv run polymarket-weather operator queue-summary
uv run polymarket-weather operator queue-detail act_...
uv run polymarket-weather operator queue-timeline act_...
uv run polymarket-weather operator queue-failed
```

## Advanced Commands

```bash
uv run polymarket-weather init-db
uv run polymarket-weather doctor
uv run polymarket-weather discover-markets --limit 100 --pages 3
uv run polymarket-weather markets
uv run polymarket-weather candidates
uv run polymarket-weather candidate-mark --market <market_id> --status reviewed --notes "looks good"
uv run polymarket-weather inspect-market <market_id>
uv run polymarket-weather refresh-weather --market <market_id>
uv run polymarket-weather analyze --market <market_id>
uv run polymarket-weather trade --market <market_id> --dry-run
uv run polymarket-weather orders
uv run polymarket-weather doctor --live
uv run polymarket-weather live-readiness
uv run polymarket-weather reconcile
uv run polymarket-weather risk-report
```

## Candidate Queue

Discovery and fixture loading persist reviewable market candidates:

```bash
uv run polymarket-weather candidates
uv run polymarket-weather candidates --status dry_run_ready
uv run polymarket-weather candidate-mark --market <market_id> --status reviewed --notes "looks good"
```

`dry_run_ready` candidates have a tradable parsed rule and enough seeded data for local dry-run testing. `rejected` candidates keep the parser or risk rejection reason so broad scans can be reviewed without guessing settlement rules.

## Fixture Dry Run

When live discovery has no short-cycle tradable weather market, use a raw market JSON fixture to test the local pipeline:

```bash
uv run polymarket-weather fixtures import-market-json data/demo-market.json --output-dir fixtures/markets
uv run polymarket-weather fixtures load-market-fixture fixtures/markets/<generated-fixture>.json --demo-analysis
uv run polymarket-weather trade --market <market_id> --dry-run
uv run polymarket-weather orders
```

`--demo-analysis` seeds a fixture-only quote, forecast, and analysis so `trade --dry-run` can verify risk checks and order-intent persistence without placing an order.

## Reconciliation Gate

Live trading requires a fresh successful reconciliation run. `reconcile` reads CLOB balances, open orders, trades/fills, and wallet positions, then stores local snapshots for exposure checks:

```bash
uv run polymarket-weather reconcile
uv run polymarket-weather risk-report
```

Without live credentials, `reconcile` records an `adapter-error` and live trading remains blocked. If any required adapter read fails or returns an unsupported shape, the run is not marked successful.

## Forecast Source Gate

Live trading accepts **official forecasts** (`source_grade=official_forecast`) by default,
e.g. NOAA/NWS forecast products. The explicit exception is `global_temp_bucket` in
`micro-live`: it may use a persisted Open-Meteo `research_forecast` when NOAA cannot
resolve the city. This exception does not apply to other modules and never promotes
the source label to official. Forecasts are not settlement observations.

| Grade | Meaning | Live trading |
|-------|---------|--------------|
| `official_forecast` | Official agency forecast product | Allowed |
| `research_forecast` | Open-Meteo / ensemble / research signal | Global temperature buckets in micro-live only; otherwise dry-run |
| `settlement_observation` | Observed weather for market resolution | Not a forecast; never used as live forecast input |
| `unknown` / `legacy` | Missing grade or old ambiguous `settlement_grade` rows | Reject live; refresh forecast |

Demo fixtures and Open-Meteo signals remain usable for discovery, analysis, and dry-run order intents. Legacy rows that only set `official_signal=true` or `source_grade=settlement_grade` are **not** promoted to live-eligible.

## Module Credibility & Live Eligibility

Each weather market module has clear promotion criteria for live trading:

| Module | Current Status | Requirements for Live |
|--------|----------------|----------------------|
| **weather** | `candidate_gate_required` | Rule confidence ≥ 0.85, NOAA data source, fresh reconciliation, whitelisted, override enabled |
| **china_temp_bucket** | `candidate_gate_required` | Rule confidence ≥ 0.85, official China weather data, fresh reconciliation, whitelisted, override enabled |
| **global_temp_bucket** | `micro_live_ready` | Rule confidence ≥ 0.85, persisted source provenance, fresh reconciliation, whitelist/override and micro-live risk caps |
| **precip_snow** | `dry_run_only` | NOAA/NWS official source, accumulation rules parsed, unit handling verified, time window tested |
| **hurricane_storm** | `research_only` | NHC official source, event type model, settlement rule parser, probability model |

### Checking Module Credibility

```bash
# View module credibility for a market
uv run polymarket-weather inspect-market <market_id>

# Check live readiness
uv run polymarket-weather live-readiness

# View Live Launchpad for all candidates
# Open http://127.0.0.1:8765?lang=zh
```

### Promotion Criteria

To promote a module from `dry_run_only` to `candidate_gate_required`:
1. Official data source verified
2. Rule confidence ≥ 0.85
3. Unit handling tested
4. Fresh reconciliation
5. Market whitelisted
6. Strategy override enabled

## Weather Data Source Strategy

The system uses different weather data sources depending on the market location. This is critical for live trading accuracy.

### Data Source Selection Rules

| Market Location | Primary Source | Source Grade | Use Case |
|-----------------|----------------|--------------|----------|
| **US Markets** | NOAA/NWS forecast | `official_forecast` | Live trading |
| **China Markets** | China Official Weather | `official_forecast` (when official) | Live trading (requires verification) |
| **Other Markets** | Open-Meteo | `signal_only` | Dry-run only |

### Why NOAA for US Markets?

NOAA is the official data source used by Polymarket for settlement. Verified accuracy:

```
NYC Temperature (June 3, 2026):
- Historical range (past 3 days): 72.3-78.1°F
- NOAA forecast: 75°F ✓ (within historical range)
- Open-Meteo forecast: 83.7°F ✗ (above historical range)
```

### NOAA Station Mapping

The system automatically maps cities to NOAA observation stations:

| City | NOAA Station | Gridpoint | Notes |
|------|--------------|-----------|-------|
| New York | KNYC | OKX/33,42 | Central Park |
| Los Angeles | KLAX | LOX/149,47 | LAX Airport |
| Chicago | KORD | LOT/76,73 | O'Hare Airport |
| Miami | KMIA | MFL/110,50 | Miami Airport |
| San Francisco | KSFO | MTR/85,105 | SFO Airport |
| Seattle | KBFI | SEW/125,68 | Boeing Field |
| Boston | KBOS | BOX/71,76 | Logan Airport |
| Washington DC | KDCA | LWX/97,71 | Reagan Airport |
| Denver | KDEN | BOU/64,62 | DIA Airport |
| Phoenix | KPHX | PSR/155,57 | Sky Harbor |

### Using NOAA Data

```bash
# Test NOAA for a specific city
uv run polymarket-weather inspect-market <market_id>

# The system automatically uses NOAA for US markets
# No manual configuration needed
```

### Multi-model Ensemble Forecast

The system reads local-day ensemble forecasts from GFS, ECMWF IFS, ICON EPS,
and GEM through Open-Meteo. Each numerical model first produces its own bucket
probability; models are then equally weighted so a model with more members does
not dominate the result. When NOAA is available, its deterministic forecast is
included as an additional reference source after its point temperature is
converted to a soft bucket probability using the existing lead-time error model.

An optional Google Weather API key adds a compact daily high/low forecast as one
deterministic pricing reference. It receives one model-level vote, matching the
existing NOAA/Open-Meteo deterministic reference, but its point forecast is not
treated as a binary `0` or `1` bucket probability. Each numerical ensemble family
also receives one vote regardless of member count. Google is never treated as the
market's official settlement source, and an API failure degrades cleanly to the
remaining models.

Polymarket's weather buckets settle on whole-degree values. Pricing therefore
uses mutually exclusive half-open intervals: an exact `25°C` bucket covers
`[24.5, 25.5)`, `90-91°F` covers `[89.5, 91.5)`, and the first/last buckets are
unbounded tails. A complete event's bucket probabilities must sum to `1` for
every contributing model before model-level averaging.

Entry uses one consensus rule for D0-D2: at least three model-level probabilities
must be available, at least two thirds must value the bucket above the current ask
plus the configured execution buffer, and the median model probability must clear
the configured net-edge threshold. D0 uses the same quorum and edge thresholds;
fresh verified observations condition every model on the maximum already observed
instead of imposing a separate local-noon cutoff.

**Key Features:**
- Four independent numerical model families with many perturbed members
- Equal model weighting for calibration, plus median probability and a two-thirds
  model quorum for entry; MAD/IQR disagreement remains visible in the audit interval
- Target-city IANA timezone and target local-day daily high/low
- Source grade: `research_forecast` (NOT `official_forecast` / not settlement observation)

**Usage:**

```bash
# Enable ensemble provider
export WEATHER_PROVIDER=open-meteo-ensemble

# Optional pricing reference; keep this only in .env or macOS Keychain
GOOGLE_WEATHER_API_KEY=your_restricted_google_weather_key

# Run analysis with ensemble
uv run polymarket-weather analyze --market <market_id>

# View ensemble context in dashboard
# Open http://127.0.0.1:8765/markets/<market_id>
# Check "Latest Forecast" section for ensemble details
```

**Dashboard Display:**
- Source Grade: `research_forecast` (warning style)
- Ensemble Mean: Average of all returned members
- Ensemble Std Dev: Standard deviation
- Ensemble Members: Number of returned members and models
- Ensemble Agreement: Fraction of members that agree

**Important Constraints:**
- ✅ Can be used for research and dry-run
- ✅ Source grade is `research_forecast`
- ✅ Global temperature buckets may use it as research evidence in the existing live workflow
- ✅ Google Weather contributes one deterministic pricing vote when configured
- ❌ Cannot be marked as `official_forecast`
- ❌ Google Weather never replaces an official settlement observation
- ❌ Threshold markets remain subject to their existing source-grade live policy

### Current Global Bucket Production Strategy

The production global-temperature workflow uses
`global-temp-bucket-multimodel-v8` with `weather-entry-v4`. It collapses
correlated weather feeds into independent source families, calibrates bias and
uncertainty by local forecast phase, conditions D0 probabilities on observed
weather, uses a conservative fee-aware probability for entry, and sizes orders
with bounded fractional Kelly. Production Autopilot does not call or price with
the LLM. See
[`docs/strategy/global-temp-bucket-v8.md`](docs/strategy/global-temp-bucket-v8.md)
for the complete strategy and safety contract.

### Ensemble Trading Strategies (Research Hypotheses - NOT Validated)

**⚠️ IMPORTANT: These are research hypotheses only, NOT validated strategies. They are for research/dry-run only and cannot be used for live trading.**

Based on initial testing, Ensemble forecasts may provide probability estimates. Two hypothetical strategies are proposed for research purposes:

#### Strategy 1: Conservative (Research Hypothesis)

**Concept:** Only trade when Ensemble and NOAA agree (hypothetical).

**Hypothetical Rules:**
- Ensemble P(>threshold) > 70%
- NOAA P(>threshold) > 70%
- Both agree on direction
- Min edge: 5%

**Hypothetical Example:**
```
Ensemble: 81.3°F, P(>80°F) = 0.90
NOAA: 81°F, P(>80°F) = 0.80
Market price: YES @ 0.80
Action: Buy YES @ 0.80 (hypothetical)
Reason: Both agree, high probability, low risk
```

**Hypothetical Pros:**
- Low risk
- High win rate
- Stable returns

**Hypothetical Cons:**
- Fewer opportunities
- Limited returns

#### Strategy 2: Aggressive (Research Hypothesis)

**Concept:** Rely mainly on Ensemble prediction, even if NOAA disagrees (hypothetical).

**Hypothetical Rules:**
- Ensemble P(>threshold) > 80%
- NOAA P(>threshold) can be < 30%
- Min edge: 10%
- Position size: 5-10 USDC

**Hypothetical Example:**
```
Ensemble: 81.3°F, P(>80°F) = 0.90
NOAA: 76°F, P(>80°F) = 0.10
Market price: YES @ 0.10
Action: Buy YES @ 0.10 (hypothetical)
Reason: Ensemble more accurate, high potential return
```

**Hypothetical Expected Value:**
```
90% chance to earn 0.90 USDC = 0.81 USDC
10% chance to lose 0.10 USDC = -0.01 USDC
Expected value = 0.80 USDC (hypothetical)
```

**Hypothetical Pros:**
- More opportunities
- Higher returns
- Leverages Ensemble accuracy

**Hypothetical Cons:**
- Higher risk
- May lose money
- Requires more capital

#### Strategy Comparison (Hypothetical)

| Aspect | Conservative | Aggressive |
|--------|--------------|------------|
| Risk Level | Low | High |
| Expected Return | Medium | High |
| Win Rate | High | Medium |
| Position Size | 1-5 USDC | 5-10 USDC |
| Diversification | 3-5 markets | 5-10 markets |
| Stop Loss | Not needed | 10-20% |

#### Risk Management (Hypothetical)

**Conservative:**
- Position size: 1-2% of total capital
- Diversify across 3-5 markets
- No stop loss needed

**Aggressive:**
- Position size: 3-5% of total capital
- Diversify across 5-10 markets
- Set 10-20% stop loss

#### Important Notes

1. **These strategies are NOT validated** - They are research hypotheses only
2. **Cannot be used for live trading** - Ensemble source_grade is research_forecast
3. **Requires further testing** - Need more data to validate effectiveness
4. **For research/dry-run only** - Use for learning and experimentation

#### Usage

```bash
# Enable ensemble provider
export WEATHER_PROVIDER=open-meteo-ensemble

# Analyze market
uv run polymarket-weather analyze --market <market_id>

# View ensemble vs NOAA comparison
# Open http://127.0.0.1:8765/markets/<market_id>
# Compare ensemble and NOAA predictions

# Choose strategy based on your risk tolerance
```

### Data Source Validation

The module credibility service checks:
1. Data source matches market location
2. Source grade is `official_forecast` for live trading
3. Forecast freshness is within acceptable range

**US Markets**: Must use NOAA/NWS
**China Markets**: Must use official weather stations
**Other Markets**: Can use Open-Meteo (dry-run only)

## Ops: CI, systemd, backups

The repository ships a GitHub Actions workflow that runs lint and tests on pushes to `main` and pull requests. For a small HK VPS deployment, use `docs/hk-vps-production-checklist.md`, the minimal `deploy/env/hk-live.example.env`, and `deploy/systemd/` for Autopilot, dashboard, and daily SQLite backup unit templates. Optional endpoint, retry, LLM, stream, and legacy module overrides live in `.env.advanced.example`.

Run the safe dry-run to live-readiness rehearsal before enabling live mode:

```bash
python scripts/rehearse_live_readiness.py
```

Create an online SQLite backup without stopping the bot:

```bash
uv run polymarket-weather backup-db --output-dir backups --retention 3
```

The backup command uses SQLite's backup API and prunes old `*.sqlite3` backup files by label, so it is safe to run from a systemd timer while the daemon is active.

## Order Lifecycle Management

The system provides comprehensive order lifecycle management:

### Stale Order Detection

Detect open orders that have been pending too long:

```bash
# View open orders
uv run polymarket-weather operator open-orders

# The system automatically detects stale orders (>5 minutes)
# Shown in the dashboard with age information
```

### Position Exposure

Track your exposure across markets:

```bash
# View all positions
uv run polymarket-weather operator positions

# View only non-zero positions
uv run polymarket-weather operator positions --nonzero-only

# View position exposure summary
# Open http://127.0.0.1:8765/positions
```

### Fill Summary

Track your trading activity:

```bash
# View recent fills
uv run polymarket-weather operator fills

# The dashboard shows:
# - Total fills in last 7 days
# - Total volume
# - Total fees
```

### Live Launchpad

The Live Launchpad shows:
- Why a market can/cannot go live
- Which gates are missing
- Next CLI command to run
- Maximum loss preview

```bash
# Open Live Launchpad
# Open http://127.0.0.1:8765?lang=zh
```

## Discord Dashboard Notifications

The coordinator repo can act as a lightweight Discord UI dashboard. These commands run local read-only checks and post summaries through `$HOME/agent-discussion-coordinator` (override with `AGENT_COORDINATOR_DIR`):

```bash
python3 scripts/notify_dashboard.py tests
python3 scripts/notify_dashboard.py discovery --limit 100 --pages 3
python3 scripts/notify_dashboard.py risk
python3 scripts/notify_dashboard.py queue
python3 scripts/notify_dashboard.py reconciliation
python3 scripts/notify_dashboard.py propose dry-run --market demo-weather-nyc-high-2026-05-08 --reason "review dry-run candidate"
python3 scripts/notify_dashboard.py tick --limit 100 --pages 3
python3 scripts/notify_dashboard.py daemon --payload-file daemon-event.json
```

`tick` runs discovery, candidate queue, and risk notifications together and suppresses unchanged cards unless `--force` is passed. Add `--include-reconciliation` only when you want the tick to use live credentials for a reconciliation status card. The `operator daemon --notify-dashboard` command uses the same notification bridge and duplicate suppression internally.

Dashboard notifications are read-only. They may propose future actions, but live trading still requires local CLI risk checks, reconciliation gates, and human approval. `propose` sends an approval card only; it never runs the proposed command.

Live trading is intentionally adapter-gated. Use a dedicated small wallet and review dry-run output first. Live orders require fresh successful reconciliation plus all local risk checks.
