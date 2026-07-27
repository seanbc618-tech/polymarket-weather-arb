# Console v5 → `dashboard_ui/app.py` mapping

Visual source of truth: `design/console-v5/weather-autopilot-console-v5.html`  
Production renderer: `src/polymarket_weather_arb/dashboard_ui/app.py` (`render_app`)  
Stream helpers: `src/polymarket_weather_arb/dashboard_ui/stream_panel.py`  
Brand mark: `src/polymarket_weather_arb/dashboard_ui/assets/brand-mark.png`

**Do not** port design-only demo bars, fake evt/s generators, scenario switchers, or hardcoded KPI numbers into production.

## Layout order (v5 hierarchy)

1. Sticky header (brand avatar + run/mode/tick chips + language)
2. Onboarding / compact command toolbar
3. Alert region (stale tick / blockers)
4. Checks + safety dual grid
5. **Finance KPI strip** (`#panel-finance`) — ledgers collapsed under disclosure
6. **Stream monitor** (`#panel-stream` / `data-od-id="panel-stream-live"`) — primary visual weight
7. Positions (exit ladder) + opportunity funnel mid grid
8. Setup disclosure / stats / ranked / advanced / remote (secondary)
9. Recent runs (aux)

## `data-od-id` → renderer

| Design id | Production | Notes |
|-----------|------------|--------|
| `app-header` / topbar | `_app_shell` header | Sticky glass header |
| `brand` | brand block + `brand_mark_html()` | PNG avatar, not gradient square |
| `top-metrics` / chips | header metric chips | Live status via `aria-live` |
| `panel-finance` / `kpi-grid` | `_verified_pnl_panel` | Verified + Estimated tags; ledgers in `<details>` |
| `panel-stream-live` | `render_stream_monitor_panel` | Real decisions + funnel SVG |
| `stream-feed` | decision feed | From `snapshot.decisions` only |
| `panel-positions` | `_exit_policy_panel` | Exit ladder |
| `panel-funnel` | `_opportunity_funnel_panel` | 24h funnel stages |
| `panel-runs` | `_decisions_panel` | Read-only recent runs |
| `main` | `<main id="main">` | skip-link target |

## Backend contracts (must keep)

- `AutopilotService.snapshot()` for mode / ticks / blockers / first-run checks / decisions
- `build_cockpit_snapshot()` → `VerifiedRealizedPnL` + `OpportunityFunnel`
- `ExitGuardianService.evaluate()` for position ladder
- Safety caps: `min(settings, HARDCODED_MAX_*)`
- No trading logic changes in this UI pass

## Forbidden in production

- Demo scenario switcher / fake evt/s throughput animation
- Hardcoded fake KPI numbers
- Removing safety gate / trading-disabled semantics
- Nesting cards inside cards beyond existing panel structure

## Acceptance states

| State | `body[data-run-state]` | Header chip | Stream LIVE pill |
|-------|------------------------|-------------|------------------|
| Running | `running` | green Running | cyan LIVE |
| Paused | `paused` | amber Paused | neutral |
| Stale | `stale` | grey Stale | neutral |
| Blocked | `blocked` | red Blocked | neutral |
