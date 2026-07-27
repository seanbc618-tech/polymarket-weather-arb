from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from polymarket_weather_arb.adapters.polymarket.client import GammaPolymarketClient
from polymarket_weather_arb.dashboard import serve_dashboard
from polymarket_weather_arb.cli_commands.common import (
    _bool_label,
    _dash,
    _build_daemon_notifier,
    _database,
    _print_action,
    _print_action_detail,
    _print_action_table,
    _print_audit_timeline,
    _print_counts_table,
    _print_daemon_result,
    _print_operator_readiness,
    _print_overrides_table,
    _print_proposal_hints,
    _profile_or_exit,
    _repo,
    _settings,
    console,
)
from polymarket_weather_arb.profiles import profile_summary
from polymarket_weather_arb.services.automation_service import AutomationService
from polymarket_weather_arb.services.circuit_breaker_service import (
    CircuitBreakerService,
    live_execution_blocked,
)
from polymarket_weather_arb.services.exit_guardian_service import ExitGuardianService
from polymarket_weather_arb.services.fixture_service import load_market_fixture
from polymarket_weather_arb.services.live_monitor_service import build_live_monitor_snapshot
from polymarket_weather_arb.services.operator_daemon import OperatorDaemon
from polymarket_weather_arb.services.order_lifecycle_service import OrderLifecycleService
from polymarket_weather_arb.services.reconciliation_service import ReconciliationService
from polymarket_weather_arb.services.resolution_audit_service import ResolutionAuditService
from polymarket_weather_arb.domain.execution import OrderAttempt, build_order_intent
from polymarket_weather_arb.domain.risk import ProposedOrder

operator_app = typer.Typer(help="Guided local operator console.")
cb_app = typer.Typer(help="Manage the global resolution circuit breaker.")
operator_app.add_typer(cb_app, name="circuit-breaker")

_OPENISH_ORDER_STATUSES = {"open", "live", "unmatched", "active", "pending"}


def _operator_go_impl(
    *,
    profile: str,
    dry_run: bool,
    propose_only: bool,
    approve_latest: bool,
    run_approved: bool,
    yes: bool,
) -> None:
    settings = _settings()
    selected_profile = _profile_or_exit(profile)
    if dry_run:
        selected_kind = "dry_run"
    else:
        selected_kind = selected_profile.normalized_action_kind()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        service = AutomationService(repository)
        suggestion = service.suggest_next_action()
        _print_operator_readiness(settings, selected_profile, repository)
        console.print(f"Next: {suggestion.label}")
        console.print(f"Reason: {suggestion.reason}")
        if approve_latest:
            pending = repository.latest_action_by_status("pending")
            if pending is None:
                console.print("No pending action found.")
            else:
                action = service.approve(pending["id"], f"operator:{selected_profile.name}")
                connection.commit()
                _print_action(action)
                console.print("Next: uv run polymarket-weather operator run-approved --limit 1")
            return
        if run_approved:
            actions = service.execute_approved(limit=1)
            connection.commit()
            if actions:
                _print_action_table(actions)
            else:
                console.print("No approved actions waiting.")
            return
        if propose_only or (yes and suggestion.label == "propose-next"):
            action = service.propose_next(
                kind=selected_kind,
                reason=f"operator go with {selected_profile.name} profile",
                ttl_minutes=selected_profile.action_ttl_minutes,
                requested_by=f"operator-go:{selected_profile.name}",
            )
            repository.append_automation_audit_event(
                action["id"], "profile_selected", "operator-go", profile_summary(selected_profile)
            )
            connection.commit()
            _print_action(action)
            _print_proposal_hints(action)
            return
    finally:
        connection.close()
    console.print("Run:")
    console.print(suggestion.command)
    if suggestion.label == "propose-next":
        console.print(
            f"Or propose now: uv run polymarket-weather operator go --profile {selected_profile.name} --propose-only"
        )


def _print_live_monitor_snapshot(snapshot) -> None:
    settings = _settings()
    from polymarket_weather_arb.services.auto_exit_service import AutoExitService

    auto_exit = AutoExitService.status_snapshot(
        settings=settings,
        profile_name=snapshot.profile,
        allow_auto_exit=False,
    )
    console.print(
        f"Live monitor: profile={snapshot.profile} allow_live_auto={snapshot.allow_live_auto} "
        f"risk={snapshot.risk_status} reconciliation_fresh={snapshot.reconciliation_fresh}"
    )
    console.print(
        f"AUTO EXIT: enabled={auto_exit['auto_exit_enabled']} "
        f"(status only; no browser enable — requires env + daemon --allow-auto-exit + micro-live)"
    )
    console.print(
        f"orders={snapshot.open_orders_count} positions={snapshot.positions_count} "
        f"nonzero_positions={snapshot.nonzero_positions_count} "
        f"pending_live_actions={len(snapshot.pending_live_actions)}"
    )
    if snapshot.blockers:
        console.print("Blockers:")
        for blocker in snapshot.blockers:
            console.print(f"- {blocker}")
    else:
        console.print("Blockers: none")
    for action in snapshot.pending_live_actions:
        failed = [gate.reason for gate in action.gates if not gate.ok]
        status = "ready" if action.can_auto_execute else "blocked"
        console.print(f"Action {action.action_id} market={action.market_id} status={status}")
        for reason in failed:
            console.print(f"  - {reason}")


@operator_app.command("daemon")
def operator_daemon(
    profile: Annotated[str, typer.Option("--profile")] = "dry-run-demo",
    tick_seconds: Annotated[int, typer.Option("--tick-seconds", min=5)] = 300,
    max_ticks: Annotated[int | None, typer.Option("--max-ticks", min=1)] = None,
    once: Annotated[bool, typer.Option("--once")] = False,
    dry_run_only: Annotated[bool, typer.Option("--dry-run-only/--allow-profile-kind")] = True,
    no_discover: Annotated[bool, typer.Option("--no-discover")] = False,
    no_propose: Annotated[bool, typer.Option("--no-propose")] = False,
    no_auto_dry_run: Annotated[bool, typer.Option("--no-auto-dry-run")] = False,
    no_risk_guard: Annotated[bool, typer.Option("--no-risk-guard")] = False,
    include_reconciliation: Annotated[bool, typer.Option("--include-reconciliation")] = False,
    notify_dashboard: Annotated[bool, typer.Option("--notify-dashboard")] = False,
    notify_telegram: Annotated[
        bool,
        typer.Option(
            "--notify-telegram/--no-notify-telegram",
            help=(
                "Push legacy daemon events to Telegram. Must be explicit; "
                "/app owns automatic Telegram notifications."
            ),
        ),
    ] = False,
    notify_force: Annotated[bool, typer.Option("--notify-force")] = False,
    allow_live_auto: Annotated[bool, typer.Option("--allow-live-auto")] = False,
    live_market: Annotated[list[str] | None, typer.Option("--live-market")] = None,
    max_live_actions_per_tick: Annotated[
        int, typer.Option("--max-live-actions-per-tick", min=1, max=5)
    ] = 1,
    require_fresh_reconciliation: Annotated[
        bool, typer.Option("--require-fresh-reconciliation/--no-require-fresh-reconciliation")
    ] = True,
    block_live_on_positions: Annotated[
        bool, typer.Option("--block-live-on-positions/--allow-live-with-positions")
    ] = True,
    allow_auto_exit: Annotated[
        bool,
        typer.Option(
            "--allow-auto-exit/--no-allow-auto-exit",
            help=(
                "Also requires micro-live or full-live. AUTO_EXIT_ENABLED is required "
                "for micro-live; full-live always includes automatic exits. Default off."
            ),
        ),
    ] = False,
    full_auto: Annotated[
        bool,
        typer.Option(
            "--full-auto/--no-full-auto",
            help=(
                "Arm full-live full-auto: auto BUY (trade_live) + auto SELL (auto-exit). "
                "Requires --profile full-live and live credentials. "
                "Whitelist optional: omit --live-market / LIVE_MARKET_IDS to allow all "
                "local candidates. Default off."
            ),
        ),
    ] = False,
) -> None:
    settings = _settings()
    from polymarket_weather_arb.logging_config import setup_persistent_logging
    from pathlib import Path
    setup_persistent_logging(Path(settings.database_path).parent)
    selected_profile = _profile_or_exit(profile)
    notifier = _build_daemon_notifier(
        settings=settings,
        notify_dashboard=notify_dashboard,
        notify_telegram=notify_telegram,
        notify_force=notify_force,
    )
    database, connection, repository = _repo(settings)

    # Resolved per-session execution posture (defaults remain conservative).
    resolved_dry_run_only = dry_run_only
    resolved_allow_live_auto = allow_live_auto
    resolved_allow_auto_exit = allow_auto_exit
    resolved_block_positions = block_live_on_positions
    resolved_include_recon = include_reconciliation
    resolved_auto_dry_run = not no_auto_dry_run
    resolved_live_markets = set(live_market or [])
    resolved_max_live = max_live_actions_per_tick

    try:
        database.init_schema()
        if full_auto:
            from polymarket_weather_arb.services.full_auto_service import (
                arm_legacy_operator_live_overrides,
                describe_full_auto_plan,
                resolve_full_auto_plan,
            )

            try:
                plan = resolve_full_auto_plan(
                    settings=settings,
                    profile=selected_profile,
                    live_markets_cli=live_market,
                    max_live_actions_per_tick=max_live_actions_per_tick,
                )
            except ValueError as exc:
                raise typer.BadParameter(str(exc)) from exc

            resolved_dry_run_only = plan.dry_run_only
            resolved_allow_live_auto = plan.allow_live_auto
            resolved_allow_auto_exit = plan.allow_auto_exit
            resolved_block_positions = plan.block_live_on_positions
            resolved_include_recon = plan.include_reconciliation
            resolved_auto_dry_run = plan.auto_dry_run
            resolved_live_markets = set(plan.live_market_ids)
            resolved_max_live = plan.max_live_actions_per_tick
            resolved_whitelist_open = plan.live_whitelist_open

            armed = arm_legacy_operator_live_overrides(
                repository,
                market_ids=resolved_live_markets,
                profile_name=selected_profile.name,
                open_whitelist=plan.live_whitelist_open,
            )
            connection.commit()
            console.print("[bold red]FULL-AUTO FULL-LIVE ARMED[/bold red]")
            console.print(describe_full_auto_plan(plan))
            if armed:
                console.print(f"live_auto overrides armed: {armed}")
            else:
                console.print(
                    "[yellow]No overrides armed (whitelist ids missing from local DB); "
                    "run discover first if you use a restricted list.[/yellow]"
                )

        resolved_whitelist_open = False if not full_auto else resolved_whitelist_open

        daemon = OperatorDaemon(
            repository=repository,
            client=GammaPolymarketClient(settings),
            profile=selected_profile,
            settings=settings,
            dry_run_only=resolved_dry_run_only,
            notifier=notifier,
            notify_force=notify_force,
            allow_live_auto=resolved_allow_live_auto,
            live_market_ids=resolved_live_markets,
            max_live_actions_per_tick=resolved_max_live,
            require_fresh_reconciliation=require_fresh_reconciliation,
            block_live_on_positions=resolved_block_positions,
            allow_auto_exit=resolved_allow_auto_exit,
            live_whitelist_open=resolved_whitelist_open if full_auto else False,
        )
        if once:
            result = daemon.tick(
                discover=not no_discover,
                propose=not no_propose,
                auto_dry_run=resolved_auto_dry_run,
                risk_guard=not no_risk_guard,
                include_reconciliation=resolved_include_recon,
                auto_live=resolved_allow_live_auto,
            )
            connection.commit()
            if notifier:
                notifier.flush()
            _print_daemon_result(result)
            return
        ticks = []
        max_count = max_ticks
        tick_count = 0
        while max_count is None or tick_count < max_count:
            result = daemon.tick(
                discover=not no_discover,
                propose=not no_propose,
                auto_dry_run=resolved_auto_dry_run,
                risk_guard=not no_risk_guard,
                include_reconciliation=resolved_include_recon,
                auto_live=resolved_allow_live_auto,
            )
            ticks.append(result)
            tick_count += 1
            connection.commit()
            if notifier:
                notifier.flush()
            if max_count is not None and tick_count >= max_count:
                break
            import time

            time.sleep(tick_seconds)
        connection.commit()
    finally:
        connection.close()
    for result in ticks:
        _print_daemon_result(result)


@operator_app.command("go")
def operator_go(
    profile: Annotated[str, typer.Option("--profile")] = "balanced",
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    propose_only: Annotated[bool, typer.Option("--propose-only")] = False,
    approve_latest: Annotated[bool, typer.Option("--approve-latest")] = False,
    run_approved: Annotated[bool, typer.Option("--run-approved")] = False,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    _operator_go_impl(
        profile=profile,
        dry_run=dry_run,
        propose_only=propose_only,
        approve_latest=approve_latest,
        run_approved=run_approved,
        yes=yes,
    )


@operator_app.command("guide")
def operator_guide(
    profile: Annotated[str, typer.Option("--profile")] = "balanced",
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    _operator_go_impl(
        profile=profile,
        dry_run=dry_run,
        propose_only=False,
        approve_latest=False,
        run_approved=False,
        yes=False,
    )


@operator_app.command("smoke-live")
def operator_smoke_live(
    market_id: Annotated[str, typer.Option("--market")],
    side: Annotated[str, typer.Option("--side")] = "buy_yes",
    price: Annotated[str, typer.Option("--price")] = "0.001",
    size: Annotated[str, typer.Option("--size")] = "1000",
    max_loss: Annotated[str, typer.Option("--max-loss")] = "1",
    cancel_immediately: Annotated[bool, typer.Option("--cancel-immediately/--leave-open")] = True,
) -> None:
    """Submit a capped real limit order, audit every step, and optionally cancel it."""
    settings = _settings()
    database, connection, repository = _repo(settings)
    client = GammaPolymarketClient(settings)
    submitted_persisted = False
    try:
        database.init_schema()
        if settings.trading_disabled:
            raise typer.BadParameter("TRADING_DISABLED=true blocks live smoke orders")
        settings.ensure_live_trading_ready()
        blocker = live_execution_blocked(repository)
        if blocker:
            raise typer.BadParameter(blocker)
        signing = client.validate_order_signing()
        if not isinstance(signing, dict) or not signing.get("ok"):
            raise typer.BadParameter(
                str(signing.get("detail") or signing.get("status") or "order signing blocked")
            )
        if side not in {"buy_yes", "buy_no"}:
            raise typer.BadParameter("side must be buy_yes or buy_no")
        try:
            price_value = Decimal(price)
            size_value = Decimal(size)
            max_loss_value = Decimal(max_loss)
        except Exception as exc:
            raise typer.BadParameter(f"price, size, and max-loss must be decimals: {exc}") from exc
        if price_value <= 0 or price_value >= 1:
            raise typer.BadParameter("price must be greater than 0 and less than 1")
        if size_value <= 0:
            raise typer.BadParameter("size must be positive")
        if max_loss_value <= 0 or max_loss_value > Decimal("1"):
            raise typer.BadParameter("max-loss must be greater than 0 and no more than 1")
        notional = price_value * size_value
        if notional > max_loss_value:
            raise typer.BadParameter(f"notional {notional} exceeds max loss {max_loss_value}")

        market_pair = client.get_market(market_id)
        if market_pair is None:
            raise typer.BadParameter(f"market {market_id} not found")
        market, raw_market = market_pair
        repository.upsert_market(market, raw_market)
        token_id = market.yes_token_id if side == "buy_yes" else market.no_token_id
        if not token_id:
            raise typer.BadParameter(f"{side} token id is missing for market {market_id}")
        duplicate = repository.active_live_order_intent(market_id, side)
        if duplicate is not None:
            raise typer.BadParameter(
                f"duplicate active live order intent {duplicate['id']} for market {market_id}"
            )

        order = ProposedOrder(
            market_id=market_id,
            side=side,
            token_id=token_id,
            limit_price=price_value,
            size=size_value,
        )
        intent = build_order_intent(
            order,
            f"operator smoke-live max_loss={max_loss_value} cancel_immediately={cancel_immediately}",
            dry_run=False,
            status="submitted",
        )
        intent_id = repository.save_order_intent(intent)
        request_payload = {
            "step": "submit",
            "market_id": market_id,
            "token_id": token_id,
            "side": side,
            "price": str(price_value),
            "size": str(size_value),
            "max_loss": str(max_loss_value),
            "cancel_immediately": cancel_immediately,
        }
        submit_response = client.place_limit_order(
            token_id=token_id,
            side=side,
            price=str(price_value),
            size=str(size_value),
        )
        repository.save_order_attempt(
            OrderAttempt(
                intent_id=intent_id,
                request_payload=request_payload,
                response_payload=submit_response,
                status="submitted",
            )
        )
        # Durable order audit first — before roundtrip binding or further network.
        connection.commit()
        submitted_persisted = True

        if side.startswith("buy"):
            try:
                repository.record_roundtrip_buy_intent(market_id, intent_id, status="buy_open")
                connection.commit()
            except Exception as exc:
                console.print(
                    f"[yellow]roundtrip buy binding failed after exchange submit "
                    f"(order audit retained): {exc}[/yellow]"
                )

        order_id = str(
            submit_response.get("order_id")
            or submit_response.get("orderID")
            or submit_response.get("orderId")
            or submit_response.get("id")
            or ""
        )
        if not order_id:
            repository.update_order_intent_status(intent_id, "submitted")
            connection.commit()
            console.print(f"Smoke live intent {intent_id}: submitted without order id")
            return

        checked_order = client.get_order(order_id)
        repository.save_order_attempt(
            OrderAttempt(
                intent_id=intent_id,
                request_payload={"step": "check", "order_id": order_id},
                response_payload=checked_order,
                status="checked",
            )
        )
        order_status = str(
            checked_order.get("status") or submit_response.get("status") or ""
        ).lower()
        final_status = "open" if order_status in _OPENISH_ORDER_STATUSES else order_status
        if cancel_immediately and order_status in _OPENISH_ORDER_STATUSES:
            cancel_response = client.cancel_order(order_id)
            repository.save_order_attempt(
                OrderAttempt(
                    intent_id=intent_id,
                    request_payload={"step": "cancel", "order_id": order_id},
                    response_payload=cancel_response,
                    status="cancelled",
                )
            )
            cancelled = order_id in set(cancel_response.get("canceled") or [])
            final_status = "cancelled" if cancelled else "cancel_failed"

        reconciliation = ReconciliationService(client, repository).reconcile()
        repository.save_order_attempt(
            OrderAttempt(
                intent_id=intent_id,
                request_payload={"step": "reconcile", "order_id": order_id},
                response_payload=reconciliation,
                status="reconciled",
            )
        )
        repository.update_order_intent_status(intent_id, final_status or "checked")
        connection.commit()
    except Exception:
        if not submitted_persisted:
            connection.rollback()
        else:
            # Never erase a submitted exchange order audit via outer rollback.
            connection.commit()
        raise
    finally:
        connection.close()
    console.print(
        f"Smoke live intent {intent_id}: order={order_id} status={final_status or 'checked'}"
    )


@operator_app.command("launch")
def operator_launch(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1024, max=65535)] = 8765,
) -> None:
    settings = _settings()
    _database(settings).init_schema()
    console.print("[green]Beginner cockpit ready[/green]")
    console.print(f"Open: http://{host}:{port}/beginner?lang=zh")
    serve_dashboard(settings, host=host, port=port)


@operator_app.command("start")
def operator_start() -> None:
    settings = _settings()
    _database(settings).init_schema()
    console.print("[green]Operator console ready[/green]")
    console.print(f"Database: {settings.database_path}")
    console.print("Next safe commands:")
    console.print("uv run polymarket-weather operator queue")
    console.print("uv run polymarket-weather operator next")
    console.print("uv run polymarket-weather operator demo --kind dry-run")


@operator_app.command("queue")
def operator_queue(
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 20,
    status: Annotated[str | None, typer.Option("--status")] = None,
    kind: Annotated[str | None, typer.Option("--kind")] = None,
    market: Annotated[str | None, typer.Option("--market")] = None,
    failed: Annotated[bool, typer.Option("--failed")] = False,
) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        filter_status = "failed" if failed else status
        actions = AutomationService(repository).list_actions(
            status=filter_status, kind=kind, market_id=market, limit=limit
        )
    finally:
        connection.close()
    console.print(
        f"Filters: status={filter_status or 'any'} kind={kind or 'any'} market={market or 'any'} count={len(actions)}"
    )
    _print_action_table(actions)
    if not actions:
        console.print("No actions yet. Try: uv run polymarket-weather operator next")


@operator_app.command("queue-summary")
def operator_queue_summary() -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        repository.expire_automation_actions(datetime.now(timezone.utc).isoformat())
        action_counts = repository.automation_status_counts()
        candidate_counts = repository.candidate_status_counts()
        reconciliation = repository.latest_reconciliation()
    finally:
        connection.close()
    _print_counts_table("Automation actions", action_counts)
    _print_counts_table("Candidates", candidate_counts)
    if reconciliation:
        console.print(
            f"Latest reconciliation: {reconciliation['status']} at {reconciliation['created_at']}"
        )
    else:
        console.print("Latest reconciliation: none")


@operator_app.command("live-monitor")
def operator_live_monitor(
    profile: Annotated[str, typer.Option("--profile")] = "micro-live",
    allow_live_auto: Annotated[bool, typer.Option("--allow-live-auto")] = False,
    live_market: Annotated[list[str] | None, typer.Option("--live-market")] = None,
    require_fresh_reconciliation: Annotated[
        bool, typer.Option("--require-fresh-reconciliation/--no-require-fresh-reconciliation")
    ] = True,
    block_live_on_positions: Annotated[
        bool, typer.Option("--block-live-on-positions/--allow-live-with-positions")
    ] = True,
) -> None:
    settings = _settings()
    selected_profile = _profile_or_exit(profile)
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        snapshot = build_live_monitor_snapshot(
            repository,
            profile=selected_profile,
            allow_live_auto=allow_live_auto,
            live_market_ids=set(live_market or []),
            require_fresh_reconciliation=require_fresh_reconciliation,
            block_live_on_positions=block_live_on_positions,
            settings=settings,
        )
    finally:
        connection.close()
    _print_live_monitor_snapshot(snapshot)


@operator_app.command("exit-guardian")
def operator_exit_guardian(
    stale_threshold_seconds: Annotated[int, typer.Option("--stale-threshold-seconds", min=1)] = 300,
    min_edge: Annotated[str, typer.Option("--min-edge")] = "0.03",
) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        recommendations = ExitGuardianService(repository).evaluate(
            stale_threshold_seconds=stale_threshold_seconds,
            min_edge=Decimal(min_edge),
        )
    finally:
        connection.close()
    table = Table("Kind", "Action", "Market", "Reference", "Notional", "Reason", "Executes")
    for item in recommendations:
        reference = item.exchange_order_id or item.outcome or item.token_id or "-"
        table.add_row(
            item.kind,
            item.action,
            item.market_id,
            reference,
            _dash(item.notional),
            item.reason,
            "no",
        )
    console.print(table)
    for item in recommendations:
        reference = item.exchange_order_id or item.outcome or item.token_id or "-"
        console.print(
            f"Recommendation: kind={item.kind} action={item.action} "
            f"market={item.market_id} reference={reference} reason={item.reason}"
        )
    console.print(f"Exit guardian recommendations: {len(recommendations)}")
    console.print("Dry-run only: no orders were cancelled or closed.")


@operator_app.command("lifecycle-review")
def operator_lifecycle_review(
    stale_threshold_seconds: Annotated[int, typer.Option("--stale-threshold-seconds", min=1)] = 300,
    min_edge: Annotated[str, typer.Option("--min-edge")] = "0.03",
) -> None:
    """Dry-run position/order lifecycle review (no cancel or close)."""
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        recommendations = OrderLifecycleService(None, repository).review_lifecycle(
            stale_threshold_seconds=stale_threshold_seconds,
            min_edge=Decimal(min_edge),
        )
    finally:
        connection.close()
    table = Table(
        "Kind",
        "Action",
        "Market",
        "Reference",
        "Notional",
        "Edge",
        "Decision",
        "Reason",
        "Executes",
    )
    for item in recommendations:
        reference = item.exchange_order_id or item.outcome or item.token_id or "-"
        table.add_row(
            item.kind,
            item.action,
            item.market_id,
            reference,
            _dash(item.notional),
            _dash(item.latest_edge),
            item.latest_decision or "-",
            item.reason,
            "no",
        )
    console.print(table)
    for item in recommendations:
        reference = item.exchange_order_id or item.outcome or item.token_id or "-"
        console.print(
            f"Lifecycle: kind={item.kind} action={item.action} "
            f"market={item.market_id} reference={reference} reason={item.reason}"
        )
    console.print(f"Lifecycle recommendations: {len(recommendations)}")
    console.print("Dry-run only: no orders were cancelled or closed.")


@operator_app.command("queue-detail")
def operator_queue_detail(action_id: str) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        action = AutomationService(repository).status(action_id)
        events = repository.list_automation_audit_events(action_id)
    finally:
        connection.close()
    _print_action_detail(action, events)


@operator_app.command("queue-timeline")
def operator_queue_timeline(action_id: str) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        action = AutomationService(repository).status(action_id)
        events = repository.list_automation_audit_events(action_id)
    finally:
        connection.close()
    console.print(f"Timeline for {action['id']} ({action['status']})")
    _print_audit_timeline(events)


@operator_app.command("queue-failed")
def operator_queue_failed(
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 20,
) -> None:
    operator_queue(limit=limit, failed=True)


@operator_app.command("open-orders")
def operator_open_orders(
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 50,
) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        rows = repository.list_open_orders(limit=limit)
    finally:
        connection.close()
    table = Table("Order", "Market", "Side", "Price", "Size", "Notional", "Status", "Updated")
    for row in rows:
        table.add_row(
            row["exchange_order_id"],
            row["market_id"] or "-",
            row["side"] or "-",
            _dash(row["price"]),
            _dash(row["size"]),
            _dash(row["notional"]),
            row["status"] or "-",
            row["updated_at"],
        )
    console.print(table)


@operator_app.command("refresh-open-orders")
def operator_refresh_open_orders() -> None:
    settings = _settings()
    try:
        settings.ensure_live_trading_ready()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        count = OrderLifecycleService(
            GammaPolymarketClient(settings), repository
        ).refresh_open_orders()
        connection.commit()
    finally:
        connection.close()
    console.print(f"Refreshed open orders: {count}")


@operator_app.command("cancel-order")
def operator_cancel_order(order_id: str) -> None:
    settings = _settings()
    try:
        settings.ensure_live_trading_ready()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        response = OrderLifecycleService(GammaPolymarketClient(settings), repository).cancel_order(
            order_id
        )
        connection.commit()
    finally:
        connection.close()
    console.print(f"Cancelled order {order_id}: {response.get('status') or response}")


@operator_app.command("positions")
def operator_positions(
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 50,
    nonzero_only: Annotated[bool, typer.Option("--nonzero-only")] = False,
) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        rows = repository.list_positions(limit=limit, nonzero_only=nonzero_only)
    finally:
        connection.close()
    table = Table("Market", "Outcome", "Size", "Notional", "Updated")
    for row in rows:
        table.add_row(
            row["market_id"],
            row["outcome"],
            _dash(row["size"]),
            _dash(row["notional"]),
            row["updated_at"],
        )
    console.print(table)


@operator_app.command("fills")
def operator_fills(limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 50) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        rows = repository.list_fills(limit=limit)
    finally:
        connection.close()
    table = Table("Fill", "Order", "Market", "Side", "Price", "Size", "Fee", "Filled")
    for row in rows:
        table.add_row(
            row["exchange_fill_id"] or str(row["id"]),
            row["order_id"] or "-",
            row["market_id"],
            row["side"],
            _dash(row["price"]),
            _dash(row["size"]),
            _dash(row["fee"]),
            row["filled_at"],
        )
    console.print(table)


@operator_app.command("overrides")
def operator_overrides(limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 50) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        rows = repository.list_strategy_overrides(limit=limit)
    finally:
        connection.close()
    console.print(f"Overrides count={len(rows)}")
    for row in rows:
        console.print(
            f"Override: market={row['market_id']} profile={row['profile']} live_auto={_bool_label(row['live_auto_enabled'])} notes={row['notes'] or '-'}"
        )
    _print_overrides_table(rows)


@operator_app.command("override-set")
def operator_override_set(
    market: Annotated[str, typer.Option("--market")] = "*",
    profile: Annotated[str, typer.Option("--profile")] = "*",
    min_edge: Annotated[str | None, typer.Option("--min-edge")] = None,
    max_order_usdc: Annotated[str | None, typer.Option("--max-order-usdc")] = None,
    max_daily_usdc: Annotated[str | None, typer.Option("--max-daily-usdc")] = None,
    max_market_usdc: Annotated[str | None, typer.Option("--max-market-usdc")] = None,
    live_auto: Annotated[bool | None, typer.Option("--live-auto/--no-live-auto")] = None,
    notes: Annotated[str | None, typer.Option("--notes")] = None,
) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        row = repository.upsert_strategy_override(
            market_id=market,
            profile=profile,
            min_edge=min_edge,
            max_order_usdc=max_order_usdc,
            max_daily_usdc=max_daily_usdc,
            max_market_usdc=max_market_usdc,
            live_auto_enabled=live_auto,
            notes=notes,
        )
        connection.commit()
    finally:
        connection.close()
    console.print(
        f"Override: market={row['market_id']} profile={row['profile']} live_auto={_bool_label(row['live_auto_enabled'])}"
    )
    _print_overrides_table([row])


@operator_app.command("override-delete")
def operator_override_delete(
    market: Annotated[str, typer.Option("--market")],
    profile: Annotated[str, typer.Option("--profile")],
) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        deleted = repository.delete_strategy_override(market, profile)
        connection.commit()
    finally:
        connection.close()
    console.print(f"Deleted override: {deleted}")


@operator_app.command("action")
def operator_action(action_id: str) -> None:
    operator_queue_detail(action_id)


@operator_app.command("next")
def operator_next(profile: Annotated[str, typer.Option("--profile")] = "balanced") -> None:
    settings = _settings()
    selected_profile = _profile_or_exit(profile)
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        suggestion = AutomationService(repository).suggest_next_action()
    finally:
        connection.close()
    console.print(f"Profile: {selected_profile.name} ({selected_profile.role})")
    console.print(f"Next: {suggestion.label}")
    console.print(f"Reason: {suggestion.reason}")
    if suggestion.market_id:
        console.print(f"Market: {suggestion.market_id}")
    if suggestion.action_id:
        console.print(f"Action: {suggestion.action_id}")
    console.print("Run:")
    console.print(suggestion.command)


@operator_app.command("propose-next")
def operator_propose_next(
    kind: Annotated[str | None, typer.Option("--kind")] = None,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    ttl_minutes: Annotated[int | None, typer.Option("--ttl-minutes")] = None,
    profile: Annotated[str, typer.Option("--profile")] = "balanced",
) -> None:
    settings = _settings()
    selected_profile = _profile_or_exit(profile)
    action_kind = kind or selected_profile.normalized_action_kind()
    ttl = ttl_minutes if ttl_minutes is not None else selected_profile.action_ttl_minutes
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        action = AutomationService(repository).propose_next(
            kind=action_kind,
            reason=reason
            or f"operator proposed next ready candidate with {selected_profile.name} profile",
            ttl_minutes=ttl,
            requested_by=f"operator:{selected_profile.name}",
        )
        repository.append_automation_audit_event(
            action["id"],
            "profile_selected",
            "operator",
            profile_summary(selected_profile),
        )
        connection.commit()
    finally:
        connection.close()
    console.print(f"Profile: {selected_profile.name} ({selected_profile.role})")
    _print_action(action)
    _print_proposal_hints(action)


@operator_app.command("run-approved")
def operator_run_approved(
    limit: Annotated[int, typer.Option("--limit", min=1, max=20)] = 1,
) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        actions = AutomationService(repository).execute_approved(limit=limit)
        connection.commit()
    finally:
        connection.close()
    if not actions:
        console.print("No approved actions waiting. Try: uv run polymarket-weather operator queue")
        return
    _print_action_table(actions)


@operator_app.command("approve-latest")
def operator_approve_latest(
    actor: Annotated[str, typer.Option("--actor")] = "local-operator",
) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        pending = repository.latest_action_by_status("pending")
        if pending is None:
            console.print("No pending action found. Try: uv run polymarket-weather operator queue")
            return
        action = AutomationService(repository).approve(pending["id"], actor)
        connection.commit()
    finally:
        connection.close()
    _print_action(action)
    if action["status"] == "approved":
        console.print("Next: uv run polymarket-weather operator run-approved --limit 1")


@operator_app.command("demo")
def operator_demo(
    kind: Annotated[str | None, typer.Option("--kind")] = None,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    profile: Annotated[str, typer.Option("--profile")] = "dry-run-demo",
) -> None:
    settings = _settings()
    selected_profile = _profile_or_exit(profile)
    action_kind = kind or selected_profile.normalized_action_kind()
    fixture_path = Path("fixtures/markets/demo-weather-nyc-high-2026-05-08.json")
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        market_id = load_market_fixture(fixture_path, repository, settings, demo_analysis=True)
        action = AutomationService(repository).propose(
            kind=action_kind,
            market_id=market_id,
            reason=reason or f"operator demo flow with {selected_profile.name} profile",
            ttl_minutes=selected_profile.action_ttl_minutes,
            requested_by=f"operator-demo:{selected_profile.name}",
        )
        repository.append_automation_audit_event(
            action["id"],
            "profile_selected",
            "operator-demo",
            profile_summary(selected_profile),
        )
        connection.commit()
    finally:
        connection.close()
    console.print(f"Profile: {selected_profile.name} ({selected_profile.role})")
    _print_action(action)
    _print_proposal_hints(action)


@cb_app.command("status")
def cb_status() -> None:
    """Show the current status of the global circuit breaker."""
    settings = _settings()
    _, connection, repository = _repo(settings)
    try:
        service = CircuitBreakerService(repository)
        status = service.status()
        if status.tripped:
            console.print("[bold red]Circuit breaker is TRIPPED[/]")
            console.print(f"Reason: {status.reason}")
            console.print(f"Time:   {status.tripped_at}")
        else:
            console.print("[bold green]Circuit breaker is OK (not tripped)[/]")
    finally:
        connection.close()


@cb_app.command("clear")
def cb_clear(
    note: str = typer.Option(
        ..., help="Reason for clearing the breaker (e.g. 'fixed parser issue')."
    ),
) -> None:
    """Manually clear a tripped circuit breaker."""
    if not note.strip():
        console.print("[bold red]A descriptive note is required to clear the circuit breaker.[/]")
        raise typer.Exit(1)

    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        service = CircuitBreakerService(repository)
        status = service.status()
        if not status.tripped:
            console.print("[yellow]Circuit breaker is not currently tripped.[/]")
            return

        with database.transaction():
            service.clear(by="operator_cli", note=note)

        console.print(f"[bold green]Circuit breaker cleared.[/] Note: {note}")
    finally:
        connection.close()


@operator_app.command("resolution-audit")
def operator_resolution_audit(
    market_id: str = typer.Option(..., "--market", help="Market ID to audit."),
) -> None:
    """Run a resolution audit against a specific market."""
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        client = GammaPolymarketClient(settings)
        cb_service = CircuitBreakerService(repository)
        audit_service = ResolutionAuditService(repository, client, cb_service)

        with database.transaction():
            result = audit_service.audit_market(market_id)

        console.print(f"Audit Result for market {market_id}:")
        console.print(f"  Status: {result.status}")
        console.print(f"  Match: {result.match}")
        console.print(f"  Local Outcome: {result.local_resolved_outcome}")
        console.print(f"  PM Outcome: {result.polymarket_resolved_outcome}")
        console.print(f"  PM Closed: {result.polymarket_closed}")
        console.print(f"  PM UMA Status: {result.polymarket_uma_status}")

        if result.status == "mismatch":
            console.print("[bold red]Global circuit breaker TRIPPED due to mismatch.[/]")

    except Exception as exc:
        console.print(f"[bold red]Audit failed: {exc}[/]")
    finally:
        connection.close()


@operator_app.command("close-preview")
def close_preview(
    market: str = typer.Option(..., "--market", help="Market ID"),
    outcome: str = typer.Option(..., "--outcome", help="YES or NO"),
    size: str = typer.Option(None, "--size", help="Amount of shares to sell"),
    percent: str = typer.Option(None, "--percent", help="Percent of position to sell (1-100)"),
) -> None:
    """Read-only preview of closing a position."""
    settings = _settings()
    from polymarket_weather_arb.services.position_exit_service import PositionExitService

    database, connection, repository = _repo(settings)
    database.init_schema()
    client = GammaPolymarketClient(settings)

    try:
        service = PositionExitService(repository, client)
        sz = Decimal(size) if size else None
        pct = Decimal(percent) if percent else None

        result = service.preview_close(
            settings=settings, market_id=market, outcome=outcome, size=sz, percent=pct
        )

        table = Table(title="Close Preview", show_header=True)
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="magenta")
        table.add_row("Market ID", result["market_id"])
        table.add_row("Token ID", result["token_id"])
        table.add_row("Outcome", result["outcome"])
        table.add_row("Actual Position", f"{result['actual_size']:.2f}")
        table.add_row("Close Size", f"{result['close_size']:.2f}")
        table.add_row("Best Bid", f"{result['best_bid']:.4f}")
        table.add_row("Max Slippage Config", f"{result['max_slippage']:.4f}")
        table.add_row("Min Acceptable Price", f"{result['min_acceptable_price']:.4f}")
        table.add_row("Estimated USDC", f"{result['estimated_usdc']:.4f}")
        table.add_row("Quote Age (s)", f"{result['quote_age_s']:.1f}")
        table.add_row("Reconciliation Fresh", str(result["reconciliation_fresh"]))

        console.print(table)
    except ValueError as e:
        console.print(f"[red]Blocked:[/red] {e}")
        raise typer.Exit(1)
    finally:
        console.print("\n[yellow]Preview only: no SELL order was submitted.[/yellow]")


@operator_app.command("close-live")
def close_live(
    market: str = typer.Option(..., "--market", help="Market ID"),
    outcome: str = typer.Option(..., "--outcome", help="YES or NO"),
    price: str = typer.Option(..., "--price", help="Limit sell price in (0, 1)"),
    size: str = typer.Option(..., "--size", help="Shares to sell; must not exceed position"),
    max_slippage: str = typer.Option(
        ...,
        "--max-slippage",
        help="Max allowed price drop below best bid (absolute probability units)",
    ),
    confirm: str = typer.Option(
        ...,
        "--confirm",
        help="Exact phrase: SELL <market_id> <YES|NO> <size>",
    ),
) -> None:
    """Submit a confirmed SELL limit exit. Default-locked; exact confirm required.

    Does not auto-continue selling, reprice, or integrate with the daemon.
    """
    settings = _settings()
    from polymarket_weather_arb.services.position_exit_service import PositionExitService

    database, connection, repository = _repo(settings)
    database.init_schema()
    client = GammaPolymarketClient(settings)
    submitted_persisted = False
    try:
        try:
            price_value = Decimal(price)
            size_value = Decimal(size)
            slippage_value = Decimal(max_slippage)
        except Exception as exc:
            raise typer.BadParameter(
                f"price, size, and max-slippage must be decimals: {exc}"
            ) from exc

        def _persist_submitted(_intent_id: int) -> None:
            nonlocal submitted_persisted
            connection.commit()
            submitted_persisted = True

        service = PositionExitService(repository, client)
        result = service.close_live(
            settings=settings,
            market_id=market,
            outcome=outcome,
            price=price_value,
            size=size_value,
            size_text=size,
            max_slippage=slippage_value,
            confirm=confirm,
            on_submitted=_persist_submitted,
        )
        # Commit post-submit verification rows when present; never roll back a
        # submitted exchange SELL audit trail.
        connection.commit()
    except ValueError as exc:
        if not submitted_persisted:
            connection.rollback()
        else:
            connection.commit()
        console.print(f"[red]Blocked:[/red] {exc}")
        raise typer.Exit(1) from exc
    except Exception:
        if not submitted_persisted:
            connection.rollback()
        else:
            # Keep submitted intent/attempt durable even if later steps explode.
            connection.commit()
        raise
    finally:
        connection.close()

    if not result.get("ok"):
        console.print(
            f"[red]SELL failed[/red] intent={result.get('intent_id')} error={result.get('error')}"
        )
        raise typer.Exit(1)

    status = str(result.get("status") or "")
    if not result.get("verified") or status in {"submitted_unverified", "reconcile_failed"}:
        console.print(
            f"[yellow]SELL submitted but not fully verified[/yellow] "
            f"intent={result.get('intent_id')} order={result.get('order_id')} "
            f"status={status}"
        )
        if result.get("warning"):
            console.print(f"[yellow]{result['warning']}[/yellow]")
        console.print(
            "[red]Do not re-submit this SELL.[/red] "
            "Confirm the exchange order and run reconcile before any further exit."
        )
        raise typer.Exit(2)

    console.print(
        f"[green]SELL submitted[/green] intent={result['intent_id']} "
        f"order={result.get('order_id')} status={status}"
    )
    if result.get("warning"):
        console.print(f"[yellow]{result['warning']}[/yellow]")
    console.print(
        "[yellow]No automatic re-sell or fill chase. "
        "Review order status and run reconcile if needed.[/yellow]"
    )


@operator_app.command("roundtrip-status")
def roundtrip_status(market: str = typer.Option(..., "--market", help="Market ID")) -> None:
    """Show the roundtrip status for a micro-live position."""
    settings = _settings()
    from polymarket_weather_arb.services.roundtrip_status_service import RoundtripStatusService

    database, connection, repository = _repo(settings)
    database.init_schema()

    try:
        service = RoundtripStatusService(repository)
        result = service.get_status(market)
        # Persist stage transitions (e.g. completed/failed) written by the service.
        connection.commit()

        table = Table(title="Roundtrip Status", show_header=True)
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="magenta")
        table.add_row("Market ID", result.market_id)
        table.add_row("Stage", result.stage)
        table.add_row("Reconciliation Fresh", str(result.reconciliation_fresh))
        table.add_row("Open Orders", str(len(result.open_orders)))
        table.add_row("Positions", str(len(result.positions)))
        table.add_row("Buy Intents", str(len(result.buy_intents)))
        table.add_row("Sell Intents", str(len(result.sell_intents)))

        console.print(table)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
