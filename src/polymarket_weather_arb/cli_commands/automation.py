from __future__ import annotations

import json
from typing import Annotated

import typer

from polymarket_weather_arb.cli_commands.common import (
    _action_payload,
    _print_action,
    _print_proposal_hints,
    _repo,
    _settings,
    console,
)
from polymarket_weather_arb.services.automation_service import AutomationService

automation_app = typer.Typer(help="Human-approved automation action queue.")


@automation_app.command("propose")
def automation_propose(
    kind: Annotated[str, typer.Option("--kind")],
    market: Annotated[str, typer.Option("--market")],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    ttl_minutes: Annotated[int | None, typer.Option("--ttl-minutes")] = None,
    requested_by: Annotated[str | None, typer.Option("--requested-by")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        action = AutomationService(repository).propose(
            kind=kind,
            market_id=market,
            reason=reason,
            ttl_minutes=ttl_minutes,
            requested_by=requested_by,
            idempotency_key=idempotency_key,
        )
        connection.commit()
    finally:
        connection.close()
    _print_action(action)
    _print_proposal_hints(action)


@automation_app.command("approve")
def automation_approve(
    action_id: Annotated[str, typer.Option("--action-id")],
    actor: Annotated[str, typer.Option("--actor")],
) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        action = AutomationService(repository).approve(action_id, actor)
        connection.commit()
    finally:
        connection.close()
    _print_action(action)
    if action["status"] == "approved":
        console.print("Next: uv run polymarket-weather operator run-approved --limit 1")


@automation_app.command("reject")
def automation_reject(
    action_id: Annotated[str, typer.Option("--action-id")],
    actor: Annotated[str, typer.Option("--actor")],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        action = AutomationService(repository).reject(action_id, actor, reason)
        connection.commit()
    finally:
        connection.close()
    _print_action(action)


@automation_app.command("status")
def automation_status(action_id: Annotated[str, typer.Option("--action-id")]) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        action = AutomationService(repository).status(action_id)
    finally:
        connection.close()
    _print_action(action)


@automation_app.command("execute")
def automation_execute(action_id: Annotated[str, typer.Option("--action-id")]) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        action = AutomationService(repository).execute(action_id)
        connection.commit()
    finally:
        connection.close()
    _print_action(action)


@automation_app.command("execute-approved")
def automation_execute_approved(
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
    console.print(
        json.dumps([_action_payload(action) for action in actions], ensure_ascii=False, indent=2)
    )
