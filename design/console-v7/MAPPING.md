# Console v7 → `dashboard_ui` mapping

Visual source of truth: `design/console-v7/weather-autopilot-console-v7.html`  
Production renderer: `src/polymarket_weather_arb/dashboard_ui/app.py` (`render_app`)  
Stream + magazine strip styles: `src/polymarket_weather_arb/dashboard_ui/stream_panel.css`  
Stream helpers: `src/polymarket_weather_arb/dashboard_ui/stream_panel.py`  
Brand mark: `src/polymarket_weather_arb/dashboard_ui/assets/brand-mark.png`

**Do not** port design-only demo bars, fake evt/s generators, scenario switchers,
fake position mid-price drift, or hardcoded KPI numbers into production.

## Layout order (v7 hierarchy)

1. Sticky header (brand avatar + run/mode/tick chips + language)
2. Onboarding / compact command toolbar
3. Alert region (stale tick / blockers)
4. Ops health rail (checks + safety folded when operating) — or dual checks/safety on first run
5. **Finance KPI strip** (`#panel-finance`)
6. **Stream monitor** (`#panel-stream` / `data-od-id="panel-stream-live"`)
7. **Position status stream + Opportunity funnel stream** (magazine strips)
8. Setup disclosure / more-ops (stats / ranked / advanced / remote / runs)

## `data-od-id` → renderer

| Design id | Production | Notes |
|-----------|------------|--------|
| `panel-position-stream` | `_exit_policy_panel` | Magazine `strip-item` rows; real ExitGuardian data |
| `panel-funnel-stream` | `_opportunity_funnel_panel` | Magazine `funnel-strip` stages; real 24h funnel counts |
| `row-position-funnel-streams` | mid grid wrapper | Also keeps `data-od-row="row-positions-funnel"` |
| `panel-stream-live` | `render_stream_monitor_panel` | Unchanged contract |
| `stream-feed` | decision feed | From `snapshot.decisions` only |
| `funnel-steps` | funnel strip list | Same 8 real stages as before |
| `positions-stream` | strip list | Replaces old `positions-table` |

## V7 visual language (must match design)

Shared with event feed:

1. Left 3px cyan rail (`.mag-stream::before`)
2. Uppercase kicker (`.mag-kicker`)
3. `line1` primary / `line2` secondary
4. Mono tabular numbers
5. `is-fresh` / `is-hot` left cyan sweep — not whole-card flash
6. Magazine density (not denser tables)

### Position strip columns

`idx | stage pill | market+next | qty/cost/mid | mark PnL`

### Funnel strip columns

`step# | label + drop% + thin track | count`

## Backend contracts (must keep)

- `AutopilotService.snapshot()` for mode / ticks / blockers / decisions
- `build_cockpit_snapshot()` → `VerifiedRealizedPnL` + `OpportunityFunnel`
- `ExitGuardianService.evaluate()` for position ladder (kind == position)
- Safety caps: `min(settings, HARDCODED_MAX_*)`
- No trading logic changes in this UI pass

## Forbidden in production

- Demo scenario switcher / fake evt/s / fake position mid drift timers
- Hardcoded fake KPI / position / funnel numbers
- Removing safety gate / trading-disabled semantics
- Nesting cards inside cards beyond existing panel structure
- Reverting positions/funnel to dense HTML tables

## Acceptance checklist

- [ ] `/app?lang=zh` shows 持仓状态流 + 机会漏斗流 as strip lists (no position/funnel data tables)
- [ ] `.mag-stream`, `.strip-item`, `.funnel-strip` present in HTML/CSS
- [ ] Finance → Stream → Position/Funnel streams order preserved
- [ ] Empty positions still render mag-stream empty state
- [ ] Funnel stages use real cockpit counts; conversion % = fill/discovered
- [ ] No demo-bar / 状态演示 / fake throughput
- [ ] ruff + dashboard tests pass
