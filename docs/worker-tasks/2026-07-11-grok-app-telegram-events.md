# Grok Task: `/app` Telegram Event Notifications

## Read First

Read `AGENTS.md`, `CLAUDE.md`, and `docs/agent-worker-standards.md` before
editing. The worker standard is authoritative.

## Objective

When the `/app` Autopilot is running, deliver Telegram notifications for material
trading events without sending routine tick, discovery, candidate, skip, or
heartbeat messages.

The desired operator experience is simple: Telegram should wake the user only for
a real order action, a confirmed exchange fill, or a material execution failure.

## Existing Components To Reuse

- `services/telegram_notifier.py`: `TelegramNotifier`, `FanoutNotifier`, message
  formatting, level filtering, proxy behavior, and send-failure isolation.
- `services/autopilot_service.py`: primary `/app` autonomous loop and its
  `AutopilotTickResult`.
- `services/reconciliation_service.py`: exchange reads and fill persistence.
- `storage/repositories.py`: `fills`, `order_intents`, `order_attempts`, and
  reconciliation persistence.
- `dashboard.py:serve_dashboard()`: process-level `/app` background tick owner.
- `cli_commands/autopilot.py`: starts `/app` through `serve_dashboard()`.

Do not create a second Telegram client, notification transport, queue, worker,
database table, dashboard route, or polling loop.

## Required Behavior

1. `/app` must use the existing Telegram configuration:
   `TELEGRAM_NOTIFY_ENABLED`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and
   `TELEGRAM_NOTIFY_MIN_LEVEL`.
2. Construct one notifier for the dashboard process and inject/reuse it for each
   background Autopilot tick. Do not construct a new notifier or HTTP client per
   tick.
3. Emit notifications only for these events:
   - a live BUY accepted by the exchange (`submitted`, not merely a rejected
     intent);
   - an automatic SELL accepted by the exchange;
   - newly persisted exchange fills discovered by reconciliation;
   - a submitted order whose verification/reconciliation becomes unverified or
     fails, if and only if that is a material execution state.
4. Do not notify for ordinary ticks, discovery counts, no-candidate results,
   dry-runs, low edge, skips, LLM opinions, or normal live-gate rejections.
5. A fill must be notified once per `exchange_fill_id`, including after a process
   restart. Do not use an in-memory-only de-duplication set. Extend the existing
   fill persistence contract to return the inserted fill identifiers, or add a
   small repository read method that can reliably distinguish newly inserted rows.
   Do not add a notification-history table for this task.
6. BUY/SELL messages must clearly say `submitted` or `filled`; never call a mere
   submitted limit order "profit", "completed", or "matched".
7. Include only useful non-secret fields: event type, market id/title when
   available, side/outcome, price, size, order id or fill id, and status. Never
   include private keys, API keys, complete raw exchange payloads, or wallet
   signatures.
8. Telegram failure must log/record locally and never stop Autopilot, order
   submission, reconciliation, or SQLite commit.

## Suggested Minimal Design

- Add an optional notifier dependency to `AutopilotService`, following the
  existing `OperatorDaemon` injection style.
- Have `serve_dashboard()` create the reusable notifier once and pass it to each
  newly-created Autopilot service in its background loop and the manual `/app/tick`
  route factory as appropriate.
- Extend `ReconciliationService.reconcile()` and the existing repository fill
  method only enough to expose newly inserted fill rows/IDs to its caller.
- Reuse `TelegramNotifier` payload classification and `flush()` once at the end
  of a tick. The default Telegram minimum level should continue to suppress
  informational traffic.

If a tiny callback is enough, prefer it over a new service class.

## Explicit Non-Goals

- No real BUY, SELL, cancel, or reconciliation against the user's account.
- No Telegram commands, conversational bot, inline keyboard, alert scheduler,
  retry queue, or notification database.
- No notifications for paper mode or dry-run activity.
- No changes to trading limits, strategy selection, LLM behavior, or exit policy.
- No duplicate implementation under `OperatorDaemon`; improve shared notifier
  behavior only when it directly benefits both paths.

## Tests Required

Use mocked Telegram sender and mocked Polymarket client. Add focused tests proving:

1. a routine `/app` tick sends nothing;
2. a submitted live BUY sends exactly one `submitted` event;
3. an auto-exit SELL sends exactly one `submitted` event;
4. reconciliation sends one notification for a newly inserted fill and none when
   the same fill appears on the next tick or after a fresh service instance;
5. Telegram sender failure does not change an otherwise successful Autopilot tick;
6. missing Telegram configuration results in no send and no tick failure;
7. event payloads/messages contain no secret configuration values.

Run targeted tests, `uv run ruff check src/ tests/`, `uv run pytest -q`, and
`git diff --check`.

## Completion Report

Report existing components reused, every new file/class/table/setting (expected:
none), exact test results, commit hash, remaining working-tree changes, and the
explicit statement that no real trading mutation ran.

