# Runtime Retrospective Baseline — 2026-07-17

**Purpose:** Frozen snapshot after the first full-auto endurance review.
Use this file as the **before** side when writing the next retro (after a longer run).

**Review time:** 2026-07-17 ~12:43–13:03 CST
**Process left running:** yes (`autopilot start --full-auto`, no stop/restart)
**Reviewer method:** read-only (`ps`, SQLite, logs). No config/code changes for this baseline.

---

## 1. Run identity (compare these first)

| Field | Baseline value |
|---|---|
| Command | `uv run polymarket-weather autopilot start --full-auto` |
| PID (at review) | 2690 (parent `uv` 2687) |
| `process_started_at` (UTC) | `2026-07-16T18:38:29.210164+00:00` |
| Local start | 2026-07-17 **02:38 CST** |
| Elapsed at review | **~10.1–10.4 h** (user expected 12h+; actual session shorter) |
| `mode` / `app_mode` | `live` / `full_live` |
| `tick_seconds` (config) | 300 |
| `tick_count` | **2951** (hybrid/pulse much faster than 300s) |
| `last_tick_status` | `ok` |
| `last_error` | empty |
| Circuit breaker | **not tripped** |
| Open orders | **0** |
| CLOB collateral | **≈ 121.75 USDC** (`balance=121754784` micro) |
| DB path | `data/polymarket_weather.db` (~545MB + WAL) |
| Log | `data/logs/autopilot.log` (+ rotated `.1` / `.2`) |

### Continuity check for next retro

- Same PID / same `process_started_at` → continuous session; extend metrics only.
- Different `process_started_at` → new session; report **session B** vs this baseline, and optionally merge windows.

---

## 2. Headline verdict (baseline)

| Dimension | Score | One-liner |
|---|---:|---|
| Process stability | 9/10 | Continuous full-auto; healthy ticks; no crash |
| Execution plumbing | 8/10 | Limit orders, reconcile, risk, auto-exit all fire |
| Strategy EV | 3/10 | Multi-hour realized negative; weak entry quality |
| Runtime efficiency | 5/10 | Heavy capital-maintenance + 404 + reject noise |
| Risk controls | 8/10 | 1 USDC market cap works; CB idle |
| Observability | 7/10 | SQLite/logs enough; PnL must be reconstructed from fills |

**One sentence:** Execution machine is solid; edge engine is not yet proven under micro-live.

---

## 3. Session metrics (this process only)

Window: `created_at/filled_at >= 2026-07-16T18:38:29` (UTC).

### 3.1 Decision funnel

| status / action | count |
|---|---:|
| `ok` / `skip` | ~1464 |
| `skipped` / `skip` | ~385 |
| `failed` / `skip` | ~234 |
| `rejected` / `buy_yes` | **113** |
| `executed` / `buy_yes` | **2** |
| `skipped` / `settlement_pending` | 5 |
| `blocked` / `skip` | 2 |
| **decisions total** | **~2205** |

### 3.2 Reason buckets (coarse)

| Bucket | count | Note |
|---|---:|---|
| `capital_maintenance_ok` | ~1062 | Dominant healthy idle |
| `missing_forecast_defer` | ~294 | Slow-refresh backlog |
| `dead_book_404` | ~224 | Expired markets still on hot path |
| `slow_refresh` | ~222 | Discovery/rotation |
| `cached_reprice_ok` | ~180 | Reprice without full recompute |
| `exposure_cap_reject` | **113** | Same markets after 1U filled |
| `stale_forecast_defer` | ~91 | Forecast freshness |
| recon `adapter-error` block | 10 | Timeout / reset / SSL |
| live submitted | 2 | Both buy_yes @ 0.001 |
| geoblock connection reset | 2 | Transient |

### 3.3 Fills (session)

| Time UTC | Side | Market | px × size | Notional |
|---|---|---|---|---:|
| 00:17:40 | SELL | Shanghai 39°C Jul 17 (`2930677`) | 0.302 × 5.8 | +1.75 |
| 00:30:26 | BUY | Atlanta 90–91°F Jul 16 (`2929463`) | 0.001 × 1000 | −1.00 |
| 03:57:14 | SELL | Shanghai 38°C Jul 18 (`2942536`) | 0.12 × 10 | +1.20 |
| 04:05:16 | BUY | Dallas 90–91°F Jul 16 (`2929453`) | 0.001 × 1000 | −1.00 |

- Session cash flow (fees included): buy 2.00 − sell 2.95 − fees 0.21 ≈ **+0.74 USDC cash**
- Cash ≠ realized PnL (open inventory still held)

### 3.4 Positions at baseline

| market_id | Title | size | mark notional | close_time UTC |
|---|---|---:|---:|---|
| 2929453 | Dallas 90–91°F Jul 16 | 1000 YES | 0.50 | 2026-07-16T12:00:00Z |
| 2929463 | Atlanta 90–91°F Jul 16 | 1000 YES | 0.50 | 2026-07-16T12:00:00Z |
| 2942490 | Seoul 26°C Jul 18 | 6.25 YES | 0.97 | 2026-07-18T12:00:00Z |
| 2916005 | Miami 90–91°F Jul 15 | 68.96 YES | **0.00** | 2026-07-15T12:00:00Z |
| 2917167 | Chengdu 35°C Jul 16 | 12.9 YES | **0.00** | 2026-07-16T12:00:00Z |

### 3.5 Volume / coverage

| Metric | value |
|---|---:|
| analyses (session / 12h) | ~13997 |
| market_snapshots (session) | ~38734 |
| weather markets / candidates | 1024 / 1023 |
| reconciliations ~12h ok / adapter-error | ~1067 / 10 |
| risk accepted / rejected (broader 12h window) | 12 / 113 |
| order intents non-reject live (session) | small; 113 rejects dominate |

---

## 4. Longer-window trading context (~33h fills)

Not limited to this process; useful as strategy context.

- Closed round-trips (rough, fee-adjusted): **8 markets, sum realized ≈ −5.90 USDC**
- Notable winner: Shanghai 38°C Jul 18 buy@0.10 → sell@0.12 ≈ **+0.10**
- Pattern: several entries auto-exited at a loss (NYC / Miami / Chicago / Atlanta buckets)
- Auto-exit path **works** (`exit_full` / `recover_principal`); entries often do not pay fees + adverse move

---

## 5. Priority findings (open at baseline)

### P0 — Model / LLM / observation disagreement on live buys

Both session live BUYs:

- Action: `buy_yes` @ **0.001**, reported edge **~0.91**
- LLM (DeepSeek): text recommends **`buy_no`** — obs max already **≥91.9°F**, outside 90–91 bucket
- Ensemble still produced high YES probability on Dallas (`model_probability_mean≈0.855` near fill time)
- Atlanta later analyses moved to low probability / `settlement_pending`
- Mark notional ~0.5 on 1000 shares → YES nearly worthless, inconsistent with edge 0.91

**Hypothesis for next retro:** observation-conditioned pricing missing or wrong local-day; LLM veto not hard-gated.

### P1 — Dead books on hot path

Repeated `order book refresh failed: 404` for Miami Jul 15 / Chengdu Jul 16 while positions show mark 0.

### P2 — Exposure-cap reject storm

113× `market exposure exceeds 1 USDC cap` after $1 fills — risk correct, selection loop wasteful.

### P3 — Forecast freshness / rotation backlog

Hundreds of missing/stale forecast defers; `rotation_backlog` often tens of groups.

### P4 — Transient reconcile network errors

10 adapter-errors (timeout, connection reset, SSL handshake); recovered; can briefly block entry/exit.

---

## 6. Comparison checklist (for next retro)

Fill the right column when re-reviewing. Prefer same queries/windows.

| Metric | Baseline (this file) | Next retro | Δ |
|---|---|---|---|
| Elapsed runtime | ~10.1 h | | |
| Same process? (`process_started_at`) | `2026-07-16T18:38:29Z` | | |
| tick_count | 2951 | | |
| last_tick_status / last_error | ok / empty | | |
| CB tripped? | no | | |
| CLOB balance USDC | ~121.75 | | |
| Open orders | 0 | | |
| Open positions (count + ids) | 5 (see §3.4) | | |
| Session fills (count) | 4 | | |
| Session executed buy_yes | 2 | | |
| Session rejected buy_yes | 113 | | |
| Session decisions total | ~2205 | | |
| capital_maintenance share | ~1062 (~48%) | | |
| dead_book_404 count | ~224 | | |
| exposure_cap_reject | 113 | | |
| recon adapter-error (window) | 10 | | |
| Closed-market realized (multi-hour) | ≈ −5.90 USDC / 8 | | |
| Seoul 26°C position | 6.25 @ mark ~0.97 | | |
| Dallas/Atlanta 0.001 lots | still open / mark 0.5 | | |
| Miami/Chengdu dust | mark 0, book 404 | | |
| P0 signal bug still open? | yes | | |
| P1 dead-book noise | yes | | |
| Strategy EV score (1–10) | 3 | | |
| Stability score (1–10) | 9 | | |

### Suggested SQL anchors (next time)

```sql
-- Autopilot state
SELECT * FROM autopilot_state;

-- Decisions since process start (use process_started_at)
SELECT status, action, count(*) n
FROM autopilot_decisions
WHERE created_at >= '2026-07-16 18:38:29'
GROUP BY 1,2 ORDER BY n DESC;

-- Fills since process start
SELECT filled_at, market_id, side, price, size, fee
FROM fills
WHERE filled_at >= '2026-07-16T18:38:29'
ORDER BY filled_at;

-- Positions
SELECT p.market_id, m.title, p.outcome, p.size, p.notional, m.close_time
FROM positions p LEFT JOIN markets m ON m.id = p.market_id
WHERE abs(p.size) > 1e-6;

-- Latest balance
SELECT id, status, created_at,
       json_extract(details, '$.balances.balance') AS balance_micro
FROM reconciliations ORDER BY id DESC LIMIT 1;
```

---

## 7. Operator intent recorded here

- **Do not stop** the running full-auto process for data collection.
- Let it run longer to accumulate fills, settlements, and reject/404 distributions.
- Next retro should:
  1. Diff every row in §6.
  2. Resolve what happened to Dallas / Atlanta / Miami / Chengdu / Seoul.
  3. Re-score P0–P4 (fixed / worse / unchanged).
  4. Only then recommend code changes or capital changes.

---

## 8. Pointers

- Full narrative retro was delivered in chat on 2026-07-17 (this file is the durable numeric baseline).
- Process at save time still running: elapsed ~10h25m (`2026-07-17 13:03 CST`).

---

## 9. Phase 3 runtime smoke blocker — 2026-07-18

**Window:** 2026-07-18 02:20:10–02:33:07 CST
**Command:** `uv run polymarket-weather autopilot start --full-auto`
**Stop:** operator-requested graceful `SIGINT`; both `uv` and Python child exited.
**Open orders at stop:** 0.

### P0 — Official SDK subscription result is not awaited

The Phase 3 WebSocket reader never consumed a market event. The repeated runtime
error was:

```text
stream reader stopped: 'async for' requires an object with __aiter__ method, got coroutine
```

Observed health progression:

| Metric | Initial | ~11 minutes |
|---|---:|---:|
| `exchange_stream_status` | degraded | degraded |
| `market_events` | 0 | 0 |
| `reader_errors` | 2 | 7 |
| `rest_reads` | 0 | 132 |
| `rest_skips` | 0 | 0 |
| `rest_fallback_active` | true | true |
| subscribed tokens | 95 | 94 |

The official SDK's `client.subscribe(...)` returned an awaitable in the installed
runtime, but `_apply_subscription()` stored that coroutine as the subscription
handle. `_read_handle()` then attempted `async for` directly over it. Phase 3 was
therefore fully degraded to REST polling.

Before changing code, inspect the installed official SDK signature and support
both possible compatible forms without creating a second stream path:

1. Call `client.subscribe(spec)` once.
2. If the result is awaitable, await it to obtain the async-iterator handle.
3. Otherwise use the returned async iterator directly.
4. Preserve resubscribe cancellation, old-handle close, token backfill, and REST
   fallback behavior.

### REST fallback behavior during the failure

- No HTTP 429 or CLOB 5xx was observed in this short window.
- REST polling reached at least 132 token-book reads in about 11 minutes.
- One Aviation Weather METAR `ReadTimeout` was retried successfully.
- The fallback kept pricing and reconciliation alive, but this request rate is
  not the intended overnight steady state.

### Live order evidence during the smoke

Autopilot submitted intent `415` for market `2954218`:

```text
Miami July 18, 86–87°F YES
limit price: 0.02
requested size: 100
maximum notional: 2.00 USDC
```

- Filled: 24.65 shares at 0.02; recorded fee 0.02416 USDC.
- Remaining 75.35 shares were automatically cancelled by stale-order handling.
- Final intent state: `partially_filled_closed`.
- Reconciliation remained `ok`; circuit breaker remained clear.
- The 24.65-share position remains open for the normal exit/settlement path.

### Required verification before the next overnight run

1. Unit test an async `subscribe()` returning a handle and retain the existing
   synchronous fake compatibility test.
2. Run full pytest and Ruff gates.
3. Run a read-only real WebSocket smoke before enabling full-auto.
4. Require `exchange_stream_status=live`, `market_events>0`, and increasing
   `rest_skips`.
5. Confirm `rest_reads` grows only for startup backfill and periodic verification,
   not every cached-reprice batch.
6. Confirm no duplicate subscription, duplicate order, or leaked coroutine warning.

### Resolution — 2026-07-18

The adapter now calls `subscribe()` once and awaits its result when it is
awaitable, while retaining compatibility with synchronous test doubles. A
generation check after the await closes and discards late stale handles so an
older subscription cannot replace a newer token set.

Verification completed without starting Autopilot or executing any trade:

- Installed SDK confirmed: `polymarket-client 0.1.0b16`; both public and secure
  `subscribe()` methods are coroutines.
- Targeted stream/pulse/health tests: 27 passed.
- Full suite: 885 passed, 1 skipped.
- Ruff and `git diff --check`: passed.
- Direct official public SDK smoke received a `market/book` event.
- Real `PolymarketStreamBridge` smoke received a live quote with
  `exchange_stream_status=live`, `market_events=1`, and `reader_errors=0`.
- Bridge shutdown completed with final status `disabled`.

The next runtime step remains a short controlled Autopilot observation to verify
startup REST backfill transitions to increasing `rest_skips`; it must not be
described as an overnight acceptance run until that evidence is captured.
