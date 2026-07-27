#!/usr/bin/env python3
"""Read-only chronological A/B/C replay for weather settlement-core V5.

This script never opens SQLite in write mode.  It uses exact
intent -> exchange order -> reconciled fill linkage for entry cash, binds each
entry to the V8 analysis available at that timestamp, and keeps actual cash,
hold-to-settlement, and V5 counterfactual ledgers separate.

Path A is the observed V4 cash path.  Path B holds every resolved V4 entry to
settlement.  Path C applies the V5 entry gates, one accepted entry per
city/date event, then permits only an official-impossibility exit or two
distinct forecast revisions whose fee-adjusted bid dominates the held
probability upper bound.

Maker-first is deliberately not modeled here because production has no
post-only fill/markout history for that proposed policy.
"""

from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from polymarket_weather_arb.domain.fees import (
    WEATHER_TAKER_FEE_RATE,
    compute_taker_fee,
    expected_taker_fee_per_share,
    extract_market_fee_schedule,
)
from polymarket_weather_arb.domain.global_temperature_bucket import (
    observation_source_tolerance,
    parse_global_temperature_bucket_rule,
    settlement_bucket_bounds,
)
from polymarket_weather_arb.domain.position_inventory import account_fill_view

V4_ENTRY_POLICY = "weather-entry-v4"
V8_MODEL = "global-temp-bucket-multimodel-v8"
V5_MIN_EDGE = Decimal("0.10")
V5_MIN_PRICE = Decimal("0.05")
V5_VALUE_MARGIN = Decimal("0.02")
V5_ORDER_CAP = Decimal("4")
V5_EVENT_CAP = Decimal("10")
V5_DAILY_CAP = Decimal("100")
ANALYSIS_FRESHNESS = timedelta(minutes=30)
QUOTE_LOOKAHEAD = timedelta(minutes=10)
GOOD_OBSERVATION_QUALITY = {
    "",
    "V",
    "OK",
    "GOOD",
    "OFFICIAL",
    "VERIFIED",
    "Z",
    "AWC",
}


@dataclass(frozen=True)
class LinkedEntry:
    intent_id: int
    market_id: str
    held_outcome: str
    event_key: str
    city: str
    target_date: str
    token_id: str
    entered_at: datetime
    edge: Decimal
    reference_price: Decimal
    horizon: str
    size: Decimal
    principal: Decimal
    fee: Decimal
    cash: Decimal
    resolved_outcome: str
    settled_at: datetime | None


@dataclass(frozen=True)
class ExitTrigger:
    kind: str
    at: datetime
    bid: Decimal
    net_per_share: Decimal
    evidence: str


@dataclass(frozen=True)
class EventReplay:
    event_key: str
    market_id: str
    held_outcome: str
    resolved_outcome: str
    horizon: str
    edge: str
    reference_price: str
    size: str
    buy_cash: str
    old_path_pnl_fifo: str
    hold_pnl: str
    v5_exit_kind: str
    v5_exit_at: str | None
    v5_revenue: str
    v5_pnl: str
    evidence: str


def _decimal(value: object) -> Decimal:
    return Decimal(str(value or 0))


def _dt(value: object) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_reasons(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return [value]
    return [str(item) for item in parsed] if isinstance(parsed, list) else [value]


def _order_ids(payload: object) -> set[str]:
    data = _json_object(payload)
    result: set[str] = set()
    for key in ("order_id", "orderID", "orderId", "id"):
        value = data.get(key)
        if value:
            result.add(str(value))
    return result


def _fill_fee(row: sqlite3.Row, view: dict[str, Any]) -> Decimal:
    raw = _json_object(row["raw_payload"])
    resolution = raw.get("_fee_resolution")
    if isinstance(resolution, dict) and resolution.get("fee") not in {None, ""}:
        return _decimal(resolution["fee"])
    if view.get("fee") not in {None, ""}:
        return _decimal(view["fee"])
    return _decimal(row["fee"])


def _normalized_outcome(value: object) -> str | None:
    text = str(value or "").strip().upper()
    if text in {"YES", "Y", "1", "TRUE"}:
        return "YES"
    if text in {"NO", "N", "0", "FALSE"}:
        return "NO"
    return None


def _fill_matches_outcome(
    view: dict[str, Any],
    *,
    held_outcome: str,
    target_token: str,
) -> bool:
    explicit = _normalized_outcome(view.get("outcome"))
    if explicit is not None:
        return explicit == held_outcome
    token = (
        view.get("token_id")
        or view.get("asset_id")
        or view.get("assetId")
        or ""
    )
    return not token or str(token) == target_token


def _resolved_state(
    connection: sqlite3.Connection,
    market_id: str,
) -> tuple[str, datetime | None] | None:
    row = connection.execute(
        """
        SELECT resolved_outcome, settled_at
        FROM model_signals
        WHERE market_id = ?
          AND outcome_status = 'resolved'
          AND resolved_outcome IN ('yes', 'no')
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (market_id,),
    ).fetchone()
    if row is None:
        return None
    return str(row["resolved_outcome"]).upper(), _dt(row["settled_at"])


def _analysis_at_entry(
    connection: sqlite3.Connection,
    market_id: str,
    created_at: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT
            a.*,
            json_extract(ms.raw_payload, '$.horizon') AS horizon,
            json_extract(ms.raw_payload, '$.forecast_revision') AS forecast_revision
        FROM analyses a
        LEFT JOIN model_signals ms ON ms.analysis_id = a.id
        WHERE a.market_id = ?
          AND a.model_version = ?
          AND a.created_at <= ?
        ORDER BY a.created_at DESC, a.id DESC
        LIMIT 1
        """,
        (market_id, V8_MODEL, created_at),
    ).fetchone()


def load_linked_entries(
    connection: sqlite3.Connection,
    *,
    entry_policy_version: str,
) -> list[LinkedEntry]:
    """Load only resolved entries with exact local intent/fill linkage."""
    intents = connection.execute(
        """
        SELECT
            oi.*,
            m.yes_token_id,
            m.no_token_id,
            COALESCE(NULLIF(r.city, ''), m.event_title, m.title) AS city,
            COALESCE(r.target_date, '') AS target_date
        FROM order_intents oi
        JOIN markets m ON m.id = oi.market_id
        LEFT JOIN temperature_bucket_rules r ON r.market_id = oi.market_id
        WHERE oi.dry_run = 0
          AND LOWER(oi.side) LIKE 'buy%'
          AND oi.entry_policy_version = ?
          AND oi.status IN (
              'filled', 'matched', 'partially_filled', 'partially_filled_closed'
          )
        ORDER BY oi.created_at ASC, oi.id ASC
        """,
        (entry_policy_version,),
    ).fetchall()
    entries: list[LinkedEntry] = []
    consumed_fill_ids: set[int] = set()
    for intent in intents:
        resolved = _resolved_state(connection, str(intent["market_id"]))
        if resolved is None:
            continue
        held_outcome = "NO" if "no" in str(intent["side"]).lower() else "YES"
        target_token = str(
            intent["no_token_id"] if held_outcome == "NO" else intent["yes_token_id"]
        )
        order_ids: set[str] = set()
        for attempt in connection.execute(
            """
            SELECT request_payload, response_payload
            FROM order_attempts
            WHERE intent_id = ?
            ORDER BY id ASC
            """,
            (intent["id"],),
        ):
            order_ids.update(_order_ids(attempt["request_payload"]))
            order_ids.update(_order_ids(attempt["response_payload"]))
        if not order_ids:
            continue
        placeholders = ",".join("?" for _ in order_ids)
        fills = connection.execute(
            f"""
            SELECT * FROM fills
            WHERE market_id = ? AND order_id IN ({placeholders})
            ORDER BY filled_at ASC, id ASC
            """,  # noqa: S608 - placeholders are literal question marks only.
            (intent["market_id"], *sorted(order_ids)),
        ).fetchall()
        size = Decimal("0")
        principal = Decimal("0")
        fee = Decimal("0")
        entered_at: datetime | None = None
        for row in fills:
            if int(row["id"]) in consumed_fill_ids:
                continue
            view = account_fill_view(dict(row))
            if not str(view.get("side") or "").upper().startswith("BUY"):
                continue
            if not _fill_matches_outcome(
                view,
                held_outcome=held_outcome,
                target_token=target_token,
            ):
                continue
            fill_price = _decimal(view.get("price"))
            fill_size = _decimal(view.get("size") or view.get("quantity"))
            if fill_price <= 0 or fill_size <= 0:
                continue
            consumed_fill_ids.add(int(row["id"]))
            size += fill_size
            principal += fill_price * fill_size
            fee += _fill_fee(row, view)
            fill_time = _dt(row["filled_at"])
            if fill_time is not None and (entered_at is None or fill_time < entered_at):
                entered_at = fill_time
        if size <= 0 or entered_at is None:
            continue
        analysis = _analysis_at_entry(
            connection,
            str(intent["market_id"]),
            str(intent["created_at"]),
        )
        if analysis is None:
            continue
        analysis_at = _dt(analysis["created_at"])
        intent_at = _dt(intent["created_at"])
        if (
            analysis_at is None
            or intent_at is None
            or intent_at - analysis_at > ANALYSIS_FRESHNESS
        ):
            continue
        city = str(intent["city"] or "").strip().casefold()
        target_date = str(intent["target_date"] or "").strip()
        event_key = f"{city}|{target_date}" if city and target_date else str(intent["market_id"])
        entries.append(
            LinkedEntry(
                intent_id=int(intent["id"]),
                market_id=str(intent["market_id"]),
                held_outcome=held_outcome,
                event_key=event_key,
                city=str(intent["city"] or ""),
                target_date=target_date,
                token_id=target_token,
                entered_at=entered_at,
                edge=_decimal(analysis["edge"]),
                reference_price=_decimal(analysis["reference_price"]),
                horizon=str(analysis["horizon"] or "unknown"),
                size=size,
                principal=principal,
                fee=fee,
                cash=principal + fee,
                resolved_outcome=resolved[0],
                settled_at=resolved[1],
            )
        )
    return entries


def _market_payload(connection: sqlite3.Connection, market_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT raw_payload FROM markets WHERE id = ?",
        (market_id,),
    ).fetchone()
    return _json_object(row["raw_payload"]) if row is not None else {}


def _fee_rate(connection: sqlite3.Connection, market_id: str) -> Decimal:
    schedule = extract_market_fee_schedule(_market_payload(connection, market_id))
    if not schedule.fees_enabled:
        return Decimal("0")
    return schedule.fee_rate or WEATHER_TAKER_FEE_RATE


def _sell_fills(
    connection: sqlite3.Connection,
    entry: LinkedEntry,
) -> list[tuple[datetime, Decimal, Decimal, Decimal]]:
    rows = connection.execute(
        """
        SELECT * FROM fills
        WHERE market_id = ? AND filled_at >= ?
        ORDER BY filled_at ASC, id ASC
        """,
        (entry.market_id, entry.entered_at.isoformat()),
    ).fetchall()
    sells: list[tuple[datetime, Decimal, Decimal, Decimal]] = []
    for row in rows:
        view = account_fill_view(dict(row))
        if not str(view.get("side") or "").upper().startswith("SELL"):
            continue
        if not _fill_matches_outcome(
            view,
            held_outcome=entry.held_outcome,
            target_token=entry.token_id,
        ):
            continue
        at = _dt(row["filled_at"])
        price = _decimal(view.get("price"))
        size = _decimal(view.get("size") or view.get("quantity"))
        if at is None or price <= 0 or size <= 0:
            continue
        sells.append((at, price, size, _fill_fee(row, view)))
    return sells


def _actual_fifo_revenue(
    connection: sqlite3.Connection,
    entry: LinkedEntry,
) -> Decimal:
    """Attribute old-path SELL fills to this entry's shares in FIFO order."""
    remaining = entry.size
    revenue = Decimal("0")
    for _at, price, fill_size, fill_fee in _sell_fills(connection, entry):
        if remaining <= 0:
            break
        allocated = min(remaining, fill_size)
        fee_per_share = fill_fee / fill_size if fill_size > 0 else Decimal("0")
        revenue += allocated * (price - fee_per_share)
        remaining -= allocated
    if remaining > 0 and entry.resolved_outcome == entry.held_outcome:
        revenue += remaining
    return revenue


def _actual_market_sell_net(
    connection: sqlite3.Connection,
    entries: Iterable[LinkedEntry],
) -> Decimal:
    group = list(entries)
    if not group:
        return Decimal("0")
    first = min(group, key=lambda item: item.entered_at)
    return sum(
        (price * size - fee for _at, price, size, fee in _sell_fills(connection, first)),
        Decimal("0"),
    )


def select_v5_entries(entries: Iterable[LinkedEntry]) -> list[LinkedEntry]:
    selected: list[LinkedEntry] = []
    accepted_events: set[str] = set()
    daily_cash: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    event_cash: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    bankroll = V5_DAILY_CAP
    releases: list[tuple[datetime, Decimal]] = []

    for entry in sorted(entries, key=lambda item: (item.entered_at, item.intent_id)):
        still_pending: list[tuple[datetime, Decimal]] = []
        for release_at, amount in releases:
            if release_at <= entry.entered_at:
                bankroll += amount
            else:
                still_pending.append((release_at, amount))
        releases = still_pending
        if entry.event_key in accepted_events:
            continue
        if entry.edge < V5_MIN_EDGE or entry.reference_price < V5_MIN_PRICE:
            continue
        if entry.horizon not in {"D1", "D2"}:
            continue
        day = entry.entered_at.date().isoformat()
        if entry.cash > V5_ORDER_CAP:
            continue
        if event_cash[entry.event_key] + entry.cash > V5_EVENT_CAP:
            continue
        if daily_cash[day] + entry.cash > V5_DAILY_CAP:
            continue
        if entry.cash > bankroll:
            continue
        selected.append(entry)
        accepted_events.add(entry.event_key)
        daily_cash[day] += entry.cash
        event_cash[entry.event_key] += entry.cash
        bankroll -= entry.cash
        if entry.settled_at is not None:
            payout = entry.size if entry.resolved_outcome == entry.held_outcome else Decimal("0")
            releases.append((entry.settled_at, payout))
    return selected


def _quotes(
    connection: sqlite3.Connection,
    entry: LinkedEntry,
) -> tuple[list[datetime], list[tuple[Decimal, datetime]]]:
    rows = connection.execute(
        """
        SELECT best_bid, fetched_at
        FROM market_snapshots
        WHERE market_id = ? AND token_id = ? AND best_bid IS NOT NULL
        ORDER BY fetched_at ASC, id ASC
        """,
        (entry.market_id, entry.token_id),
    ).fetchall()
    values: list[tuple[Decimal, datetime]] = []
    for row in rows:
        at = _dt(row["fetched_at"])
        bid = _decimal(row["best_bid"])
        if at is not None and Decimal("0") < bid < Decimal("1"):
            values.append((bid, at))
    return [item[1] for item in values], values


def _quote_after(
    quote_times: list[datetime],
    quotes: list[tuple[Decimal, datetime]],
    at: datetime,
) -> tuple[Decimal, datetime] | None:
    index = bisect.bisect_left(quote_times, at)
    if index >= len(quotes):
        return None
    bid, quote_at = quotes[index]
    if quote_at - at > QUOTE_LOOKAHEAD:
        return None
    return bid, quote_at


def _analysis_is_usable(row: sqlite3.Row) -> bool:
    if "unavailable" in str(row["model_version"] or "").lower():
        return False
    markers = (
        "evidence_status=insufficient_models",
        "requires at least 3 models",
        "requires at least 3 independent source families",
        "forecast/analysis failed:",
    )
    return not any(
        marker in reason.lower()
        for reason in _json_reasons(row["reasons"])
        for marker in markers
    )


def _model_value_exit(
    connection: sqlite3.Connection,
    entry: LinkedEntry,
    *,
    quote_times: list[datetime],
    quotes: list[tuple[Decimal, datetime]],
) -> ExitTrigger | None:
    rows = connection.execute(
        """
        SELECT
            a.*,
            json_extract(ms.raw_payload, '$.forecast_revision') AS forecast_revision
        FROM analyses a
        JOIN model_signals ms ON ms.analysis_id = a.id
        WHERE a.market_id = ?
          AND a.model_version = ?
          AND a.created_at >= ?
        ORDER BY a.created_at ASC, a.id ASC
        """,
        (entry.market_id, V8_MODEL, entry.entered_at.isoformat()),
    ).fetchall()
    fee_rate = _fee_rate(connection, entry.market_id)
    streak: list[str] = []
    for row in rows:
        analysis_at = _dt(row["created_at"])
        revision = str(row["forecast_revision"] or "").strip()
        if analysis_at is None or not revision or not _analysis_is_usable(row):
            streak.clear()
            continue
        quote = _quote_after(quote_times, quotes, analysis_at)
        if quote is None:
            continue
        bid, quote_at = quote
        if quote_at - analysis_at > ANALYSIS_FRESHNESS:
            streak.clear()
            continue
        fair_lower = _decimal(row["fair_lower"])
        fair_upper = _decimal(row["fair_upper"])
        hold_upper = (
            fair_upper
            if entry.held_outcome == "YES"
            else Decimal("1") - fair_lower
        )
        net = bid - expected_taker_fee_per_share(price=bid, fee_rate=fee_rate)
        if net < hold_upper + V5_VALUE_MARGIN:
            streak.clear()
            continue
        if not streak or streak[-1] != revision:
            streak.append(revision)
        if len(streak) >= 2:
            return ExitTrigger(
                kind="model_value_exit",
                at=quote_at,
                bid=bid,
                net_per_share=net,
                evidence=(
                    f"revisions={streak[-2:]}; net={net}; "
                    f"hold_upper={hold_upper}; margin={V5_VALUE_MARGIN}"
                ),
            )
    return None


def _official_impossibility_exit(
    connection: sqlite3.Connection,
    entry: LinkedEntry,
    *,
    quote_times: list[datetime],
    quotes: list[tuple[Decimal, datetime]],
) -> ExitTrigger | None:
    market = connection.execute(
        "SELECT title, description FROM markets WHERE id = ?",
        (entry.market_id,),
    ).fetchone()
    if market is None:
        return None
    rule = parse_global_temperature_bucket_rule(
        str(market["title"] or ""),
        market["description"],
    )
    lower, upper = settlement_bucket_bounds(rule)
    if lower is None and upper is None:
        return None
    rows = connection.execute(
        """
        SELECT *
        FROM weather_observations
        WHERE market_id = ? AND fetched_at >= ?
        ORDER BY fetched_at ASC, id ASC
        """,
        (entry.market_id, entry.entered_at.isoformat()),
    ).fetchall()
    fee_rate = _fee_rate(connection, entry.market_id)
    for row in rows:
        quality = str(row["quality_status"] or "").strip().upper()
        if quality not in GOOD_OBSERVATION_QUALITY:
            continue
        observed = _decimal(row["value"])
        at = _dt(row["fetched_at"])
        if at is None:
            continue
        invalidates_yes = False
        locks_yes = False
        tolerance = observation_source_tolerance(rule.unit or row["unit"])
        if rule.bucket_kind == "upper_tail" and lower is not None:
            locks_yes = observed - tolerance >= lower
        elif upper is not None:
            invalidates_yes = observed >= upper
        invalidates_held = (
            invalidates_yes if entry.held_outcome == "YES" else locks_yes
        )
        if not invalidates_held:
            continue
        quote = _quote_after(quote_times, quotes, at)
        if quote is None:
            continue
        bid, quote_at = quote
        net = bid - expected_taker_fee_per_share(price=bid, fee_rate=fee_rate)
        return ExitTrigger(
            kind="official_impossibility",
            at=quote_at,
            bid=bid,
            net_per_share=net,
            evidence=(
                f"observation={observed}{row['unit']}; "
                f"bucket={rule.bucket_kind}[{lower},{upper})"
            ),
        )
    return None


def replay_v5_event(
    connection: sqlite3.Connection,
    entry: LinkedEntry,
) -> EventReplay:
    quote_times, quotes = _quotes(connection, entry)
    triggers = [
        item
        for item in (
            _official_impossibility_exit(
                connection,
                entry,
                quote_times=quote_times,
                quotes=quotes,
            ),
            _model_value_exit(
                connection,
                entry,
                quote_times=quote_times,
                quotes=quotes,
            ),
        )
        if item is not None
    ]
    trigger = min(triggers, key=lambda item: item.at) if triggers else None
    if trigger is None:
        revenue = (
            entry.size if entry.resolved_outcome == entry.held_outcome else Decimal("0")
        )
        exit_kind = "settlement"
        exit_at = entry.settled_at
        evidence = "no permitted historical V5 exit; scored at final resolution"
    else:
        sell_fee = compute_taker_fee(
            shares=entry.size,
            price=trigger.bid,
            fee_rate=_fee_rate(connection, entry.market_id),
        )
        revenue = entry.size * trigger.bid - sell_fee
        exit_kind = trigger.kind
        exit_at = trigger.at
        evidence = trigger.evidence
    actual_fifo = _actual_fifo_revenue(connection, entry) - entry.cash
    hold_revenue = (
        entry.size if entry.resolved_outcome == entry.held_outcome else Decimal("0")
    )
    return EventReplay(
        event_key=entry.event_key,
        market_id=entry.market_id,
        held_outcome=entry.held_outcome,
        resolved_outcome=entry.resolved_outcome,
        horizon=entry.horizon,
        edge=str(entry.edge),
        reference_price=str(entry.reference_price),
        size=str(entry.size),
        buy_cash=str(entry.cash),
        old_path_pnl_fifo=str(actual_fifo),
        hold_pnl=str(hold_revenue - entry.cash),
        v5_exit_kind=exit_kind,
        v5_exit_at=exit_at.isoformat() if exit_at is not None else None,
        v5_revenue=str(revenue),
        v5_pnl=str(revenue - entry.cash),
        evidence=evidence,
    )


def _sum_decimal(rows: Iterable[dict[str, Any]], key: str) -> Decimal:
    return sum((_decimal(row[key]) for row in rows), Decimal("0"))


def build_report(
    connection: sqlite3.Connection,
    *,
    source: str,
) -> dict[str, Any]:
    entries = load_linked_entries(connection, entry_policy_version=V4_ENTRY_POLICY)
    by_market: dict[str, list[LinkedEntry]] = defaultdict(list)
    for entry in entries:
        by_market[entry.market_id].append(entry)

    actual_buy = Decimal("0")
    actual_sell = Decimal("0")
    hold_payout = Decimal("0")
    for market_entries in by_market.values():
        actual_buy += sum((entry.cash for entry in market_entries), Decimal("0"))
        actual_sell += _actual_market_sell_net(connection, market_entries)
        for entry in market_entries:
            if entry.resolved_outcome == entry.held_outcome:
                hold_payout += entry.size

    selected = select_v5_entries(entries)
    event_rows = [asdict(replay_v5_event(connection, entry)) for entry in selected]
    c_buy = _sum_decimal(event_rows, "buy_cash")
    c_old = _sum_decimal(event_rows, "old_path_pnl_fifo")
    c_hold = _sum_decimal(event_rows, "hold_pnl")
    c_pnl = _sum_decimal(event_rows, "v5_pnl")
    c_revenue = _sum_decimal(event_rows, "v5_revenue")
    model_exits = sum(row["v5_exit_kind"] == "model_value_exit" for row in event_rows)
    official_exits = sum(
        row["v5_exit_kind"] == "official_impossibility" for row in event_rows
    )
    winners = sum(
        row["held_outcome"] == row["resolved_outcome"] for row in event_rows
    )
    pnl_not_worse_than_old = c_pnl >= c_old
    right_tail_not_worse_than_hold = c_pnl >= c_hold
    return {
        "metadata": {
            "source": source,
            "opened_sqlite": "mode=ro",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "entry_source_policy": V4_ENTRY_POLICY,
            "model": V8_MODEL,
            "target_entry_policy": "weather-entry-v5",
            "target_exit_policy": "weather-exit-v2-settlement-core",
        },
        "methodology": {
            "entry_fill_linkage": "intent -> order_attempt -> reconciled fill",
            "event_identity": "casefold(city) + target_date",
            "entry_analysis": "latest V8 analysis at or before intent timestamp; max age 30m",
            "horizon": "model_signal horizon at entry analysis timestamp",
            "actual_selected_path": "FIFO allocation of later account SELL fills",
            "quote_binding": "first token-specific quote no more than 10m after evidence",
            "entry_caps": {
                "order": str(V5_ORDER_CAP),
                "event": str(V5_EVENT_CAP),
                "daily": str(V5_DAILY_CAP),
                "synthetic_starting_bankroll": str(V5_DAILY_CAP),
            },
            "known_limitations": [
                "historical best bid is not full-depth VWAP",
                "synthetic bankroll is explicit because historical free USDC is not persisted",
                "maker-first is not modeled",
                "unlinked fills are excluded",
            ],
        },
        "paths": {
            "A_current_v4_actual_cash": {
                "resolved_markets": len(by_market),
                "buy_cash": str(actual_buy),
                "sell_net": str(actual_sell),
                "cash_pnl": str(actual_sell - actual_buy),
            },
            "B_all_v4_hold_to_settlement": {
                "resolved_markets": len(by_market),
                "buy_cash": str(actual_buy),
                "settlement_payout": str(hold_payout),
                "pnl": str(hold_payout - actual_buy),
            },
            "C_v5_entry_plus_settlement_core": {
                "resolved_events": len(event_rows),
                "winners": winners,
                "buy_cash": str(c_buy),
                "revenue": str(c_revenue),
                "pnl": str(c_pnl),
                "old_path_fifo_pnl_same_entries": str(c_old),
                "hold_pnl_same_entries": str(c_hold),
                "model_value_exits": model_exits,
                "official_impossibility_exits": official_exits,
            },
            "D_maker_first": {
                "status": "NOT_MODELED",
                "reason": "no historical post-only V5b fill/markout model",
            },
        },
        "deployment_gate": {
            "historical_c_not_worse_than_old_same_entries": pnl_not_worse_than_old,
            "historical_c_not_worse_than_hold_same_entries": right_tail_not_worse_than_hold,
            "full_live_allowed": False,
            "reason": (
                f"only {len(event_rows)} resolved counterfactual V5 events; "
                "20 newly resolved real V5 events are required"
            ),
        },
        "events": event_rows,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--pretty", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    database = args.database.resolve()
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        report = build_report(connection, source=str(database))
    finally:
        connection.close()
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=args.pretty,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
