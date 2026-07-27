from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from polymarket_weather_arb.cli_commands.common import _repo, _settings, console
from polymarket_weather_arb.services.fixture_service import import_market_json, load_market_fixture

fixtures_app = typer.Typer(help="Fixture helpers for real market samples.")


def _import_market_json_command(input_path: Path, output_dir: Path) -> None:
    output_path = import_market_json(input_path, output_dir)
    console.print(f"Wrote fixture: {output_path}")


@fixtures_app.command("import-market-json")
def fixtures_import_market_json(
    input_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    output_dir: Annotated[
        Path, typer.Option("--output-dir", help="Directory for generated market fixtures.")
    ] = Path("fixtures/markets"),
) -> None:
    _import_market_json_command(input_path, output_dir)


@fixtures_app.command("load-market-fixture")
def fixtures_load_market_fixture(
    input_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    demo_analysis: Annotated[
        bool,
        typer.Option(
            "--demo-analysis",
            help="Also seed demo quote, forecast, and analysis for dry-run testing.",
        ),
    ] = False,
) -> None:
    settings = _settings()
    database, connection, repository = _repo(settings)
    try:
        database.init_schema()
        market_id = load_market_fixture(
            input_path, repository, settings, demo_analysis=demo_analysis
        )
        connection.commit()
    finally:
        connection.close()
    console.print(f"Loaded fixture market: {market_id}")
