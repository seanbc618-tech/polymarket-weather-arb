# Full-Live Autopilot Runbook

Arms **both** sides of the `/app` loop under explicit operator control:

```text
full-auto / full_live =
  discover -> quote/forecast/analyze -> rank -> reconcile
  -> manage stale orders -> automatically exit invalid positions
  -> submit the best eligible live BUY -> persist/notify -> repeat
```

LLM is **not** required. Quantitative analysis drives entry.

Global temperature buckets use Strategy V2 entry behavior:

- combine GFS, ECMWF IFS, ICON EPS, GEM, and an available configured reference
  forecast into an equal-model probability estimate;
- score both a high-probability `core` lane and a cheap asymmetric `tail` lane;
- cap cumulative entry at 1 USDC on D2, then 2 USDC on D1/D0;
- keep one active bucket per city/date;
- switch only when another bucket's strategy score leads by at least 0.15, and
  always exit/reconcile the old bucket before the new BUY can occur;
- on D0, require a fresh verified official observed maximum and floor forecast
  outcomes at that value; D0 then uses the same two-thirds model quorum and
  median net-edge threshold as D1/D2, without a local-noon cutoff.

The LLM reviews the selected candidate before the quantitative gate and does not
receive the quant engine's action/edge. Its independent opinion is persisted for
diagnosis, but it cannot promote a failed quant candidate, veto an accepted one,
or submit an order.

Auto-exit uses the **profit-protection ladder** in `ExitGuardianService` and
submits only through `PositionExitService` (limit SELL):

```text
settlement_pending / review_no_analysis / hold_*  -> never SELL
recover_principal -> partial SELL (minimum fee-adjusted size)
exit_full         -> full residual SELL
hold_runner       -> hold after principal recovered
```

Inventory accounting is recomputed each tick from reconciled fills (current
campaign after the last zero-position crossing), preferring `_account_fill`
and intent→order-id token linkage. Unverified ledgers never trigger
principal-recovery SELL; model reversal may still full-exit.

Pre-submit AutoExit refreshes the token book and **re-runs ExitGuardian** at
the new bid (size, fees, dust). If principal can no longer be recovered, no
SELL is sent. Top-of-book bid depth is used when present on the CLOB payload.

This is **not** a claim of guaranteed profit.

## Defaults stay safe

Without `--full-auto` / selecting Full live + Start, nothing changes: paper/observe
defaults. Full live always includes automatic exits; micro live still uses the
optional `AUTO_EXIT_ENABLED` switch.

## Prerequisites

1. Manual micro BUY→SELL roundtrip already proven on this wallet (recommended).
2. `.env`:
   ```env
   TRADING_DISABLED=false
   MAX_AUTO_EXITS_PER_TICK=1
   AUTO_EXIT_MAX_SLIPPAGE=0.02
   # LIVE_MARKET_IDS optional — leave empty to allow all local weather candidates
   # Full live uses configured MAX_ORDER_USDC / MAX_DAILY_USDC / MAX_MARKET_USDC / MIN_EDGE
   # (still hard-capped in code; does not inherit micro-live 5/10/5 or MIN_EDGE=0.10)
   POLYMARKET_PRIVATE_KEY=...
   POLYMARKET_FUNDER=...
   LLM_ENABLED=false
   # Weather uses REST quotes plus a mandatory REST read immediately before a live order.
   # Keep the exchange WebSocket off; the /app local SQLite event stream remains active.
   POLYMARKET_MARKET_STREAM_ENABLED=false
   ```
3. Markets exist in local DB (run discover/analyze first if needed).
4. Fresh successful reconciliation and open resolution circuit breaker.

### Risk-cap versus dynamic-entry sizing

`MAX_ORDER_USDC`, `MAX_MARKET_USDC`, and `MAX_DAILY_USDC` are hard ceilings,
not fixed order sizes. Global temperature buckets first receive a cumulative
horizon budget (D2 up to 4 USDC; D1/D0 up to 10 USDC), then the existing
fee-aware entry policy reduces that budget for marginal Edge, exact-quorum
support, model dispersion, stale weather, low-price buckets, and calibrated
history. Each submitted order is still capped by `MAX_ORDER_USDC`, and total
bucket/day exposure remains capped by the configured market/day values.

Minimum-order rejections report the **effective entry headroom** after these
reductions. That value can legitimately be lower than `MAX_ORDER_USDC`.

## Local production startup

```bash
uv run polymarket-weather reconcile
uv run polymarket-weather live-readiness
uv run polymarket-weather autopilot start --full-auto
```

Open `http://127.0.0.1:8765/app?lang=zh`.

Persistent Autopilot logs (redacted rotating file) are written next to the DB:

```text
<parent of DATABASE_PATH>/logs/autopilot.log
```

`autopilot start --full-auto` and the `/app` background thread both:

- initialize that log file;
- set `autopilot_state.process_started_at` so `/app` shows process liveness.

Equivalent one-tick CLI check:

```bash
uv run polymarket-weather autopilot start --full-auto --once
```

### Tick order (capital path first)

Within each tick when live:

1. reconciliation (fail-stop if not `ok`);
2. stale-order lifecycle;
3. position analysis refresh / expired settlement routing (read-only, no forecast);
4. full-live auto-redeem of at most one freshly verified winner;
5. auto-exit, unless a redeem was attempted in this pulse;
6. discovery + global research **only if remaining cycle budget allows**;
7. select / at most one BUY.

Expired or past-local-day positions do not call forecast APIs. Settlement state is
recorded as analysis `decision=skip` with a settlement reason; resolution audit
is **not** invoked on this path (avoids circuit-breaker side effects).

Full-live can automatically redeem a resolved winning condition through the
official SDK. It requires a successful fresh reconciliation, a fresh official
Polymarket response proving the held YES/NO outcome is the unique winner, an
available `conditionId`, a clear breaker, and a supported wallet transaction
path. The production Deposit Wallet additionally requires all three secrets:

```text
BUILDER_API_KEY
BUILDER_SECRET
BUILDER_PASS_PHRASE
```

The service durably writes `auto_redeem=prepared` before mutation and commits
the relayer transaction identifiers immediately after submission. A
`submitted_unverified` or stranded `prepared` decision blocks automatic replay;
reconcile and investigate it. Never substitute a direct signer-EOA redemption
for positions owned by a different `POLYMARKET_FUNDER`.

Weather target-date eligibility uses the market city/station **local calendar
day** (IANA timezone), not UTC midnight alone.

### Global city discovery

The built-in city tuple is only a cold-start seed. Full-auto reads the current
Polymarket weather page with a format-tolerant event-slug extractor, learns new
cities from active events, and persists their event slug, settlement station,
and verified IANA timezone in the existing market/rule tables. On later starts,
that SQLite catalog regenerates D0-D2 event slugs even if the public page is
temporarily unavailable.

A newly observed city is eligible for the global temperature strategy only when
the market rule remains unambiguous, Open-Meteo resolves a valid IANA timezone,
and any declared ICAO settlement station is confirmed by AviationWeather.gov.
Qualification failure leaves the market in review/skip state; it never falls
back to UTC and never expands the live whitelist by itself.

`--full-auto` automatically sets:

| Item | Value |
|------|--------|
| app_mode | `full_live` |
| profile | `full-live` |
| auto-exit | always included in `full_live`; `AUTO_EXIT_ENABLED` is micro-live only |
| whitelist | **open** if no ids given; otherwise restricted |
| live_auto override | none; `app_mode=full_live` is the source of truth |
| entry | at most one new eligible live BUY per tick via `TradingService` |
| exit | AutoExit → official-impossibility/risk `exit_full` only via `PositionExitService`; models always HOLD |
| redeem | one fresh verified winner per capital pulse through the official SDK; ambiguous submissions never replay |
| order lifecycle | `OrderLifecycleService` cancels stale open orders when policy says so |

### V5 settlement-only examples

| Situation | Action |
|-----------|--------|
| BUY 100 @ 0.01, bid 0.20 | HOLD; profit is not a SELL signal |
| Model side reverses twice | HOLD; model-only exits are disabled |
| Edge becomes negative | HOLD; entry thresholds do not manage existing positions |
| Reliable official observation makes the held bucket impossible | `exit_full` through the one SELL owner |
| Market is resolved and held bucket won | settlement route, then guarded auto-redeem |
| `settlement-route-v1` / closed market | `settlement_pending` (no SELL) |
| Stale/missing analysis | `review_no_analysis` (no SELL) |

### Audit / recovery commands

```bash
uv run polymarket-weather reconcile
uv run polymarket-weather risk-report
# Inspect SQLite automation_actions / order_intents / fills for exit audit
sqlite3 "$DATABASE_PATH" "SELECT id,status,reason FROM automation_actions WHERE kind='auto_exit' ORDER BY created_at DESC LIMIT 20;"
```

### Runtime efficiency diagnostics (auth, phases, recon stage)

Persistent rotating log (same path as process liveness):

```text
<parent of DATABASE_PATH>/logs/autopilot.log
```

Inspect auth bootstrap count (one create/derive sequence per process is normal;
four sequences per tick means session reuse is broken):

```bash
rg -n "creating authenticated SecureClient|api-key|derive-api-key" \
  "$(dirname "$DATABASE_PATH")/logs/autopilot.log" | tail -50
```

Inspect per-phase durations and counts (logged each tick; no secrets):

```bash
rg -n "tick phase=|tick complete" \
  "$(dirname "$DATABASE_PATH")/logs/autopilot.log" | tail -80
```

Inspect reconciliation failure stage (fail-stop blocks cancel/exit/entry):

```bash
rg -n "reconciliation adapter-|failed_stage|stage=" \
  "$(dirname "$DATABASE_PATH")/logs/autopilot.log" | tail -40
sqlite3 "$DATABASE_PATH" \
  "SELECT id,status,substr(details,1,200),created_at FROM reconciliations ORDER BY id DESC LIMIT 5;"
```

Inspect total tick duration and deferred candidates (also on `/app` state):

```bash
sqlite3 "$DATABASE_PATH" \
  "SELECT last_tick_at,last_tick_status,last_tick_duration_ms,deferred_candidates_count,last_error FROM autopilot_state WHERE id=1;"
rg -n "tick complete|deferred=" \
  "$(dirname "$DATABASE_PATH")/logs/autopilot.log" | tail -40
```

Expected after the runtime-efficiency work:

- discovery does not call CLOB books for every Gamma-quoted bucket;
- reconciliation reuses one authenticated SDK session across balances/orders/trades/positions;
- repeated identical recon failures log every tick but send one Telegram transition;
- recovery from recon failure sends one Telegram recovery event.

Telegram remains material-only: SELL submitted, fills, material failures — not every runner hold.

### Advanced operator daemon

`operator daemon` is retained for diagnostics and legacy queue workflows. It is
not the production full-auto entry point and must not run beside `autopilot start`.

## Stop

1. Use `/app` **Pause** or Ctrl-C the process.
2. Set `TRADING_DISABLED=true` for an execution kill switch.
3. Inspect `/orders`, `/positions`, and the latest reconciliation before restart.

Also useful: set `LIVE_MARKET_IDS` to narrow entry. Full live intentionally has
no buy-only posture; use **Pause** or `TRADING_DISABLED=true` to stop automation.

## Process liveness vs Autopilot liveness

A running Python process is **not** proof that Autopilot is healthy. `/app` shows a
**stale last-tick** warning when the last successful/failed cycle is older than two
configured tick intervals.

## What you should observe

- **No trade signal:** idle / skip — normal; no Telegram heartbeat
- **Trade signal + gates ok:** limit BUY via `TradingService`
- **Position + position_at_risk:** limit SELL via auto-exit
- **hold_position:** no auto sell
- **Stale open orders:** lifecycle cancel when policy requires it
- **Full-live exit larger than AUTO_EXIT_MAX_POSITION_USDC:** allowed only as a
  reduction of the actual reconciled position (still limit-only, no oversell)
- **Material Telegram only:** live BUY/SELL submitted, fill confirmed,
  submitted-unverified/reconcile failure, breaker trip / loop fatal/recovered

Profit is not a success metric for the first runs; fills, skips, and audit trails are.
