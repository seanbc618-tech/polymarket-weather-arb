# Auto-Exit Enablement Checklist

**Status:** High-risk, opt-in only. Default is OFF.
**Code (verified offline):** `6afdbee` + `fa46d89`
**Prerequisite:** At least one successful **manual** micro-live BUY→SELL roundtrip with
`position=0`, `open_orders=0`, and roundtrip stage `completed` (already done on
market `2854646`).

This checklist is for operators. Completing it does **not** auto-enable anything.
Do not treat UI status as an enable control.

---

## 0. Do not enable if any of these is true

- [ ] You have not completed a manual BUY→SELL roundtrip on this machine/wallet
- [ ] `TRADING_DISABLED=true` is required for safety today (leave auto-exit off)
- [ ] Circuit breaker is tripped
- [ ] Reconciliation is missing or stale and you cannot refresh it
- [ ] You cannot watch the process for the entire session
- [ ] Position notional is larger than your comfort cap (default auto-exit cap is **1 USDC**)
- [ ] You want browser/one-click enable (not supported; do not build it ad-hoc)

**If any box above is a concern: stop. Keep auto-exit off.**

---

## 1. Environment & process health

```bash
uv run polymarket-weather doctor --live
uv run polymarket-weather live-readiness
uv run polymarket-weather reconcile
uv run polymarket-weather operator live-monitor --profile micro-live
uv run polymarket-weather operator exit-guardian
uv run polymarket-weather operator circuit-breaker status 2>/dev/null || true
```

Confirm:

- [ ] Live credentials configured (`POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`)
- [ ] Compliance path acceptable for your location (or you understand why check is set as it is)
- [ ] `TRADING_DISABLED=false` only for the intentional window
- [ ] Latest reconciliation status `ok` and **fresh** (≤ ~5 minutes)
- [ ] Circuit breaker **not** tripped
- [ ] Exit Guardian output reviewed (know which markets would be `position_at_risk`)
- [ ] Local DB path is the intended production/micro DB (not a scratch test file)

---

## 2. Configuration (double-gated)

### 2.1 Required env (defaults are safe)

| Variable | Safe default | Enable value | Meaning |
|----------|--------------|--------------|---------|
| `AUTO_EXIT_ENABLED` | `false` | `true` | Master software switch |
| `MAX_AUTO_EXITS_PER_TICK` | `1` | keep `1` for first runs | Hard per-tick ceiling |
| `AUTO_EXIT_MAX_POSITION_USDC` | `1` | keep small (≤1) first | Skip positions above this notional |
| `AUTO_EXIT_MAX_SLIPPAGE` | `0.02` | keep conservative | Max sell price below best bid |
| `TRADING_DISABLED` | — | `false` only while testing | Global kill switch |

Also keep micro-live risk caps tight (`MAX_ORDER_USDC` / daily / market) and
`LIVE_MARKET_IDS` restricted if you use whitelist elsewhere.

### 2.2 Required CLI (second gate)

Daemon must pass **`--allow-auto-exit`**.
Env alone is **not** enough. Flag alone is **not** enough.

Profile **must** be `micro-live`.

---

## 3. Position & market readiness

For each position that might auto-exit:

- [ ] Outcome is clear (YES/NO) and maps to a token id
- [ ] Reconciled size > 0 and matches exchange
- [ ] Planned notional `best_bid * size` ≤ `AUTO_EXIT_MAX_POSITION_USDC`
- [ ] No open SELL / no active `sell_yes`/`sell_no` intent for that market
- [ ] You accept selling at ~best bid (limit, not market)
- [ ] You understand fees + spread can make small notional round-trips lose money
      (as seen on the ~0.10 USDC micro roundtrip)

Optional but recommended:

```bash
uv run polymarket-weather operator close-preview --market <id> --outcome YES
uv run polymarket-weather operator roundtrip-status --market <id>
```

---

## 4. What auto-exit will / will not do

| Will | Will not |
|------|----------|
| Only act on ExitGuardian `position_at_risk` | Act on `hold_position` |
| Place **limit SELL** reducing existing size | Open new positions / naked SELL |
| Re-check quote before submit | Chase fills or auto reprice loops |
| Write automation audit + order intent/attempts | Use browser enable buttons |
| Cap exits per tick | Ignore circuit breaker / TRADING_DISABLED |

---

## 5. First enable: single-tick dry rehearsal of gates (no real need to leave on)

### 5.1 Prove default remains off (optional sanity)

With `AUTO_EXIT_ENABLED=false` (or without `--allow-auto-exit`):

```bash
uv run polymarket-weather operator daemon \
  --profile micro-live \
  --once \
  --no-discover \
  --no-propose \
  --no-auto-dry-run
```

Expect: no SELL; notes should show auto-exit not armed / blocked.

### 5.2 First armed run (only after sections 1–3)

1. Set env for a **short window only**:
   ```bash
   # example — put in .env or export for this shell
   export AUTO_EXIT_ENABLED=true
   export MAX_AUTO_EXITS_PER_TICK=1
   export AUTO_EXIT_MAX_POSITION_USDC=1
   export AUTO_EXIT_MAX_SLIPPAGE=0.02
   export TRADING_DISABLED=false
   ```
2. Reconcile again immediately before arming:
   ```bash
   uv run polymarket-weather reconcile
   ```
3. Run **one** tick only:
   ```bash
   uv run polymarket-weather operator daemon \
     --profile micro-live \
     --allow-auto-exit \
     --once \
     --include-reconciliation \
     --no-discover \
     --no-propose \
     --no-auto-dry-run
   ```
4. Immediately review:
   ```bash
   uv run polymarket-weather reconcile
   uv run polymarket-weather operator positions
   uv run polymarket-weather operator open-orders
   uv run polymarket-weather operator fills
   # inspect latest order intents / automation actions in dashboard or SQLite
   ```
5. **Turn off again:**
   ```bash
   export AUTO_EXIT_ENABLED=false
   # or set TRADING_DISABLED=true if anything looks wrong
   ```

---

## 6. Post-tick verification (must pass)

- [ ] At most one auto-exit SELL attempt this tick
- [ ] Intent side is `sell_yes` / `sell_no` (never buy path)
- [ ] Order attempt trail exists (`submitted` / `checked` / `reconciled` or explicit failed/unverified)
- [ ] Automation action kind `auto_exit` + audit events present
- [ ] If status is `submitted_unverified` or `reconcile_failed`: **do not re-fire auto-exit**; verify on exchange manually
- [ ] Position reduced only as intended; no new long created
- [ ] Circuit breaker still clear (unless a separate audit tripped it)

---

## 7. Abort / kill switches (memorize)

In order of speed:

1. **Stop the daemon** (Ctrl-C / kill process)
2. Set `AUTO_EXIT_ENABLED=false` and restart nothing with `--allow-auto-exit`
3. Set `TRADING_DISABLED=true` (blocks live SELL exits)
4. Cancel open SELL on exchange if needed:
   ```bash
   uv run polymarket-weather operator cancel-order <exchange_order_id>
   ```
5. If audit/exchange diverge: `reconcile`, then manual `close-preview` / `close-live` only with exact confirm

---

## 8. Sign-off (operator)

Only enable a longer session after a successful single-tick armed run:

| Item | Initials / time |
|------|-----------------|
| Manual roundtrip completed on this wallet | |
| Doctor + live-readiness + fresh reconcile OK | |
| Env caps reviewed (≤1 USDC first) | |
| Single-tick armed run reviewed | |
| Kill-switch procedure understood | |
| `AUTO_EXIT_ENABLED` returned to `false` after test (or intentionally left on) | |

---

## 9. Explicit non-goals

- Not a substitute for manual `close-live` discipline on large size
- Not Full Live / autopilot production mode
- Not available from `/app` or beginner one-click UI
- Not automatic re-entry after exit

When in doubt: leave **off**, reconcile, and exit manually with confirm phrase.
