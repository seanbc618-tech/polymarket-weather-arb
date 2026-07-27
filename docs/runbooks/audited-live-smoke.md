# Audited Live Smoke Test

> [!WARNING]
> This runbook is a controlled operator workflow for testing production endpoints on Polymarket. It requires live credentials and explicit human authorization. **It does not mean automation is ready**. Treat smoke tests as manual rehearsal before enabling `/app` Full live.

## 1. What This Is
This is an audited exercise of the real CLOB limit order submission and reconciliation pipeline. It validates:
- CLOB token ID translation for a live market
- Small limit order placement (e.g. $0.50 USDC)
- Order ID capture
- Immediate cancellation (optional but recommended)
- Exchange state reconciliation in local SQLite

## 2. What This Is NOT
- It is **not** a fill execution or profitability test.
- It will **not** verify closing positions.
- It is **not** guaranteed to be visible on the Polymarket frontend (取消且未成交的 CLOB 订单可能不显示在前端历史中).

## 3. Prerequisites
Before attempting the smoke test, verify the following are strictly passing:
- You have explicitly authorized the test.
- The system is in `TRADING_DISABLED=false` in `.env`.
- `live-readiness` returns all clear (e.g. valid credentials, passing geoblock).
- The latest `reconcile` run was successful and fresh.
- Circuit-breaker status is `ok` (not tripped).

Check readiness:
```bash
uv run polymarket-weather reconcile
uv run polymarket-weather operator circuit-breaker status
uv run polymarket-weather live-readiness
```

## 4. Execution

Submit a real, small limit order using a placeholder market ID and minimal size. 
> [!IMPORTANT]
> The absolute maximum loss (price * size) must be <= 1 USDC.

```bash
# Example limit order: buy 'yes' at 0.50c for 1 share (total exposure 0.50 USDC)
uv run polymarket-weather operator smoke-live \
  --market <market_id> \
  --side buy_yes \
  --price 0.5 \
  --size 1 \
  --cancel-immediately
```

*(Note: Use `--cancel-immediately` to ensure the order is revoked right after it registers, minimizing fill risk).*

> [!CAUTION]
> 订单可能在取消前成交；若成交，立即停止、reconcile，不要自行假设可平仓 (The order might fill before it is cancelled; if filled, stop immediately, reconcile, do not assume you can close the position on your own).

## 5. Audit
Do not rely on the Polymarket website to confirm the result. Verify the order lifecycle entirely through the local SQLite logs.

```bash
# Check that the intent and attempt were logged
uv run polymarket-weather operator queue-summary
uv run polymarket-weather orders

# Reconcile again to confirm the order appeared (and disappeared if cancelled)
uv run polymarket-weather reconcile
uv run polymarket-weather operator open-orders
```
