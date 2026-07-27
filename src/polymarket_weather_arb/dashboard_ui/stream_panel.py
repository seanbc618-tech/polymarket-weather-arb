"""V5–V7 stream-monitor + magazine-strip helpers for the /app console.

Uses real autopilot decisions + opportunity funnel counts only.
No demo event generators or fake throughput rates.
V7 CSS for position/funnel magazine streams lives in stream_panel.css.

Phase 1 local stream: read-only JSON deltas from SQLite via /app/stream.
This is not a live exchange feed.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from polymarket_weather_arb.adapters.http_reader import open_meteo_usage_snapshot
from polymarket_weather_arb.dashboard_ui.html import _e
from polymarket_weather_arb.services.cockpit_service import (
    OpportunityFunnel,
    VerifiedRealizedPnL,
)
from polymarket_weather_arb.storage.repositories import Repository

# Cap rendered client history and server delta batches.
STREAM_FEED_CAP = 80
STREAM_DELTA_LIMIT = 100
STREAM_POLL_MS = 2000
STREAM_STALE_MS = 8000


@lru_cache(maxsize=1)
def brand_mark_data_uri() -> str:
    """Load packaged brand mark; empty string falls back to solid mark."""
    asset = Path(__file__).resolve().parent / "assets" / "brand-mark.png"
    if not asset.is_file():
        return ""
    encoded = base64.b64encode(asset.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def brand_mark_html() -> str:
    uri = brand_mark_data_uri()
    if not uri:
        return ""
    return f'<img src="{uri}" alt="" width="32" height="32" decoding="async" />'


def _stream_action_class(action: str, status: str) -> str:
    key = f"{action} {status}".lower()
    if "fill" in key or "matched" in key:
        return "act-fill"
    if "submit" in key or "placed" in key or "open" in key:
        return "act-submit"
    if "reject" in key or "block" in key:
        return "act-reject"
    if "skip" in key or "hold" in key:
        return "act-skip"
    if "tick" in key or "idle" in key:
        return "act-tick"
    return "act-tick"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _serialize_rows(
    rows: list[Any], *, fields: tuple[str, ...] | None = None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        keys = set(row.keys())
        if fields is not None:
            data = {key: _json_safe(row[key]) for key in fields if key in keys}
        else:
            data = {key: _json_safe(row[key]) for key in keys}
        out.append(data)
    return out


def build_app_stream_payload(
    repository: Repository,
    *,
    after_decision_id: int = 0,
    after_fill_id: int = 0,
    after_intent_id: int = 0,
    after_attempt_id: int = 0,
    after_analysis_id: int = 0,
    limit: int = STREAM_DELTA_LIMIT,
) -> dict[str, Any]:
    """Read-only local SQLite delta for /app stream polling.

    Never calls exchange or weather providers. Labels source as local_sqlite.
    """
    limit = max(1, min(int(limit), 500))
    after_decision_id = max(0, int(after_decision_id))
    after_fill_id = max(0, int(after_fill_id))
    after_intent_id = max(0, int(after_intent_id))
    after_attempt_id = max(0, int(after_attempt_id))
    after_analysis_id = max(0, int(after_analysis_id))

    decisions = repository.list_autopilot_decisions_after(after_id=after_decision_id, limit=limit)
    fills = repository.list_fills_after(after_id=after_fill_id, limit=limit)
    intents = repository.list_order_intents_after(after_id=after_intent_id, limit=limit)
    attempts = repository.list_order_attempts_after(after_id=after_attempt_id, limit=limit)
    analyses = repository.list_analyses_after(after_id=after_analysis_id, limit=limit)
    state = repository.get_autopilot_state()
    high_water = repository.stream_cursor_high_water()

    def _max_id(rows: list[Any], fallback: int) -> int:
        if not rows:
            return fallback
        return max(int(row["id"]) for row in rows)

    decision_rows = _serialize_rows(
        decisions,
        fields=(
            "id",
            "market_id",
            "action",
            "mode",
            "edge",
            "reason",
            "status",
            "intent_id",
            "discovered",
            "created_at",
            "llm_provider",
            "llm_model",
            "llm_confidence",
            "llm_reason",
        ),
    )
    fill_rows = _serialize_rows(
        fills,
        fields=(
            "id",
            "exchange_fill_id",
            "order_id",
            "market_id",
            "side",
            "price",
            "size",
            "fee",
            "filled_at",
        ),
    )
    intent_rows = _serialize_rows(
        intents,
        fields=(
            "id",
            "market_id",
            "side",
            "limit_price",
            "size",
            "filled_size",
            "notional",
            "status",
            "dry_run",
            "created_at",
        ),
    )
    attempt_rows = _serialize_rows(
        attempts,
        fields=("id", "intent_id", "status", "error", "created_at"),
    )
    analysis_rows = _serialize_rows(
        analyses,
        fields=(
            "id",
            "market_id",
            "model_version",
            "fair_lower",
            "fair_upper",
            "reference_price",
            "edge",
            "side",
            "decision",
            "created_at",
        ),
    )

    health: dict[str, Any]
    if state is None:
        health = {
            "enabled": False,
            "mode": "dry_run",
            "app_mode": "paper",
            "tick_seconds": 300,
            "tick_count": 0,
            "last_tick_at": None,
            "last_tick_status": None,
            "last_error": None,
            "last_tick_duration_ms": None,
            "deferred_candidates_count": None,
            "process_started_at": None,
            "latest_useful_tick_at": None,
            "local_transport": "sqlite",
            "exchange_stream_status": "disabled",
            "exchange_stream_updated_at": None,
            "exchange_stream": {
                "status": "disabled",
                "subscribed_token_count": 0,
                "rest_fallback_active": True,
            },
            "open_meteo_usage": open_meteo_usage_snapshot(),
        }
    else:
        state_map = dict(state)
        stream_detail_raw = state_map.get("exchange_stream_detail")
        stream_detail: dict[str, Any] = {}
        if isinstance(stream_detail_raw, str) and stream_detail_raw.strip():
            try:
                import json

                parsed = json.loads(stream_detail_raw)
                if isinstance(parsed, dict):
                    stream_detail = {
                        k: v
                        for k, v in parsed.items()
                        if not any(
                            s in str(k).lower()
                            for s in ("key", "secret", "credential", "private", "auth")
                        )
                    }
            except Exception:
                stream_detail = {}
        stream_status = str(state_map.get("exchange_stream_status") or "disabled")
        health = {
            "enabled": bool(state_map.get("enabled")),
            "mode": state_map.get("mode") or "dry_run",
            "app_mode": state_map.get("app_mode") or "paper",
            "tick_seconds": int(state_map.get("tick_seconds") or 300),
            "tick_count": int(state_map.get("tick_count") or 0),
            "last_tick_at": state_map.get("last_tick_at"),
            "last_tick_status": state_map.get("last_tick_status"),
            "last_error": state_map.get("last_error"),
            "last_tick_duration_ms": state_map.get("last_tick_duration_ms"),
            "deferred_candidates_count": state_map.get("deferred_candidates_count"),
            "process_started_at": state_map.get("process_started_at"),
            "latest_useful_tick_at": state_map.get("latest_useful_tick_at"),
            "local_transport": "sqlite",
            "exchange_stream_status": stream_status,
            "exchange_stream_updated_at": state_map.get("exchange_stream_updated_at"),
            "exchange_stream": {
                "status": stream_status,
                "subscribed_token_count": int(stream_detail.get("subscribed_token_count") or 0),
                "rest_fallback_active": bool(
                    stream_detail.get("rest_fallback_active", stream_status != "live")
                ),
                "coalesced": stream_detail.get("coalesced"),
                "dropped": stream_detail.get("dropped"),
            },
            "open_meteo_usage": open_meteo_usage_snapshot(),
        }

    return {
        "source": "local_sqlite",
        "kind": "local_db_delta",
        "exchange_feed": False,
        "polled_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "health": health,
        "decisions": decision_rows,
        "fills": fill_rows,
        "order_intents": intent_rows,
        "order_attempts": attempt_rows,
        "analyses": analysis_rows,
        "cursors": {
            "after_decision_id": _max_id(decisions, after_decision_id),
            "after_fill_id": _max_id(fills, after_fill_id),
            "after_intent_id": _max_id(intents, after_intent_id),
            "after_attempt_id": _max_id(attempts, after_attempt_id),
            "after_analysis_id": _max_id(analyses, after_analysis_id),
        },
        "high_water": high_water,
        "limits": {"delta": limit, "feed_cap": STREAM_FEED_CAP},
    }


def funnel_categorical_svg(funnel: OpportunityFunnel) -> str:
    """Separate categorical funnel bars (not a fake time series)."""
    stages = [
        ("D", max(0, int(funnel.discovered))),
        ("R", max(0, int(funnel.rule_tradable))),
        ("Q", max(0, int(funnel.quote_available))),
        ("F", max(0, int(funnel.forecast_available))),
        ("A", max(0, int(funnel.analyzed))),
        ("S", max(0, int(funnel.quant_trade_signal))),
        ("O", max(0, int(funnel.live_submitted))),
        ("X", max(0, int(funnel.exchange_fill))),
    ]
    width, height, pad = 640, 56, 8
    peak = max(v for _, v in stages) if any(v for _, v in stages) else 1
    n = len(stages)
    gap = 6
    bar_w = (width - pad * 2 - gap * (n - 1)) / n
    bars: list[str] = []
    labels: list[str] = []
    for index, (label, value) in enumerate(stages):
        x = pad + index * (bar_w + gap)
        h = (value / peak) * (height - pad * 2 - 12)
        y = height - pad - 12 - h
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{max(h, 1):.1f}" '
            f'rx="2" fill="oklch(58% 0.11 195 / 0.55)" />'
        )
        labels.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{height - 2}" text-anchor="middle" '
            f'font-size="9" fill="oklch(48% 0.018 250)">{label}</text>'
        )
    return (
        f'<svg class="funnel-cat-svg" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="opportunity funnel stages">'
        f"{''.join(bars)}{''.join(labels)}</svg>"
    )


def stream_time_series_svg(
    points: list[tuple[float, float]],
    *,
    empty_label: str = "No timestamped series yet",
) -> str:
    """True time-axis series from (unix_ts, value) points."""
    width, height, pad = 640, 140, 14
    if not points:
        return (
            f'<svg class="time-series-svg" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="{_e(empty_label)}">'
            f'<text x="{width / 2:.0f}" y="{height / 2:.0f}" text-anchor="middle" '
            f'dominant-baseline="middle" font-size="12" fill="oklch(55% 0.014 250)">'
            f"{_e(empty_label)}</text></svg>"
        )
    if len(points) == 1:
        value = points[0][1]
        return (
            f'<svg class="time-series-svg" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="single edge point {value:.3f}">'
            f'<circle cx="{width / 2:.0f}" cy="{height / 2:.0f}" r="4" '
            'fill="oklch(58% 0.11 195)" />'
            f'<text x="{width / 2:.0f}" y="{height / 2 + 22:.0f}" text-anchor="middle" '
            'font-size="11" fill="oklch(55% 0.014 250)">'
            f"edge {value:.3f}</text></svg>"
        )

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 1e-9)
    span_y = max_y - min_y
    # Expand flat series slightly so a constant edge is still visible.
    if span_y < 1e-9:
        min_y -= 0.01
        max_y += 0.01
        span_y = max_y - min_y

    def map_pt(x: float, y: float) -> tuple[float, float]:
        px = pad + ((x - min_x) / span_x) * (width - pad * 2)
        py = height - pad - ((y - min_y) / span_y) * (height - pad * 2)
        return px, py

    poly = " ".join(f"{map_pt(x, y)[0]:.1f},{map_pt(x, y)[1]:.1f}" for x, y in points)
    dots = "".join(
        f'<circle cx="{map_pt(x, y)[0]:.1f}" cy="{map_pt(x, y)[1]:.1f}" r="2.2" '
        f'fill="oklch(58% 0.11 195)" />'
        for x, y in points[-12:]
    )
    return (
        f'<svg class="time-series-svg" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="edge over time">'
        f'<polyline fill="none" stroke="oklch(58% 0.11 195)" stroke-width="2.2" '
        f'stroke-linecap="round" stroke-linejoin="round" points="{poly}" />'
        f"{dots}</svg>"
    )


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def edge_series_from_decisions(decisions: list[Any]) -> list[tuple[float, float]]:
    """Oldest→newest edge points for charting (honest when edge+timestamp exist)."""
    points: list[tuple[float, float]] = []
    for row in reversed(list(decisions)):
        if isinstance(row, dict):
            edge = row.get("edge")
            created = row.get("created_at")
        else:
            edge = row["edge"] if "edge" in row.keys() else None
            created = row["created_at"] if "created_at" in row.keys() else None
        if edge is None:
            continue
        ts = _parse_ts(str(created) if created is not None else None)
        if ts is None:
            continue
        try:
            points.append((ts, float(edge)))
        except (TypeError, ValueError):
            continue
    return points


def stream_sparkline_svg(funnel: OpportunityFunnel) -> str:
    """Deprecated funnel waveform; kept for import compatibility.

    Prefer stream_time_series_svg + funnel_categorical_svg.
    """
    return funnel_categorical_svg(funnel)


def _normalize_feed_event(kind: str, row: dict[str, Any]) -> dict[str, Any]:
    """Map ledger rows into a single feed event shape for SSR and client parity."""
    if kind == "decision":
        return {
            "kind": "decision",
            "id": row.get("id"),
            "market_id": row.get("market_id"),
            "action": row.get("action") or "tick",
            "status": row.get("status") or "",
            "reason": row.get("reason") or row.get("status") or "—",
            "edge": row.get("edge"),
            "created_at": row.get("created_at") or "",
        }
    if kind == "intent":
        side = str(row.get("side") or "")
        status = str(row.get("status") or "intent")
        price = row.get("limit_price")
        size = row.get("size")
        filled_size = row.get("filled_size")
        dry = int(row.get("dry_run") or 0)
        detail_parts = [side or "order", status]
        if price is not None:
            try:
                detail_parts.append(f"@{float(price):.2f}")
            except (TypeError, ValueError):
                pass
        if size is not None:
            try:
                detail_parts.append(f"×{float(size):g}")
            except (TypeError, ValueError):
                pass
        if filled_size is not None and not dry:
            try:
                detail_parts.append(f"filled {float(filled_size or 0):g}/{float(size or 0):g}")
            except (TypeError, ValueError):
                pass
        if dry:
            detail_parts.append("dry-run")
        return {
            "kind": "intent",
            "id": row.get("id"),
            "market_id": row.get("market_id"),
            "action": "intent",
            "status": status,
            "reason": " ".join(detail_parts),
            "edge": None,
            "created_at": row.get("created_at") or "",
        }
    if kind == "attempt":
        status = str(row.get("status") or "attempt")
        error = str(row.get("error") or "").strip()
        intent_id = row.get("intent_id")
        reason = f"attempt #{intent_id} {status}" if intent_id is not None else f"attempt {status}"
        if error:
            reason = f"{reason}: {error[:120]}"
        return {
            "kind": "attempt",
            "id": row.get("id"),
            "market_id": None,
            "action": "attempt",
            "status": status,
            "reason": reason,
            "edge": None,
            "created_at": row.get("created_at") or "",
        }
    if kind == "fill":
        side = str(row.get("side") or "fill")
        price = row.get("price")
        size = row.get("size")
        parts = [side]
        try:
            if price is not None:
                parts.append(f"@{float(price):.2f}")
        except (TypeError, ValueError):
            pass
        try:
            if size is not None:
                parts.append(f"×{float(size):g}")
        except (TypeError, ValueError):
            pass
        return {
            "kind": "fill",
            "id": row.get("id"),
            "market_id": row.get("market_id"),
            "action": "fill",
            "status": "filled",
            "reason": " ".join(parts),
            "edge": None,
            "created_at": row.get("filled_at") or row.get("created_at") or "",
        }
    raise ValueError(f"unknown stream feed kind: {kind}")


def _feed_item_html(event: dict[str, Any], labels: dict[str, str]) -> str:
    kind = str(event.get("kind") or "decision")
    created = str(event.get("created_at") or "")
    time_label = created.replace("T", " ")[11:19] if "T" in created else created[:8] or "—"
    action = str(event.get("action") or kind)
    action_label = (
        labels.get("stream_action_entry_minimum_blocked", "MIN ORDER")
        if action == "entry_minimum_blocked"
        else action.upper()[:8]
    )
    status = str(event.get("status") or "")
    act_cls = _stream_action_class(action, status)
    market = str(event.get("market_id") or labels.get("never", "—"))
    reason = str(event.get("reason") or status or "—")
    edge = event.get("edge")
    if edge is None:
        edge_html = '<span class="edge muted">—</span>'
    else:
        try:
            edge_html = f'<span class="edge">{float(edge):+.2f}</span>'
        except (TypeError, ValueError):
            edge_html = f'<span class="edge muted">{_e(str(edge))}</span>'
    row_id = event.get("id")
    id_attr = ""
    if row_id is not None:
        try:
            id_attr = f' data-{kind}-id="{int(row_id)}"'
        except (TypeError, ValueError):
            id_attr = ""
    utc_title = _e(created) if created else ""
    return (
        f'<div class="feed-item" data-stream-kind="{_e(kind)}"{id_attr} '
        f'title="{utc_title} UTC">'
        f'<span class="t" data-local-time="{_e(created)}">{_e(time_label)}</span>'
        f'<span class="act {act_cls}">{_e(action_label)}</span>'
        '<div class="msg">'
        f'<div class="line1">{_e(market)}</div>'
        f'<div class="line2">{_e(reason)}</div>'
        "</div>"
        f"{edge_html}"
        "</div>"
    )


def _row_as_dict(row: Any) -> dict[str, Any]:
    return {key: _json_safe(row[key]) for key in row.keys()}


def _seed_lifecycle_events(repository: Repository, *, limit: int = 24) -> list[dict[str, Any]]:
    """Newest-first unified feed seed from existing ledgers (no upstream I/O)."""
    limit = max(1, min(int(limit), STREAM_FEED_CAP))
    events: list[dict[str, Any]] = []
    for row in repository.list_autopilot_decisions(limit=limit):
        events.append(_normalize_feed_event("decision", _row_as_dict(row)))
    for row in repository.list_recent_order_intents(limit=limit):
        events.append(_normalize_feed_event("intent", _row_as_dict(row)))
    # Attempts lack a newest-first helper; take a bounded window then reverse.
    attempt_rows = list(repository.list_order_attempts_after(after_id=0, limit=max(limit * 4, 40)))
    for row in reversed(attempt_rows[-limit:]):
        events.append(_normalize_feed_event("attempt", _row_as_dict(row)))
    for row in repository.list_fills(limit=limit):
        events.append(_normalize_feed_event("fill", _row_as_dict(row)))

    def sort_key(event: dict[str, Any]) -> tuple[float, int]:
        ts = _parse_ts(str(event.get("created_at") or "")) or 0.0
        try:
            eid = int(event.get("id") or 0)
        except (TypeError, ValueError):
            eid = 0
        return (ts, eid)

    events.sort(key=sort_key, reverse=True)
    return events[:limit]


def render_stream_monitor_panel(
    snapshot,
    funnel: OpportunityFunnel,
    pnl: VerifiedRealizedPnL,
    labels: dict[str, str],
    *,
    repository: Repository | None = None,
    cursors: dict[str, int] | None = None,
    poll_ms: int | None = None,
) -> str:
    decisions = list(getattr(snapshot, "decisions", ()) or ())
    tick_count = int(getattr(snapshot, "tick_count", 0) or 0)
    tick_seconds = int(getattr(snapshot, "tick_seconds", 0) or 0)
    effective_poll_ms = int(poll_ms) if poll_ms is not None else STREAM_POLL_MS
    poll_seconds = f"{effective_poll_ms / 1000:g}"
    cycle_label = labels.get(
        "stream_cadence_format", "{poll}s local / {strategy}s strategy"
    ).format(poll=poll_seconds, strategy=tick_seconds or "—")

    reject_count = sum(int(b.count) for b in (funnel.blockers or [])[:12])
    fill_count = int(funnel.exchange_fill)
    discovered = int(funnel.discovered)

    # Prefer multi-ledger seed when repository is available so intents/fills
    # appear without waiting for the first poll after a full page load.
    if repository is not None:
        seed_events = _seed_lifecycle_events(repository, limit=min(24, STREAM_FEED_CAP))
    else:
        seed_events = []
        for row in decisions[:STREAM_FEED_CAP]:
            if not isinstance(row, dict):
                row = {
                    "market_id": row["market_id"] if "market_id" in row.keys() else None,
                    "action": row["action"] if "action" in row.keys() else "tick",
                    "status": row["status"] if "status" in row.keys() else "",
                    "reason": row["reason"] if "reason" in row.keys() else "",
                    "edge": row["edge"] if "edge" in row.keys() else None,
                    "created_at": row["created_at"] if "created_at" in row.keys() else "",
                    "id": row["id"] if "id" in row.keys() else None,
                }
            seed_events.append(_normalize_feed_event("decision", row))

    feed_items = [_feed_item_html(event, labels) for event in seed_events]
    event_count = len(feed_items)

    if feed_items:
        feed_html = "".join(feed_items)
        first = seed_events[0]
        last_reason = str(first.get("reason") or first.get("action") or "—")
        last_html = f'<span class="spark-pulse"></span>{_e(last_reason)}'
    else:
        feed_html = (
            f'<div class="feed-empty" data-stream-empty="1">{_e(labels["stream_feed_empty"])}</div>'
        )
        last_html = f'<span class="spark-pulse"></span>{_e(labels["stream_foot_waiting"])}'

    recon_ok = bool(getattr(pnl, "reconciliation_fresh", False))
    recon_label = (
        labels["stream_foot_recon_fresh"] if recon_ok else labels["stream_foot_recon_stale"]
    )

    # Chart uses decision net edge only — never mix in all-market analyses.
    edge_points = edge_series_from_decisions(decisions)
    chart_svg = stream_time_series_svg(
        edge_points,
        empty_label=labels.get(
            "stream_chart_empty", "No decision edge points yet (analyses not mixed in)"
        ),
    )
    funnel_svg = funnel_categorical_svg(funnel)

    cursors = cursors or {
        "after_decision_id": 0,
        "after_fill_id": 0,
        "after_intent_id": 0,
        "after_attempt_id": 0,
        "after_analysis_id": 0,
    }
    cursor_json = json.dumps(cursors, separators=(",", ":"))
    labels_json = json.dumps(
        {
            "live": labels.get("stream_conn_live", "local live"),
            "reconnecting": labels.get("stream_conn_reconnecting", "reconnecting"),
            "stale": labels.get("stream_conn_stale", "stale"),
            "empty": labels.get("stream_feed_empty", "No events yet."),
            "source": labels.get("stream_foot_source_val", "local SQLite · not exchange tape"),
            "waiting": labels.get("stream_foot_waiting", "Waiting…"),
            "chart_empty": labels.get(
                "stream_chart_empty",
                "No decision edge points yet (analyses not mixed in)",
            ),
            "cadence": labels.get("stream_cadence_format", "{poll}s local / {strategy}s strategy"),
            "never": labels.get("never", "—"),
        },
        ensure_ascii=False,
    )

    poll_script = _stream_poll_script()

    return f"""
    <section class="stream-panel" id="panel-stream" data-od-id="panel-stream-live"
             data-stream-root="1"
             data-poll-ms="{effective_poll_ms}"
             data-stale-ms="{STREAM_STALE_MS}"
             data-feed-cap="{STREAM_FEED_CAP}"
             data-cursors='{cursor_json}'
             data-labels='{labels_json}'
             aria-labelledby="stream-title">
      <div class="panel-head">
        <div class="panel-title-wrap">
          <h2 class="panel-title" id="stream-title">{_e(labels["stream_title"])}</h2>
          <span class="status-pill status-neutral mono stream-conn"
                data-stream-conn="1" data-conn="reconnecting">
            <span class="live-dot" aria-hidden="true"></span>
            <span data-stream-conn-label>{_e(labels.get("stream_conn_reconnecting", "reconnecting"))}</span>
          </span>
          <span class="status-pill status-neutral mono"
                data-stream-tick>{_e(labels["stream_tick"].format(count=tick_count))}</span>
        </div>
        <div class="panel-meta">
          <span class="panel-sub">{_e(labels["stream_sub"])}</span>
        </div>
      </div>
      <div class="stream-layout">
        <div class="stream-viz">
          <div class="stream-stats" aria-label="{_e(labels["stream_title"])}">
            <div class="sstat">
              <div class="k">{_e(labels["stream_stat_events"])}</div>
              <div class="v hi" data-stream-event-count>{event_count}</div>
            </div>
            <div class="sstat">
              <div class="k">{_e(labels["stream_stat_cycle"])}</div>
              <div class="v ok" data-stream-cycle>{_e(cycle_label)}</div>
            </div>
            <div class="sstat">
              <div class="k">{_e(labels["stream_stat_disc"])}</div>
              <div class="v">{discovered}</div>
            </div>
            <div class="sstat">
              <div class="k">{_e(labels["stream_stat_fill"])}</div>
              <div class="v"><span style="color:var(--success)" data-stream-fill-count>{fill_count}</span>
                <span style="color:var(--muted-2);font-weight:500">/</span>
                <span style="color:oklch(48% 0.1 75)" data-stream-reject-count>{reject_count}</span></div>
            </div>
          </div>
          <div class="chart-wrap" aria-label="{_e(labels.get("stream_chart_edge", "Decision net edge over time"))}">
            <div data-stream-chart>{chart_svg}</div>
            <div class="chart-overlay">
              <div class="chart-legend">
                <span><i class="sw"></i>{_e(labels.get("stream_legend_edge", "Decision net edge (selected)"))}</span>
              </div>
              <div class="chart-axis">
                <span>{_e(labels.get("stream_axis_time_old", "earlier"))}</span>
                <span>{_e(labels.get("stream_axis_time_now", "now"))}</span>
              </div>
            </div>
          </div>
          <div class="funnel-cat-wrap" aria-label="{_e(labels.get("stream_funnel_cat", "Opportunity funnel"))}">
            <div class="funnel-cat-label">{_e(labels.get("stream_funnel_cat", "Opportunity funnel"))}</div>
            {funnel_svg}
          </div>
        </div>
        <div class="stream-feed">
          <div class="feed-head">
            <h3>{_e(labels["stream_feed_title"])}</h3>
            <span class="meta" data-stream-feed-meta>{_e(labels["stream_feed_meta"].format(count=event_count))}</span>
          </div>
          <div class="feed-scroll" role="log" aria-live="polite" aria-relevant="additions"
               aria-label="{_e(labels["stream_feed_title"])}" data-od-id="stream-feed"
               data-stream-feed="1">
            {feed_html}
          </div>
        </div>
      </div>
      <div class="stream-foot">
        <div class="sfoot">
          <div class="k">{_e(labels["stream_foot_last"])}</div>
          <div class="v" data-stream-last>{last_html}</div>
        </div>
        <div class="sfoot">
          <div class="k">{_e(labels["stream_foot_source"])}</div>
          <div class="v mono">{_e(labels.get("stream_foot_source_val", "local SQLite · not exchange tape"))}</div>
        </div>
        <div class="sfoot">
          <div class="k">{_e(labels["stream_foot_recon"])}</div>
          <div class="v">{_e(recon_label)}</div>
        </div>
      </div>
      {poll_script}
    </section>
    """


def _stream_poll_script() -> str:
    """Browser poller: local SQLite deltas only; no exchange WebSocket claims.

    Renders decision / intent / attempt / fill lifecycle rows. Edge chart uses
    decision net edge only (never mixes all-market analyses).
    """
    return r"""
<script>
(function () {
  const root = document.querySelector('[data-stream-root="1"]');
  if (!root || root.dataset.streamBound === '1') return;
  root.dataset.streamBound = '1';

  const pollMs = Number(root.dataset.pollMs || 2000);
  const staleMs = Number(root.dataset.staleMs || 8000);
  const feedCap = Number(root.dataset.feedCap || 80);
  let cursors = {};
  let labels = {};
  try { cursors = JSON.parse(root.dataset.cursors || '{}'); } catch (_) { cursors = {}; }
  try { labels = JSON.parse(root.dataset.labels || '{}'); } catch (_) { labels = {}; }

  const feed = root.querySelector('[data-stream-feed="1"]');
  const connEl = root.querySelector('[data-stream-conn="1"]');
  const connLabel = root.querySelector('[data-stream-conn-label]');
  const chartEl = root.querySelector('[data-stream-chart]');
  const lastEl = root.querySelector('[data-stream-last]');
  const eventCountEl = root.querySelector('[data-stream-event-count]');
  const tickEl = root.querySelector('[data-stream-tick]');
  const feedMetaEl = root.querySelector('[data-stream-feed-meta]');
  const cycleEl = root.querySelector('[data-stream-cycle]');
  const fillCountEl = root.querySelector('[data-stream-fill-count]');

  let lastOkAt = Date.now();
  let inFlight = false;
  let timer = null;
  let liveFillCount = fillCountEl ? Number(fillCountEl.textContent || 0) : 0;
  const seen = {
    decision: new Set(),
    intent: new Set(),
    attempt: new Set(),
    fill: new Set(),
  };
  // Decision net edge only — selected/decision rows, never analyses bulk.
  const edgeSeries = [];

  if (feed) {
    for (const item of feed.querySelectorAll('.feed-item')) {
      const kind = item.getAttribute('data-stream-kind') || 'decision';
      const idAttr = item.getAttribute('data-' + kind + '-id');
      const id = Number(idAttr);
      if (Number.isFinite(id) && seen[kind]) seen[kind].add(id);
      if (kind !== 'decision') continue;
      const edgeNode = item.querySelector('.edge:not(.muted)');
      const timeNode = item.querySelector('[data-local-time]');
      if (!edgeNode || !timeNode) continue;
      const edge = Number(String(edgeNode.textContent || '').replace('+', ''));
      const ts = Date.parse(timeNode.getAttribute('data-local-time') || '');
      if (Number.isFinite(ts) && Number.isFinite(edge)) {
        edgeSeries.push([ts / 1000, edge]);
      }
    }
    edgeSeries.sort(function (a, b) { return a[0] - b[0]; });
  }

  function setConn(state) {
    if (!connEl || !connLabel) return;
    connEl.dataset.conn = state;
    connEl.classList.toggle('pill-live', state === 'live');
    connEl.classList.toggle('status-pill', true);
    connEl.classList.toggle('status-neutral', state !== 'live');
    connLabel.textContent = labels[state] || state;
  }

  function pad(n) { return String(n).padStart(2, '0'); }

  function localClock(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso).slice(11, 19) || '—';
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  function actionClass(action, status) {
    const key = ((action || '') + ' ' + (status || '')).toLowerCase();
    if (key.includes('fill') || key.includes('matched')) return 'act-fill';
    if (key.includes('submit') || key.includes('placed') || key.includes('open') || key.includes('intent')) return 'act-submit';
    if (key.includes('reject') || key.includes('block') || key.includes('fail') || key.includes('cancel')) return 'act-reject';
    if (key.includes('skip') || key.includes('hold')) return 'act-skip';
    if (key.includes('attempt')) return 'act-submit';
    return 'act-tick';
  }

  function esc(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function normalizeEvent(kind, row) {
    if (kind === 'decision') {
      return {
        kind: 'decision',
        id: row.id,
        market_id: row.market_id,
        action: row.action || 'tick',
        status: row.status || '',
        reason: row.reason || row.status || '—',
        edge: row.edge,
        created_at: row.created_at || '',
      };
    }
    if (kind === 'intent') {
      const parts = [row.side || 'order', row.status || 'intent'];
      if (row.limit_price != null && Number.isFinite(Number(row.limit_price))) {
        parts.push('@' + Number(row.limit_price).toFixed(2));
      }
      if (row.size != null && Number.isFinite(Number(row.size))) {
        parts.push('×' + Number(row.size));
      }
      if (!Number(row.dry_run) && row.filled_size != null &&
          Number.isFinite(Number(row.filled_size)) && Number.isFinite(Number(row.size))) {
        parts.push('filled ' + Number(row.filled_size) + '/' + Number(row.size));
      }
      if (Number(row.dry_run)) parts.push('dry-run');
      return {
        kind: 'intent',
        id: row.id,
        market_id: row.market_id,
        action: 'intent',
        status: row.status || '',
        reason: parts.join(' '),
        edge: null,
        created_at: row.created_at || '',
      };
    }
    if (kind === 'attempt') {
      let reason = 'attempt' + (row.intent_id != null ? ' #' + row.intent_id : '') +
        ' ' + (row.status || '');
      if (row.error) reason += ': ' + String(row.error).slice(0, 120);
      return {
        kind: 'attempt',
        id: row.id,
        market_id: null,
        action: 'attempt',
        status: row.status || '',
        reason: reason,
        edge: null,
        created_at: row.created_at || '',
      };
    }
    // fill
    const parts = [row.side || 'fill'];
    if (row.price != null && Number.isFinite(Number(row.price))) {
      parts.push('@' + Number(row.price).toFixed(2));
    }
    if (row.size != null && Number.isFinite(Number(row.size))) {
      parts.push('×' + Number(row.size));
    }
    return {
      kind: 'fill',
      id: row.id,
      market_id: row.market_id,
      action: 'fill',
      status: 'filled',
      reason: parts.join(' '),
      edge: null,
      created_at: row.filled_at || row.created_at || '',
    };
  }

  function feedItemHtml(event) {
    const kind = event.kind || 'decision';
    const created = event.created_at || '';
    const timeLabel = localClock(created);
    const action = event.action || kind;
    const status = event.status || '';
    const actCls = actionClass(action, status);
    const market = event.market_id || labels.never || '—';
    const reason = event.reason || status || '—';
    let edgeHtml = '<span class="edge muted">—</span>';
    if (event.edge != null && event.edge !== '') {
      const n = Number(event.edge);
      if (Number.isFinite(n)) {
        edgeHtml = '<span class="edge">' + (n >= 0 ? '+' : '') + n.toFixed(2) + '</span>';
      }
    }
    const id = event.id != null ? Number(event.id) : null;
    const idAttr = Number.isFinite(id) ? ' data-' + kind + '-id="' + id + '"' : '';
    return (
      '<div class="feed-item" data-stream-kind="' + esc(kind) + '"' + idAttr +
      ' title="' + esc(created) + ' UTC">' +
      '<span class="t" data-local-time="' + esc(created) + '">' + esc(timeLabel) + '</span>' +
      '<span class="act ' + actCls + '">' + esc(String(action).toUpperCase().slice(0, 8)) + '</span>' +
      '<div class="msg"><div class="line1">' + esc(market) + '</div>' +
      '<div class="line2">' + esc(reason) + '</div></div>' +
      edgeHtml + '</div>'
    );
  }

  function refreshFeedMeta() {
    if (!feed) return;
    const count = feed.querySelectorAll('.feed-item').length;
    if (eventCountEl) eventCountEl.textContent = String(count);
    if (feedMetaEl) feedMetaEl.textContent = String(count) + ' · local';
    while (feed.querySelectorAll('.feed-item').length > feedCap) {
      const last = feed.querySelector('.feed-item:last-child');
      if (!last) break;
      last.remove();
    }
  }

  function appendEvents(kind, rows) {
    if (!feed || !rows || !rows.length) return 0;
    const empty = feed.querySelector('[data-stream-empty="1"]');
    if (empty) empty.remove();
    let added = 0;
    const newestFirst = rows.slice().reverse();
    for (const row of newestFirst) {
      const event = normalizeEvent(kind, row);
      const id = event.id != null ? Number(event.id) : null;
      if (Number.isFinite(id) && seen[kind]) {
        if (seen[kind].has(id)) continue;
        seen[kind].add(id);
      }
      feed.insertAdjacentHTML('afterbegin', feedItemHtml(event));
      added += 1;
      if (kind === 'decision' && event.edge != null && event.created_at) {
        const ts = Date.parse(event.created_at);
        const edge = Number(event.edge);
        if (Number.isFinite(ts) && Number.isFinite(edge)) {
          edgeSeries.push([ts / 1000, edge]);
        }
      }
      if (kind === 'fill') {
        liveFillCount += 1;
        if (fillCountEl) fillCountEl.textContent = String(liveFillCount);
      }
      if (lastEl) {
        lastEl.innerHTML = '<span class="spark-pulse"></span>' +
          esc(event.reason || event.action || labels.waiting || '—');
      }
    }
    if (added) refreshFeedMeta();
    return added;
  }

  function redrawChart() {
    if (!chartEl) return;
    const pts = edgeSeries.slice(-feedCap);
    if (!pts.length) {
      chartEl.innerHTML =
        '<svg class="time-series-svg" viewBox="0 0 640 140" role="img">' +
        '<text x="320" y="70" text-anchor="middle" dominant-baseline="middle" ' +
        'font-size="12" fill="oklch(55% 0.014 250)">' +
        esc(labels.chart_empty || 'No decision edge points yet') +
        '</text></svg>';
      return;
    }
    if (pts.length === 1) {
      const edge = pts[0][1];
      chartEl.innerHTML =
        '<svg class="time-series-svg" viewBox="0 0 640 140" role="img" aria-label="single edge point">' +
        '<circle cx="320" cy="70" r="4" fill="oklch(58% 0.11 195)" />' +
        '<text x="320" y="92" text-anchor="middle" font-size="11" ' +
        'fill="oklch(55% 0.014 250)">edge ' + esc(edge.toFixed(3)) + '</text></svg>';
      return;
    }
    const width = 640, height = 140, pad = 14;
    const xs = pts.map(function (p) { return p[0]; });
    const ys = pts.map(function (p) { return p[1]; });
    let minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs);
    let minY = Math.min.apply(null, ys), maxY = Math.max.apply(null, ys);
    let spanX = Math.max(maxX - minX, 1e-9);
    let spanY = maxY - minY;
    if (spanY < 1e-9) { minY -= 0.01; maxY += 0.01; spanY = maxY - minY; }
    function map(x, y) {
      const px = pad + ((x - minX) / spanX) * (width - pad * 2);
      const py = height - pad - ((y - minY) / spanY) * (height - pad * 2);
      return [px, py];
    }
    const poly = pts.map(function (p) {
      const m = map(p[0], p[1]);
      return m[0].toFixed(1) + ',' + m[1].toFixed(1);
    }).join(' ');
    chartEl.innerHTML =
      '<svg class="time-series-svg" viewBox="0 0 640 140" role="img" aria-label="decision net edge">' +
      '<polyline fill="none" stroke="oklch(58% 0.11 195)" stroke-width="2.2" ' +
      'stroke-linecap="round" stroke-linejoin="round" points="' + poly + '" /></svg>';
  }

  function applyHealth(health) {
    if (!health) return;
    if (tickEl && health.tick_count != null) {
      tickEl.textContent = 'tick #' + health.tick_count;
    }
    if (cycleEl && health.tick_seconds) {
      const pattern = labels.cadence || '{poll}s local / {strategy}s strategy';
      cycleEl.textContent = pattern
        .replace('{poll}', String(pollMs / 1000))
        .replace('{strategy}', String(health.tick_seconds));
    }
    // Local SQLite transport vs Exchange WS are separate surfaces.
    const xs = health.exchange_stream || {};
    const xsStatus = (xs.status || health.exchange_stream_status || 'disabled').toLowerCase();
    const xsLabel = {
      live: 'Live',
      degraded: 'Degraded',
      stale: 'Stale',
      connecting: 'Connecting',
      disabled: 'Disabled'
    }[xsStatus] || xsStatus;
    const tokens = xs.subscribed_token_count != null ? xs.subscribed_token_count : '—';
    const rest = xs.rest_fallback_active ? 'REST on' : 'REST off';
    if (connEl) {
      const local = connEl.dataset.conn || 'live';
      connEl.title =
        'Local SQLite: ' + local +
        ' | Exchange WS: ' + xsLabel +
        ' | assets=' + tokens +
        ' | ' + rest;
    }
    let xsEl = root.querySelector('[data-stream-exchange-ws]');
    if (!xsEl) {
      xsEl = document.createElement('span');
      xsEl.dataset.streamExchangeWs = '1';
      xsEl.className = 'stream-exchange-ws mono';
      if (connEl && connEl.parentNode) connEl.parentNode.insertBefore(xsEl, connEl.nextSibling);
      else root.appendChild(xsEl);
    }
    xsEl.textContent = 'Exchange WS ' + xsLabel + ' · ' + tokens + ' assets · ' + rest;
    const weather = health.open_meteo_usage || {};
    const weatherEl = document.querySelector('[data-open-meteo-usage]');
    if (weatherEl) {
      const units = Number(weather.estimated_units || 0);
      const requests = Number(weather.network_requests || 0);
      const cacheHits = Number(weather.cache_hits || 0);
      const rateLimits = Number(weather.responses_429 || 0);
      const cooldownSkips = Number(weather.cooldown_skips || 0);
      weatherEl.innerHTML = 'Open-Meteo <strong>' + units + '/10k</strong> · 429 ' + rateLimits;
      weatherEl.title =
        'UTC day · network requests=' + requests + ' · cache hits=' + cacheHits +
        ' · local cooldown skips=' + cooldownSkips;
      weatherEl.classList.toggle('summary-warn', units >= 8000 && units < 9000);
      weatherEl.classList.toggle('summary-bad', units >= 9000);
    }
  }

  function queryUrl() {
    const params = new URLSearchParams();
    params.set('after_decision_id', String(cursors.after_decision_id || 0));
    params.set('after_fill_id', String(cursors.after_fill_id || 0));
    params.set('after_intent_id', String(cursors.after_intent_id || 0));
    params.set('after_attempt_id', String(cursors.after_attempt_id || 0));
    params.set('after_analysis_id', String(cursors.after_analysis_id || 0));
    return '/app/stream?' + params.toString();
  }

  async function pollOnce() {
    if (inFlight) return;
    inFlight = true;
    if (Date.now() - lastOkAt > staleMs) setConn('stale');
    else if (connEl && connEl.dataset.conn === 'stale') setConn('reconnecting');
    try {
      const res = await fetch(queryUrl(), {
        method: 'GET',
        headers: { 'Accept': 'application/json' },
        credentials: 'same-origin',
        cache: 'no-store',
      });
      if (!res.ok) throw new Error('stream http ' + res.status);
      const data = await res.json();
      if (data && data.cursors) {
        cursors = Object.assign({}, cursors, data.cursors);
        root.dataset.cursors = JSON.stringify(cursors);
      }
      let seriesChanged = false;
      // Order: lifecycle first so fills/intents appear even without decisions.
      if (data && data.order_intents && data.order_intents.length) {
        appendEvents('intent', data.order_intents);
      }
      if (data && data.order_attempts && data.order_attempts.length) {
        appendEvents('attempt', data.order_attempts);
      }
      if (data && data.fills && data.fills.length) {
        appendEvents('fill', data.fills);
      }
      if (data && data.decisions && data.decisions.length) {
        seriesChanged = appendEvents('decision', data.decisions) > 0;
      }
      // analyses retained in payload for future panels; never charted here.
      applyHealth(data && data.health);
      if (seriesChanged) redrawChart();
      lastOkAt = Date.now();
      setConn('live');
    } catch (_) {
      if (Date.now() - lastOkAt > staleMs) setConn('stale');
      else setConn('reconnecting');
    } finally {
      inFlight = false;
    }
  }

  function schedule() {
    timer = window.setTimeout(async function () {
      await pollOnce();
      schedule();
    }, pollMs);
  }

  setConn('reconnecting');
  // Immediate first poll so lifecycle rows appear without waiting a full interval.
  pollOnce().finally(schedule);

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') pollOnce();
  });
  window.addEventListener('beforeunload', function () {
    if (timer) window.clearTimeout(timer);
  });
})();
</script>
"""
