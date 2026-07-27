# Telegram notifications

Push material `/app` trading events and a four-hour portfolio digest to a
Telegram chat. Legacy `operator daemon` notifications are separate and default
**OFF**.

## Setup (2 minutes)

1. In Telegram, open **@BotFather** → `/newbot` → copy the **bot token**.
2. Start a chat with your bot (press Start / send any message).
3. Get your **chat id**:
   - Open: `https://api.telegram.org/bot<TOKEN>/getUpdates`
   - Look for `"chat":{"id": 123456789`
   - Or use a helper bot that prints your user id.
4. Add to `.env`:

```env
TELEGRAM_NOTIFY_ENABLED=true
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
# info | trade | risk
TELEGRAM_NOTIFY_MIN_LEVEL=trade
```

## Use with `/app`

When the environment values above are configured, `/app` automatically sends:

- accepted BUY/SELL submissions and newly reconciled fills immediately;
- auto-exit actions and material execution/reconciliation failures immediately;
- one portfolio digest every four hours, after a fresh successful reconciliation.

The digest lists up to ten verified open temperature positions, their build
cost, reconciled current value, estimated campaign PnL, return, and the time to
the target city's local-day end. It does not claim that local-day end is the
exact Polymarket settlement time. Positions whose fills cannot be linked to the
local campaign ledger are counted as unverified and excluded from PnL totals.
No digest is sent when there are no positions.

Routine ticks, discovery, skips, watch/reject decisions, and dry-runs are not
sent at the recommended `trade` level. The last digest timestamp is persisted,
so restarting the app does not reset the four-hour interval.

## Use with legacy daemon

```bash
# The explicit flag is required even when Telegram is configured in .env.
uv run polymarket-weather operator daemon \
  --profile dry-run-demo \
  --once \
  --notify-telegram
```

It can be combined with the legacy notification dashboard:

```bash
uv run polymarket-weather operator daemon --once --notify-dashboard --notify-telegram
```

## Levels

| `TELEGRAM_NOTIFY_MIN_LEVEL` | Sends |
|-----------------------------|--------|
| `info` | discovery, proposals, tick summaries, risk, trades |
| `trade` | BUY/SELL submissions, fills, auto-exit, four-hour portfolio digest |
| `risk` | risk anomalies / warn status only |

## What you will see

- Immediate BUY/SELL submissions and reconciled fills
- Immediate risk guard and reconciliation anomalies
- Auto-exit SELL submissions
- Four-hour verified portfolio and PnL summary

Notifications never block trading: send failures are logged and ignored.

## Disable

```env
TELEGRAM_NOTIFY_ENABLED=false
```

Or omit token/chat id.
