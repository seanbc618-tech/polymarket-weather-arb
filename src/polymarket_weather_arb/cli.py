from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from polymarket_weather_arb.adapters.polymarket.client import GammaPolymarketClient
from polymarket_weather_arb.adapters.weather.noaa import NoaaProvider
from polymarket_weather_arb.cli_commands.automation import automation_app
from polymarket_weather_arb.cli_commands.common import (
    _analysis_from_row,
    _database,
    _has_fresh_reconciliation,
    _profile_or_exit,
    _repo,
    _risk_context,
    _settings,
    _workflow,
    console,
)
from polymarket_weather_arb.cli_commands.fixtures import _import_market_json_command, fixtures_app
from polymarket_weather_arb.cli_commands.autopilot import autopilot_app
from polymarket_weather_arb.cli_commands.operator import operator_app
from polymarket_weather_arb.cli_commands.profiles import profiles_app
from polymarket_weather_arb.dashboard import serve_dashboard
from polymarket_weather_arb.domain.rules import parse_resolution_rule
from polymarket_weather_arb.profiles import settings_for_profile
from polymarket_weather_arb.services.backup_service import DatabaseBackupService
from polymarket_weather_arb.services.calibration_service import CalibrationService
from polymarket_weather_arb.services.compliance_service import ComplianceService
from polymarket_weather_arb.services.discovery_service import DiscoveryService
from polymarket_weather_arb.services.live_readiness_service import LiveReadinessService
from polymarket_weather_arb.services.reconciliation_service import ReconciliationService
from polymarket_weather_arb.services.settlement_service import SettlementService
from polymarket_weather_arb.services.trading_service import TradingService

app = typer.Typer(help="Polymarket weather market research and trading MVP.")
app.add_typer(fixtures_app, name="fixtures")
app.add_typer(automation_app, name="automation")
app.add_typer(autopilot_app, name="autopilot")
app.add_typer(operator_app, name="operator")
app.add_typer(profiles_app, name="profiles")


@app.command("init-db")
def init_db() -> None:
    settings = _settings()
    _database(settings).init_schema()
    console.print(f"Initialized database at {settings.database_path}")


@app.command()
def doctor(
    live: bool = typer.Option(False, "--live", help="Also validate live-trading credentials."),
) -> None:
    settings = _settings()
    problems = []
    if settings.max_order_usdc > Decimal("25"):
        problems.append("MAX_ORDER_USDC is above hard cap; runtime will clamp to 25")
    if settings.max_daily_usdc > Decimal("100"):
        problems.append("MAX_DAILY_USDC is above hard cap; runtime will clamp to 100")
    if settings.max_market_usdc > Decimal("50"):
        problems.append("MAX_MARKET_USDC is above hard cap; runtime will clamp to 50")
    compliance_decision = None
    try:
        if live:
            settings.ensure_live_trading_ready()
            compliance_decision = ComplianceService(settings).check_live_allowed()
            if not compliance_decision.ok:
                problems.append(compliance_decision.reason)
    except ValueError as exc:
        problems.append(str(exc))
    _database(settings).init_schema()
    console.print(f"Database: {settings.database_path}")
    console.print(f"Weather provider: {settings.weather_provider}")
    console.print(
        f"Risk caps: order={settings.max_order_usdc}, day={settings.max_daily_usdc}, market={settings.max_market_usdc}"
    )
    console.print(
        f"Live credentials readiness: {'configured' if settings.polymarket_private_key and settings.polymarket_funder else 'missing'}"
    )
    if live:
        if compliance_decision is None:
            compliance_decision = ComplianceService(settings).check_live_allowed()
            if not compliance_decision.ok and compliance_decision.reason not in problems:
                problems.append(compliance_decision.reason)
        console.print(
            f"Compliance readiness: {compliance_decision.status} - {compliance_decision.reason}"
        )
    if problems:
        for problem in problems:
            console.print(f"[yellow]WARN[/yellow] {problem}")
    else:
        console.print("[green]OK[/green]")


@app.command("backup-db")
def backup_db(
    output_dir: Annotated[Path, typer.Option("--output-dir", file_okay=False)] = Path("backups"),
    retention: Annotated[int, typer.Option("--retention", min=1)] = 14,
) -> None:
    settings = _settings()
    result = DatabaseBackupService(settings.database_path).backup(
        output_dir=output_dir, retention=retention
    )
    console.print(f"Backup: {result.destination}")
    console.print(f"Retained: {result.retained}")
    if result.deleted:
        console.print(f"Pruned: {len(result.deleted)}")


@app.command("dashboard")
def dashboard(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1024, max=65535)] = 8765,
) -> None:
    settings = _settings()
    serve_dashboard(settings, host=host, port=port)


@app.command("live-readiness")
def live_readiness(
    check_exchange: Annotated[bool, typer.Option("--check-exchange/--no-check-exchange")] = True,
) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        client = GammaPolymarketClient(settings) if check_exchange else None
        checks = LiveReadinessService(settings, repository, client=client).check(
            check_exchange=check_exchange
        )
        connection.commit()
    finally:
        connection.close()
    table = Table("Check", "OK", "Status", "Detail")
    for check in checks:
        table.add_row(check.name, "yes" if check.ok else "no", check.status, check.detail)
    console.print(table)


@app.command("calibration-report")
def calibration_report() -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        report = CalibrationService(repository).report()
    finally:
        connection.close()
    table = Table(
        "Model",
        "Provider",
        "Signals",
        "Resolved",
        "Brier",
        "Hit Rate",
        "Avg Edge",
        "Status",
    )
    for group in report.groups:
        table.add_row(
            group.model_version,
            group.forecast_provider,
            str(group.total_signals),
            str(group.resolved_signals),
            _calibration_decimal(group.brier_score),
            _calibration_percent(group.hit_rate),
            _calibration_decimal(group.average_edge),
            group.status,
        )
    console.print(table)
    for group in report.groups:
        console.print(
            "Calibration "
            f"model={group.model_version} "
            f"provider={group.forecast_provider} "
            f"signals={group.total_signals} "
            f"resolved={group.resolved_signals} "
            f"Brier={_calibration_decimal(group.brier_score)} "
            f"hit={_calibration_percent(group.hit_rate)} "
            f"status={group.status}"
        )


@app.command("calibration-settle")
def calibration_settle(
    market: Annotated[str, typer.Option("--market")],
    outcome: Annotated[str, typer.Option("--outcome", help="yes or no")],
    settlement_value: Annotated[str | None, typer.Option("--settlement-value")] = None,
    settlement_source: Annotated[str | None, typer.Option("--settlement-source")] = None,
) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        updated = repository.settle_model_signals_for_market(
            market,
            resolved_outcome=outcome,
            settlement_value=Decimal(settlement_value) if settlement_value is not None else None,
            settlement_source=settlement_source,
        )
        connection.commit()
    finally:
        connection.close()
    console.print(f"Updated {updated} signal{'s' if updated != 1 else ''}")


@app.command("settlement-backfill")
def settlement_backfill(
    market: Annotated[str, typer.Option("--market")],
    preview: Annotated[
        bool, typer.Option("--preview", help="Preview observation without saving")
    ] = False,
) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        service = SettlementService(repository, NoaaProvider())
        if preview:
            result = service.preview_market(market)
            console.print(
                "Preview settlement "
                f"market={result.market_id} "
                f"station={result.station or '-'} "
                f"variable={result.variable or '-'} "
                f"value={result.observed_value}{result.unit} "
                f"quality={result.quality_status or '-'} "
                f"would_resolve={result.would_resolve_outcome} "
                f"source={result.settlement_source} "
                f"operator={result.rule_operator or '-'} "
                f"threshold={result.rule_threshold or '-'}"
            )
            for w in result.warnings:
                console.print(f"[yellow]WARNING: {w}[/yellow]")
        else:
            result = service.backfill_market(market)
            connection.commit()
            console.print(
                "Backfilled settlement "
                f"market={result.market_id} "
                f"outcome={result.resolved_outcome} "
                f"value={result.observation_value}{result.observation_unit} "
                f"source={result.settlement_source} "
                f"updated={result.updated_signals}"
            )
            for w in result.warnings:
                console.print(f"[yellow]WARNING: {w}[/yellow]")
    finally:
        connection.close()


@app.command("discover-markets")
def discover_markets(
    limit: int = typer.Option(100, min=1, max=500),
    pages: int = typer.Option(1, min=1, max=50),
    include_unsupported: bool = typer.Option(
        False,
        "--include-unsupported",
        help="Store weather-related markets that this analyzer cannot handle yet.",
    ),
) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        service = DiscoveryService(GammaPolymarketClient(settings), repository)
        count = service.discover(limit=limit, pages=pages, include_unsupported=include_unsupported)
        connection.commit()
    finally:
        connection.close()
    console.print(f"Discovered {count} weather markets")


@app.command("discover-weather-events")
def discover_weather_events(
    include_unsupported: bool = typer.Option(
        False,
        "--include-unsupported",
        help="Store weather-related markets that this analyzer cannot handle yet.",
    ),
    limit: int = typer.Option(0, min=0, help="Limit number of weather events to fetch (0 = all)."),
) -> None:
    """Discover weather markets from Polymarket's weather event page."""
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        service = DiscoveryService(GammaPolymarketClient(settings), repository)
        count = service.discover_weather_events(
            include_unsupported=include_unsupported, limit=limit
        )
        connection.commit()
    finally:
        connection.close()
    console.print(f"Discovered {count} weather markets from weather events")


@app.command()
def markets(limit: Annotated[int, typer.Option(min=1, max=200)] = 50) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        rows = repository.list_weather_markets()[:limit]
        table = Table("ID", "Title", "Tradable", "Reason", "Bid", "Ask")
        for row in rows:
            rule = repository.get_resolution_rule(row["id"])
            snapshot = repository.latest_market_snapshot(row["id"])
            table.add_row(
                row["id"],
                row["title"],
                str(bool(rule["tradable"])) if rule else "unknown",
                (rule["rejection_reason"] if rule else None) or "ok",
                str(snapshot["best_bid"]) if snapshot else "-",
                str(snapshot["best_ask"]) if snapshot else "-",
            )
    finally:
        connection.close()
    console.print(table)


@app.command()
def candidates(
    limit: Annotated[int, typer.Option(min=1, max=200)] = 50,
    status: Annotated[str | None, typer.Option("--status")] = None,
) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        rows = repository.list_candidates(limit=limit, status=status)
    finally:
        connection.close()
    table = Table("Market", "Status", "Tradable", "Reason", "Bid", "Ask", "Updated")
    for row in rows:
        table.add_row(
            row["market_id"],
            row["status"],
            str(bool(row["tradable"])),
            row["rejection_reason"] or "ok",
            str(row["best_bid"]) if row["best_bid"] is not None else "-",
            str(row["best_ask"]) if row["best_ask"] is not None else "-",
            row["updated_at"],
        )
    console.print(table)


@app.command("candidate-mark")
def candidate_mark(
    market: Annotated[str, typer.Option("--market")],
    status: Annotated[str, typer.Option("--status")],
    notes: Annotated[str | None, typer.Option("--notes")] = None,
) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        if repository.get_market(market) is None:
            raise typer.BadParameter(f"unknown market: {market}")
        repository.mark_candidate(market, status, notes)
        connection.commit()
    finally:
        connection.close()
    console.print(f"Marked candidate {market}: {status}")


@app.command("inspect-market")
def inspect_market(market_id: str) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        result = _workflow(settings, repository).inspect_market(market_id)
        snapshot = repository.latest_market_snapshot(market_id)
        connection.commit()
    finally:
        connection.close()
    console.print(result.summary)
    for detail in result.details:
        console.print(f"- {detail}")
    if snapshot:
        console.print(
            f"Quote: bid={snapshot['best_bid']} ask={snapshot['best_ask']} spread={snapshot['spread']} fetched={snapshot['fetched_at']}"
        )


@app.command("refresh-weather")
def refresh_weather(market: Annotated[str, typer.Option("--market")]) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        result = _workflow(settings, repository).refresh_weather(market)
        connection.commit()
    finally:
        connection.close()
    console.print(result.summary)
    for detail in result.details:
        console.print(f"- {detail}")


@app.command()
def analyze(market: Annotated[str, typer.Option("--market")]) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        if repository.latest_pricing_snapshot(market) is None:
            raise typer.BadParameter(f"market has no order book snapshot: {market}")
        result = _workflow(settings, repository).analyze(market)
        connection.commit()
    finally:
        connection.close()
    console.print(result.summary)
    for detail in result.details:
        console.print(f"- {detail}")


@app.command()
def trade(
    market: Annotated[str, typer.Option("--market")],
    dry_run: bool = typer.Option(False, "--dry-run", help="Record intent without live submission."),
    profile: Annotated[str, typer.Option("--profile")] = "balanced",
) -> None:
    base_settings = _settings()
    selected_profile = _profile_or_exit(profile)
    settings = settings_for_profile(base_settings, selected_profile)
    if not dry_run:
        try:
            settings.ensure_live_trading_ready()
            ComplianceService(settings).ensure_live_allowed()
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        if dry_run:
            result = _workflow(settings, repository).dry_run_trade(market)
            connection.commit()
            intent_id = None
            reasons = result.details
        else:
            row = repository.get_market(market)
            if row is None:
                raise typer.BadParameter(f"unknown market: {market}")
            if row["module_id"] != "weather":
                raise typer.BadParameter(
                    f"live trade is not enabled for module: {row['module_id']}"
                )
            analysis_row = repository.latest_analysis(market)
            if analysis_row is None:
                raise typer.BadParameter(f"market has no analysis: {market}")
            snapshot_row = repository.latest_pricing_snapshot(
                market,
                side=str(analysis_row["side"] or ""),
            )
            forecast_row = repository.latest_forecast(market)
            today = datetime.now(timezone.utc).date().isoformat()
            rule = parse_resolution_rule(row["title"], row["description"])
            analysis = _analysis_from_row(analysis_row)
            reconciliation_fresh = _has_fresh_reconciliation(repository)
            context = _risk_context(
                repository,
                market,
                today,
                snapshot_row,
                forecast_row,
                rule,
                reconciliation_fresh=reconciliation_fresh,
            )
            service = TradingService(settings, GammaPolymarketClient(settings), repository)
            intent_id, reasons = service.trade(
                analysis=analysis,
                yes_token_id=row["yes_token_id"],
                no_token_id=row["no_token_id"],
                context=context,
                dry_run=False,
                source_grade=_forecast_source_grade(forecast_row),
                on_submitted=lambda _intent_id: connection.commit(),
            )
            connection.commit()
    finally:
        connection.close()
    console.print(f"Order intent: {intent_id or 'none'}")
    for reason in reasons:
        console.print(f"- {reason}")
    if not dry_run and "live order submitted" not in reasons:
        raise typer.Exit(code=2)


def _forecast_source_grade(forecast_row) -> str:
    from polymarket_weather_arb.domain.source_grade import (
        UNKNOWN,
        extract_forecast_source_grade,
    )

    if forecast_row is None:
        return UNKNOWN
    try:
        raw_payload = forecast_row.get("raw_payload")
        if not raw_payload:
            return UNKNOWN
        raw = json.loads(raw_payload)
    except (json.JSONDecodeError, KeyError, AttributeError, TypeError):
        return UNKNOWN
    return extract_forecast_source_grade(raw)


def _calibration_decimal(value: Decimal | None) -> str:
    if value is None:
        return "-"
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _calibration_percent(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return f"{(value * Decimal('100')).quantize(Decimal('0.1'))}%"


@app.command()
def orders(limit: int = typer.Option(20, min=1, max=100)) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        rows = repository.list_recent_order_intents(limit=limit)
    finally:
        connection.close()
    table = Table("ID", "Market", "Side", "Price", "Size", "Notional", "Dry Run", "Status")
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["market_id"],
            row["side"],
            str(row["limit_price"]),
            str(row["size"]),
            str(row["notional"]),
            str(bool(row["dry_run"])),
            row["status"],
        )
    console.print(table)


@app.command()
def reconcile() -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        result = ReconciliationService(GammaPolymarketClient(settings), repository).reconcile()
        connection.commit()
    finally:
        connection.close()
    console.print(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("fixtures-import-market-json")
def fixtures_import_market_json_alias(
    input_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    output_dir: Annotated[
        Path, typer.Option("--output-dir", help="Directory for generated market fixtures.")
    ] = Path("fixtures/markets"),
) -> None:
    _import_market_json_command(input_path, output_dir)


@app.command("risk-report")
def risk_report() -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        database.init_schema()
        daily = repository.daily_order_notional(today)
        markets = repository.list_weather_markets()
        exposures = [(row["id"], repository.market_exposure(row["id"])) for row in markets]
    finally:
        connection.close()
    console.print(f"Daily live notional: {daily} / {settings.max_daily_usdc}")
    table = Table("Market", "Exposure")
    for market_id, exposure in exposures:
        table.add_row(market_id, str(exposure))
    console.print(table)
