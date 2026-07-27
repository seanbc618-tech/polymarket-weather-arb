# Micro-Live Roundtrip Runbook

This runbook describes the procedure for a fully audited, complete BUY -> SELL
roundtrip on a live market using the `micro-live` profile, with a **manual**
operator target of about **1 USDC** notional for the rehearsal.

## Purpose

To verify end-to-end execution, portfolio reconciliation, and position settlement
on the live Polymarket exchange using the tight `micro-live` profile caps, while
keeping this rehearsal's chosen notional small by operator discipline.

## Risk caps (do not overstate)

| Layer | Cap | Notes |
|-------|-----|--------|
| `micro-live` profile code | `max_order_usdc=5`, `max_daily_usdc=10`, `max_market_usdc=5` | Enforced by profile/settings + hardcoded absolute ceilings (25/100/50) |
| This runbook's rehearsal target | **≈1 USDC notional** | **Manual operator constraint only** — not an independent hard gate in code |
| Global hardcoded ceilings | 25 / order, 100 / day, 50 / market USDC | Profiles cannot loosen these |

**Important:** The system does **not** currently enforce a dedicated “roundtrip max
loss ≤ 1 USDC” check. Choosing `price * size ≤ 1` is an **operator procedure** for
this rehearsal. If you size up to the profile, the live path may still accept
orders up to the **5 USDC** micro-live order cap (subject to other gates).

## Procedure

The roundtrip requires **two distinct, independent manual authorizations** via the
operator CLI. At no point should a single authorization trigger both legs of the
trade.

### 1. Pre-Flight Checks

- Reconcile local SQLite with exchange state (root command, not under `operator`):

  ```bash
  uv run polymarket-weather reconcile
  ```

- Check roundtrip status (if available in your build):

  ```bash
  uv run polymarket-weather operator roundtrip-status --market <market_id>
  ```

  The stage MUST be `ready_to_buy` and reconciliation MUST be fresh.

### 2. BUY Leg (Authorization 1)

- Operator runs the buy path (e.g. live-launchpad, automation queue approval, or
  other live trade entry) explicitly defining:
  - Market ID
  - Side (YES or NO)
  - Limit Price
  - Size (Shares)
  - **Rehearsal notional target**: Prefer `price * size ≤ 1` USDC for this
    roundtrip. This is a **manual** discipline, not a separate hardcoded
    roundtrip limit. Profile enforcement allows up to **5 USDC** per order under
    `micro-live`.
- **Important constraint**: If the BUY order does not fill or only partially
  fills, the operator MUST cancel the open order before proceeding. Do not start
  the SELL leg while BUY orders are still open.

### 3. Intermediate Verification

- Reconcile again:

  ```bash
  uv run polymarket-weather reconcile
  ```

- Run:

  ```bash
  uv run polymarket-weather operator roundtrip-status --market <market_id>
  ```

- Verify that the stage is `position_confirmed`.
- If the order partially filled, you may only sell the **actual filled size**
  (current reconciled position).

### 4. SELL Leg (Authorization 2)

- Inspect exit quote first:

  ```bash
  uv run polymarket-weather operator close-preview \
    --market <market_id> \
    --outcome <YES|NO>
  ```

- Manually authorize the SELL with `close-live`, explicitly defining:
  - Market ID
  - Outcome (YES or NO)
  - Limit Price
  - Size (must not exceed the position confirmed in Step 3)
  - Max Slippage (acceptable price vs best bid)
  - Exact confirm phrase: `SELL <market_id> <YES|NO> <size>`
- **Important constraint**: If the SELL does not fill immediately, do not assume
  position is zero. Wait for fill confirmation or cancel the order. If status is
  `submitted_unverified` or `reconcile_failed`, **do not re-submit** — verify on
  the exchange and reconcile.

### 5. Post-Flight Verification

- Reconcile one final time:

  ```bash
  uv run polymarket-weather reconcile
  ```

- Run:

  ```bash
  uv run polymarket-weather operator roundtrip-status --market <market_id>
  ```

- Verify that the stage is `completed`.
- **Definition of completed**: A roundtrip is ONLY considered `completed` when
  5 conditions are met simultaneously: `reconciliation_fresh` is True, `position = 0`,
  `open_orders = 0`, and the most recent BUY and SELL intents both show as `filled`.

## Rehearsal (offline only)

You can run the offline rehearsal tests to verify state-machine logic without
hitting the live API:

```bash
uv run pytest tests/test_micro_live_rehearsal.py
```

This uses a temporary SQLite database and a mock client. It does **not** prove a
live BUY fill + SELL fill roundtrip.
