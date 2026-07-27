from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from polymarket_weather_arb.adapters.polymarket.client import GammaPolymarketClient
from polymarket_weather_arb.adapters.weather.open_meteo import OpenMeteoProvider
from polymarket_weather_arb.config import Settings, load_settings
from polymarket_weather_arb.domain.rules import ResolutionRule
from polymarket_weather_arb.profiles import get_profile, settings_for_profile
from polymarket_weather_arb.services.market_workflow_service import MarketWorkflowService
from polymarket_weather_arb.services.trading_service import age_seconds
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository

console = Console()


def _settings() -> Settings:
    return load_settings()


def _database(settings: Settings) -> Database:
    return Database(settings.database_path)


def _repo(settings: Settings):
    database = _database(settings)
    connection = database.connect()
    return database, connection, Repository(connection)


def _profile_or_exit(name: str):
    try:
        return get_profile(name)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _workflow(settings: Settings, repository: Repository) -> MarketWorkflowService:
    return MarketWorkflowService(
        settings,
        repository,
        weather_provider_factory=OpenMeteoProvider,
        polymarket_client_factory=GammaPolymarketClient,
    )


class DashboardNotifier:
    def __init__(self, module, *, notify_force: bool) -> None:
        self.module = module
        self.notify_force = notify_force
        self.payloads: list[dict[str, object]] = []

    def __call__(self, payload: dict[str, object]) -> None:
        self.payloads.append({**payload, "notify_force": self.notify_force})

    def flush(self) -> None:
        pending, self.payloads = self.payloads, []
        for payload in pending:
            self.module.notify_daemon_payload(payload, force=self.notify_force)


def _dashboard_notifier(*, notify_force: bool) -> DashboardNotifier:
    import importlib.util

    script_path = Path(__file__).resolve().parents[3] / "scripts" / "notify_dashboard.py"
    spec = importlib.util.spec_from_file_location("polymarket_notify_dashboard", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load notify_dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return DashboardNotifier(module, notify_force=notify_force)


def _telegram_notifier(settings: Settings | None = None):
    from polymarket_weather_arb.services.telegram_notifier import TelegramNotifier

    return TelegramNotifier(settings or _settings())


def _build_daemon_notifier(
    *,
    settings: Settings,
    notify_dashboard: bool,
    notify_telegram: bool,
    notify_force: bool,
):
    """Compose explicitly requested legacy-daemon notifiers."""
    from polymarket_weather_arb.services.telegram_notifier import FanoutNotifier

    parts: list[object] = []
    if notify_dashboard:
        parts.append(_dashboard_notifier(notify_force=notify_force))
    if notify_telegram:
        if not settings.telegram_notify_ready():
            raise typer.BadParameter(
                "Telegram notify requested but not ready. Set TELEGRAM_NOTIFY_ENABLED=true, "
                "TELEGRAM_BOT_TOKEN, and TELEGRAM_CHAT_ID"
            )
        parts.append(_telegram_notifier(settings))
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return FanoutNotifier(*parts)  # type: ignore[arg-type]


def _print_daemon_result(result) -> None:
    console.print(
        json.dumps(
            {
                "discovered": result.discovered,
                "proposed_action_id": result.proposed_action_id,
                "proposed_kind": result.proposed_kind,
                "auto_executed_action_ids": result.auto_executed_action_ids,
                "auto_live_executed_action_ids": result.auto_live_executed_action_ids,
                "skipped_live_action_ids": result.skipped_live_action_ids,
                "risk_status": result.risk_status,
                "risk_anomalies": result.risk_anomalies,
                "reconciliation_status": result.reconciliation_status,
                "reconciliation_fresh": result.reconciliation_fresh,
                "open_orders_count": result.open_orders_count,
                "positions_count": result.positions_count,
                "nonzero_positions_count": result.nonzero_positions_count,
                "fills_count": result.fills_count,
                "notifications_sent": result.notifications_sent,
                "notes": result.notes,
                "auto_exit_armed": getattr(result, "auto_exit_armed", False),
                "auto_exit_executed": getattr(result, "auto_exit_executed", 0),
                "auto_exit_attempted": getattr(result, "auto_exit_attempted", 0),
                "auto_exit_skipped": getattr(result, "auto_exit_skipped", []),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _print_operator_readiness(settings: Settings, profile, repository: Repository) -> None:
    console.print(f"Profile: {profile.name} ({profile.role})")
    effective = settings_for_profile(settings, profile)
    console.print(
        f"Risk caps: order={effective.max_order_usdc}, day={effective.max_daily_usdc}, market={effective.max_market_usdc}"
    )
    console.print(f"Reconciliation fresh: {_has_fresh_reconciliation(repository)}")
    try:
        effective.ensure_live_trading_ready()
        console.print("Live credentials: configured")
    except ValueError as exc:
        console.print(f"Live credentials: {exc}")


def _print_action(action) -> None:
    console.print(json.dumps(_action_payload(action), ensure_ascii=False, indent=2))


def _print_action_table(actions) -> None:
    table = Table(
        "Action",
        "Status",
        "Kind",
        "Market",
        "Updated",
        "Expires",
        "Approved By",
        "Duration",
        "Note",
    )
    for action in actions:
        note = action["failure_reason"] or action["result_summary"] or action["reason"] or ""
        table.add_row(
            action["id"],
            action["status"],
            action["kind"],
            _market_label(action),
            action["updated_at"] or "-",
            action["expires_at"] or "-",
            action["approved_by"] or "-",
            _duration_label(_row_value(action, "execution_duration_ms")),
            _short_note(note),
        )
    console.print(table)


def _print_counts_table(title: str, rows) -> None:
    table = Table(title, "Count")
    for row in rows:
        table.add_row(row["status"], str(row["count"]))
    console.print(table)


def _print_overrides_table(rows) -> None:
    table = Table(
        "Market",
        "Profile",
        "Min Edge",
        "Max Order",
        "Max Daily",
        "Max Market",
        "Live Auto",
        "Notes",
        "Updated",
    )
    for row in rows:
        table.add_row(
            row["market_id"],
            row["profile"],
            _dash(row["min_edge"]),
            _dash(row["max_order_usdc"]),
            _dash(row["max_daily_usdc"]),
            _dash(row["max_market_usdc"]),
            _bool_label(row["live_auto_enabled"]),
            row["notes"] or "-",
            row["updated_at"],
        )
    console.print(table)


def _print_action_detail(action, events) -> None:
    _print_action(action)
    detail = Table("Field", "Value")
    for label, value in [
        ("market_title", _row_value(action, "market_title")),
        ("requested_by", action["requested_by"]),
        ("approved_at", action["approved_at"]),
        ("rejected_at", action["rejected_at"]),
        ("claimed_at", action["claimed_at"]),
        ("executed_at", action["executed_at"]),
        ("failed_at", action["failed_at"]),
        ("execution_started_at", _row_value(action, "execution_started_at")),
        ("execution_finished_at", _row_value(action, "execution_finished_at")),
        ("execution_duration_ms", _row_value(action, "execution_duration_ms")),
        ("execution_argv", _row_value(action, "execution_argv")),
    ]:
        detail.add_row(label, str(value) if value is not None else "-")
    console.print(detail)
    _print_audit_timeline(events)


def _print_audit_timeline(events) -> None:
    table = Table("Time", "Event", "Actor", "Details")
    for event in events:
        table.add_row(
            event["created_at"],
            event["event"],
            event["actor"] or "-",
            _short_note(event["details"], max_chars=120),
        )
    console.print(table)


def _print_proposal_hints(action) -> None:
    console.print("Next steps:")
    console.print(f"/wufu action-status action-id:{action['id']}")
    console.print(f"/wufu action-approve action-id:{action['id']}")
    console.print(
        "or locally: uv run polymarket-weather operator approve-latest --actor local-operator"
    )
    console.print("After approval, execute locally:")
    console.print("uv run polymarket-weather operator run-approved --limit 1")


def _short_note(value: str | None, max_chars: int = 80) -> str:
    if not value:
        return "-"
    normalized = str(value).replace("\n", " ").strip()
    return normalized if len(normalized) <= max_chars else f"{normalized[: max_chars - 1]}…"


def _duration_label(value) -> str:
    return "-" if value is None else f"{value} ms"


def _dash(value) -> str:
    return "-" if value is None else str(value)


def _bool_label(value) -> str:
    if value is None:
        return "-"
    return "yes" if bool(value) else "no"


def _row_value(row, key: str):
    return row[key] if key in row.keys() else None


def _market_label(action) -> str:
    title = _row_value(action, "market_title")
    return (
        action["market_id"]
        if not title
        else f"{action['market_id']} ({_short_note(title, max_chars=40)})"
    )


def _action_payload(action) -> dict[str, object | None]:
    return {
        "id": action["id"],
        "kind": action["kind"],
        "market_id": action["market_id"],
        "status": action["status"],
        "reason": action["reason"],
        "command_preview": action["command_preview"],
        "expires_at": action["expires_at"],
        "approved_by": action["approved_by"],
        "rejected_by": action["rejected_by"],
        "return_code": action["return_code"],
        "result_summary": action["result_summary"],
        "failure_reason": action["failure_reason"],
        "created_at": action["created_at"],
        "updated_at": action["updated_at"],
        "requested_by": action["requested_by"],
        "approved_at": action["approved_at"],
        "rejected_at": action["rejected_at"],
        "claimed_at": action["claimed_at"],
        "executed_at": action["executed_at"],
        "failed_at": action["failed_at"],
        "execution_started_at": _row_value(action, "execution_started_at"),
        "execution_finished_at": _row_value(action, "execution_finished_at"),
        "execution_duration_ms": _row_value(action, "execution_duration_ms"),
        "execution_argv": _row_value(action, "execution_argv"),
    }


def _risk_context(
    repository: Repository,
    market_id: str,
    today: str,
    snapshot_row,
    forecast_row,
    rule: ResolutionRule,
    *,
    reconciliation_fresh: bool,
):
    from polymarket_weather_arb.domain.risk import RiskContext

    return RiskContext(
        daily_live_notional=repository.daily_order_notional(today),
        market_live_exposure=repository.market_exposure(market_id),
        order_book_age_seconds=age_seconds(snapshot_row["fetched_at"]) if snapshot_row else None,
        forecast_age_seconds=age_seconds(forecast_row["fetched_at"]) if forecast_row else None,
        rule_tradable=rule.tradable,
        unsupported_variable=rule.variable is None,
        reconciliation_fresh=reconciliation_fresh,
    )


def _has_fresh_reconciliation(repository: Repository) -> bool:
    row = repository.latest_successful_reconciliation()
    if row is None:
        return False
    created_at = datetime.fromisoformat(row["created_at"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created_at).total_seconds() <= 300


def _analysis_from_row(row):
    from polymarket_weather_arb.domain.pricing import Analysis

    return Analysis(
        market_id=row["market_id"],
        model_version=row["model_version"],
        fair_lower=Decimal(str(row["fair_lower"])),
        fair_upper=Decimal(str(row["fair_upper"])),
        reference_price=Decimal(str(row["reference_price"]))
        if row["reference_price"] is not None
        else None,
        edge=Decimal(str(row["edge"])),
        side=row["side"],
        decision=row["decision"],
        reasons=json.loads(row["reasons"]),
        created_at=datetime.fromisoformat(row["created_at"]).replace(tzinfo=timezone.utc),
    )
