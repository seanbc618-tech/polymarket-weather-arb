# Weather Settlement Core V5 — production replay

**Date:** 2026-07-24
**Source:** production SQLite opened with `mode=ro` while Autopilot was stopped
**Source HEAD:** `05511a6`
**Replay code:** `scripts/replay_weather_settlement_core_v5.py`
**Verdict:** PASS for a trading-disabled shadow deployment; **NO-GO** for
micro-live or full-live

## Methodology

- BUY cost uses exact `order_intent -> order_attempt -> reconciled fill`
  linkage and includes the recorded account fee.
- Entry edge, executable reference price, and horizon come from the latest V8
  analysis available before the intent timestamp, with a 30-minute maximum
  age. Final/latest analysis is not substituted.
- Event identity is case-folded `city + target_date`, not market ID.
- The old path for the V5-selected subset allocates later account SELL fills
  FIFO.
- The V5 model exit requires two distinct forecast revisions. Each revision is
  paired with the first token-specific bid no more than ten minutes after the
  analysis; taker fee is deducted.
- The counterfactual reapplies `4/order`, `10/event`, and `100/day` caps and
  uses an explicit synthetic starting bankroll of `100`. No old early-SELL
  proceeds are assumed available before their recorded time.
- Maker-first is not modeled because there is no post-only V5b fill/markout
  history.

Command:

```bash
ssh operator@203.0.113.10 \
  'cd /opt/polymarket-weather-arb && sudo -n -u polymarket-weather \
  env UV_CACHE_DIR=/tmp/pwa-replay-cache-service uv run python - \
  --database /opt/polymarket-weather-arb/data/polymarket_weather.db --pretty' \
  < scripts/replay_weather_settlement_core_v5.py
```

## A/B/C result

| Path | Resolved set | BUY cash | Revenue | PnL |
| --- | ---: | ---: | ---: | ---: |
| A — actual V4 cash path | 13 markets | $35.804863 | $35.394800 SELL net | **−$0.410063** |
| B — all V4 hold to settlement | 13 markets | $35.804863 | $47.740000 | **+$11.935137** |
| C-old — old path, V5-selected shares, FIFO | 6 events | $12.841480 | $12.982207 | **+$0.140727** |
| C — V5 entry + settlement core | 6 events | $12.841480 | $26.400000 | **+$13.558520** |

Path C selected six D1 events under the exact timestamped entry rules:
Buenos Aires, Manila, Wuhan, Ankara, Cape Town, and Dallas. Two were winning
buckets (Manila and Dallas). The Dallas sibling entered later under V4 was
correctly excluded by the one-entry-per-event rule.

No historical C event met either permitted early-exit path:

- model value exits: `0`;
- official-impossibility exits with a bound quote: `0`.

Therefore C equals hold-to-settlement on this six-event subset. This supports
retaining the right tail, but it does **not** demonstrate that the protective
value-exit branch will rescue future dead buckets. That branch is covered by
deterministic regression tests and still needs new shadow/live evidence.

## Methodology corrections

1. The previously quoted `87.5%` is an early-cash-path win rate, not bucket
   accuracy.
2. The 13-market totals independently reproduce Grok's `$35.80` BUY,
   `$47.74` settlement payout, and approximately `+$11.94` hold
   counterfactual.
3. Manila's first linked V4 entry is D1 at the entry-time model signal
   (`2026-07-22T07:25:57Z` analysis for a `2026-07-23` Manila event). A
   latest-analysis horizon join can mislabel it D0. This explains part of the
   earlier D0/D1 disagreement.
4. Partial campaign cash unrecovered is not treated as fully realized loss.

## Limits and rollout decision

- Historical best bid is not a full-depth VWAP.
- Historical free USDC is not persisted; the bankroll assumption is explicit.
- Only linked fills are counted.
- Six counterfactual events are far below the 20 newly resolved real V5 events
  required for a live decision.
- This replay is directional evidence, not proof of future profitability.

The implementation may be deployed with `TRADING_DISABLED=true` to collect
shadow analyses and reconciliation evidence. It must not restore real
execution until an explicit review authorizes micro-live; full-live remains
locked until at least 20 newly resolved V5 events show positive fee-adjusted
event-level EV and acceptable drawdown.
