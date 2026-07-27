# Console v4 → `dashboard_ui/app.py` mapping

Visual source of truth: `design/console-v4/weather-autopilot-console-v4.html`
Production renderer: `src/polymarket_weather_arb/dashboard_ui/app.py` (`render_app`)

**Do not** port the design-only demo bar, scenario tabs, or collapsed engineering spec block into production.

## Layout order (v4 hierarchy)

1. Sticky header metrics (`run / mode / last tick / ticks`)
2. Path rail + command hero
3. Alert region (stale tick / blockers)
4. Mode panel
5. Checks + safety dual grid
6. **Finance center** (`#panel-finance` / verified PnL KPI strip) — primary visual weight
7. Positions (exit ladder) + opportunity funnel mid grid
8. Stats / ranked opportunities / advanced / remote (secondary)
9. Recent runs (aux)

## `data-od-id` → renderer

| Design id | Production | Notes |
|-----------|------------|--------|
| `app-header` | `_app_shell` header | Sticky glass header |
| `brand` | brand block | Weather Autopilot |
| `top-metrics` / `chip-run` / `chip-mode` / `chip-tick` | header metric chips | Live status via `aria-live` |
| `header-actions` | language switcher | zh / en |
| `btn-toggle` | `_hero_command` toggle form | POST `/app/toggle` |
| `btn-tick` | `_hero_command` tick form | POST `/app/tick` |
| `btn-clear-runs` | reset-history form | confirm dialog kept |
| `alert-warn` / `alert-danger` | `_alert_region` | stale / blockers |
| `panel-checks` / `check-list` | `_first_run_panel` | first-run checks |
| `panel-safety` / `valves-list` | `_safety_gate` | caps + gate status (read-only) |
| `panel-finance` / `kpi-grid` | `_verified_pnl_panel` | Verified + Estimated tags |
| `kpi-realized` | `total_realized_pnl` | Verified sold-fill PnL |
| `kpi-unrealized` | `total_open_estimated_pnl` | Campaign MtM estimate |
| `kpi-pos-value` | `total_open_current_value` | Open position value |
| `kpi-exposure` | `total_reconciled_exposure` | Reconciled exposure |
| `recon-pill` / `recon-bar` | reconciliation freshness | Fresh / Stale only |
| `panel-positions` | `_exit_policy_panel` | Exit ladder, not realized PnL |
| `panel-funnel` / `funnel-steps` | `_opportunity_funnel_panel` | 24h funnel stages |
| `panel-runs` | `_decisions_panel` | Read-only recent runs |
| `main` | `<main id="main">` | skip-link target |

## Backend contracts (must keep)

- `AutopilotService.snapshot()` for mode / ticks / blockers / first-run checks
- `build_cockpit_snapshot()` → `VerifiedRealizedPnL` + `OpportunityFunnel`
- `ExitGuardianService.evaluate()` for position ladder
- Safety caps: `min(settings, HARDCODED_MAX_*)`
- No trading logic changes in this UI pass

## Forbidden in production

- Demo scenario switcher / `data-scenario=*`
- Hardcoded fake KPI numbers
- Removing safety gate / trading-disabled semantics
- Nesting cards inside cards beyond existing panel structure

## Acceptance states

| State | `body[data-run-state]` | Header chip | Alerts |
|-------|------------------------|-------------|--------|
| Running | `running` | green Running | none |
| Paused | `paused` | amber Paused | none |
| Stale | `stale` | grey Stale | warn strip |
| Blocked | `blocked` | red Blocked | danger strip with blockers |
| Empty finance ledgers | n/a | n/a | empty ledger copy inside finance panel |
