from __future__ import annotations

from typing import Annotated

import typer

from polymarket_weather_arb.adapters.polymarket.client import GammaPolymarketClient
from polymarket_weather_arb.cli_commands.common import _repo, _settings, console
from polymarket_weather_arb.dashboard import serve_dashboard
from polymarket_weather_arb.profiles import get_profile
from polymarket_weather_arb.services.autopilot_service import AutopilotService
from polymarket_weather_arb.services.deploy_service import (
    build_deploy_plan,
    repo_deploy_script_path,
)
from polymarket_weather_arb.services.live_readiness_service import LiveReadinessService

autopilot_app = typer.Typer(help="Autonomous weather trading autopilot.")


@autopilot_app.command("start")
def autopilot_start(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1024, max=65535)] = 8765,
    tick_seconds: Annotated[int | None, typer.Option("--tick-seconds", min=30)] = None,
    live: Annotated[
        bool, typer.Option("--live", help="Enable live trading when gates pass.")
    ] = False,
    full_auto: Annotated[
        bool,
        typer.Option(
            "--full-auto/--no-full-auto",
            help=(
                "Unified /app full-live: full_live mode + auto-exit arming + open "
                "live_auto override (full-live profile). Full-live always includes "
                "automatic exits; AUTO_EXIT_ENABLED only controls micro-live. "
                "Requires live credentials."
            ),
        ),
    ] = False,
    once: Annotated[bool, typer.Option("--once", help="Run a single tick then exit.")] = False,
) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    service: AutopilotService | None = None
    try:
        database.init_schema()
        from pathlib import Path

        from polymarket_weather_arb.logging_config import setup_persistent_logging
        from polymarket_weather_arb.services.autopilot_service import _now_iso

        log_path = setup_persistent_logging(Path(settings.database_path).parent)
        resolved_tick_seconds = tick_seconds or settings.autopilot_tick_seconds
        want_live = live or full_auto
        mode = "live" if want_live else "dry_run"
        if full_auto:
            app_mode = "full_live"
        elif mode == "live":
            app_mode = "micro_live"
        else:
            app_mode = "paper"
        from polymarket_weather_arb.dashboard import build_app_telegram_notifier

        # Same one-shot notifier construction as serve_dashboard (no per-tick client).
        service = AutopilotService(
            settings,
            repository,
            notifier=build_app_telegram_notifier(settings),  # type: ignore[arg-type]
        )

        if full_auto:
            from polymarket_weather_arb.services.full_auto_service import (
                clear_legacy_global_live_overrides,
                resolve_full_auto_plan,
            )

            try:
                plan = resolve_full_auto_plan(
                    settings=settings,
                    profile=get_profile("full-live"),
                    live_markets_cli=None,
                )
            except ValueError as exc:
                raise typer.BadParameter(str(exc)) from exc
            clear_legacy_global_live_overrides(repository)
            console.print("[bold red]/app FULL-AUTO FULL_LIVE[/bold red]")
            console.print(
                "entry=quant trade signal -> live limit BUY | "
                "exit=position_at_risk -> auto limit SELL | "
                f"profile=full-live | whitelist_open={plan.live_whitelist_open}"
            )

        service.ensure_state(mode=mode, tick_seconds=resolved_tick_seconds)
        repository.update_autopilot_state(
            enabled=True,
            mode=mode,
            app_mode=app_mode,
            tick_seconds=resolved_tick_seconds,
            process_started_at=_now_iso(),
        )
        connection.commit()
        console.print(f"Persistent log: {log_path}")

        if once:
            result = service.tick()
            connection.commit()
            snap = service.snapshot()
            console.print(
                f"[green]Autopilot tick complete[/green] status={snap.last_tick_status} "
                f"auto_exit_executed={result.auto_exit_executed}"
            )
            return

        console.print("[green]Autopilot ready[/green]")
        console.print(f"Open: http://{host}:{port}/app?lang=zh")
        auto_exit_status = (
            "full-live"
            if app_mode == "full_live"
            else (
                "micro-live"
                if app_mode == "micro_live" and settings.auto_exit_enabled
                else "off"
            )
        )
        console.print(
            f"Mode: {mode} app_mode={app_mode} | tick every {resolved_tick_seconds}s | "
            f"auto_exit={auto_exit_status}"
        )
        serve_dashboard(
            settings,
            host=host,
            port=port,
            autopilot_tick_seconds=resolved_tick_seconds,
        )
    finally:
        if service is not None:
            try:
                service.close()
            except Exception:
                pass
        connection.close()


@autopilot_app.command("tunnel")
def autopilot_tunnel(
    host: Annotated[str | None, typer.Option("--host")] = None,
    user: Annotated[str | None, typer.Option("--user")] = None,
    local_port: Annotated[int, typer.Option("--local-port", min=1024, max=65535)] = 8765,
    ssh_port: Annotated[int | None, typer.Option("--ssh-port", min=1, max=65535)] = None,
) -> None:
    settings = _settings()
    resolved_host = host or settings.deploy_ssh_host
    if not resolved_host:
        raise typer.BadParameter("pass --host or set DEPLOY_SSH_HOST in .env")
    resolved_user = user or settings.deploy_ssh_user or "root"
    resolved_ssh_port = ssh_port or settings.deploy_ssh_port
    plan = build_deploy_plan(
        settings.model_copy(
            update={
                "deploy_ssh_host": resolved_host,
                "deploy_ssh_user": resolved_user,
                "deploy_ssh_port": resolved_ssh_port,
            }
        ),
        dashboard_port=local_port,
    )
    console.print("[green]Remote monitoring tunnel[/green]")
    console.print(plan.tunnel_command)
    if plan.local_app_url:
        console.print(f"Open: {plan.local_app_url}")


@autopilot_app.command("deploy-plan")
def autopilot_deploy_plan() -> None:
    settings = _settings()
    plan = build_deploy_plan(settings)
    script = repo_deploy_script_path()
    console.print("[green]HK VPS deploy plan[/green]")
    console.print(f"Install dir: {plan.install_dir}")
    console.print(f"Env file: {plan.env_file}")
    console.print(f"Service user: {plan.service_user}")
    console.print("Systemd units:")
    for unit in plan.systemd_units:
        console.print(f"  - {unit}")
    console.print("On the HK VPS (as root):")
    console.print(f"  git clone <repo> {plan.install_dir}")
    console.print(f"  cd {plan.install_dir} && sudo bash {script}")
    if plan.tunnel_command:
        console.print("From your laptop:")
        console.print(f"  {plan.tunnel_command}")
        console.print(f"  {plan.local_app_url}")


@autopilot_app.command("deploy-check")
def autopilot_deploy_check(
    check_exchange: Annotated[bool, typer.Option("--check-exchange")] = False,
) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        client = GammaPolymarketClient(settings) if check_exchange else None
        service = LiveReadinessService(settings, repository, client=client)
        rows = service.check(check_exchange=check_exchange)
    finally:
        connection.close()
    table_ready = True
    for row in rows:
        ok = "yes" if row.ok else "no"
        if not row.ok:
            table_ready = False
        console.print(f"{row.name}: {ok} - {row.detail}")
    if table_ready:
        console.print("[green]Deploy checks passed[/green]")
    else:
        console.print(
            "[yellow]Deploy checks have blockers; fix before TRADING_DISABLED=false[/yellow]"
        )
        raise typer.Exit(code=1)
