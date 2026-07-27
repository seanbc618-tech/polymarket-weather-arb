from __future__ import annotations

import json

import typer
from rich.table import Table

from polymarket_weather_arb.cli_commands.common import console
from polymarket_weather_arb.profiles import get_profile, list_profiles, profile_summary

profiles_app = typer.Typer(help="Safe strategy profile presets.")


@profiles_app.command("list")
def profiles_list() -> None:
    table = Table("Name", "Role", "Default Action", "TTL", "Discovery", "Description")
    for profile in list_profiles():
        table.add_row(
            profile.name,
            profile.role,
            profile.normalized_action_kind(),
            str(profile.action_ttl_minutes),
            f"{profile.discovery_limit} x {profile.discovery_pages}",
            profile.description,
        )
    console.print(table)


@profiles_app.command("show")
def profiles_show(name: str) -> None:
    try:
        profile = get_profile(name)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(json.dumps(profile_summary(profile), ensure_ascii=False, indent=2))
