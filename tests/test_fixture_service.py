import json

from typer.testing import CliRunner

from polymarket_weather_arb.cli import app
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.services.fixture_service import import_market_json, load_market_fixture
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


runner = CliRunner()


def test_import_market_json_writes_parsed_fixture(tmp_path):
    input_path = tmp_path / "market.json"
    output_dir = tmp_path / "fixtures"
    input_path.write_text(
        json.dumps(
            {
                "id": "m1",
                "question": "Will NYC high temperature be above 70F on May 10?",
                "slug": "nyc-high-temp-above-70f-may-10",
                "description": "Resolved using NOAA station KNYC.",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes-token", "no-token"]',
                "events": [
                    {
                        "slug": "nyc-weather",
                        "title": "NYC weather",
                        "category": "Climate",
                        "tags": [{"label": "Weather"}],
                    }
                ],
            }
        )
    )

    output_path = import_market_json(input_path, output_dir)

    fixture = json.loads(output_path.read_text())
    assert output_path == output_dir / "nyc-high-temp-above-70f-may-10.json"
    assert fixture["market"]["event_title"] == "NYC weather"
    assert fixture["market"]["tags"] == ["Weather"]
    assert fixture["market"]["is_weather"] is True
    assert fixture["parsed_rule"]["station"] == "KNYC"
    assert fixture["parsed_rule"]["variable"] == "temperature_high"
    assert fixture["parsed_rule"]["threshold"] == "70"
    assert fixture["raw_market"]["id"] == "m1"


def test_fixtures_import_market_json_cli(tmp_path):
    input_path = tmp_path / "market.json"
    output_dir = tmp_path / "fixtures"
    input_path.write_text(
        json.dumps(
            {
                "id": "m2",
                "question": "Will Miami rainfall be above 1 inch on May 10?",
                "slug": "miami-rainfall-above-1-inch-may-10",
                "description": "Resolved using NOAA station KMIA.",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes-token", "no-token"]',
                "events": [
                    {
                        "title": "Miami weather",
                        "category": "Climate",
                        "tags": [{"label": "Weather"}],
                    }
                ],
            }
        )
    )

    result = runner.invoke(
        app, ["fixtures", "import-market-json", str(input_path), "--output-dir", str(output_dir)]
    )

    assert result.exit_code == 0
    output_path = output_dir / "miami-rainfall-above-1-inch-may-10.json"
    assert output_path.exists()


def test_load_market_fixture_seeds_demo_analysis(tmp_path):
    raw_path = tmp_path / "market.json"
    fixture_dir = tmp_path / "fixtures"
    raw_path.write_text(
        json.dumps(
            {
                "id": "m3",
                "question": "Will the high temperature in New York exceed 80°F on May 8, 2026?",
                "slug": "nyc-high-temp-demo",
                "description": "Resolution source: NOAA station KNYC.",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes-token", "no-token"]',
                "events": [
                    {"title": "NYC weather", "category": "Climate", "tags": [{"label": "Weather"}]}
                ],
            }
        )
    )
    fixture_path = import_market_json(raw_path, fixture_dir)
    database = Database(tmp_path / "test.db")
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        market_id = load_market_fixture(
            fixture_path, repo, Settings(DATABASE_PATH=tmp_path / "test.db"), demo_analysis=True
        )
        connection.commit()

        assert market_id == "m3"
        assert repo.get_market("m3") is not None
        assert repo.latest_market_snapshot("m3") is not None
        assert repo.latest_analysis("m3")["decision"] == "trade"
        candidate = repo.list_candidates()[0]
        assert candidate["market_id"] == "m3"
        assert candidate["status"] == "dry_run_ready"
    finally:
        connection.close()


def test_fixtures_load_market_fixture_cli_with_demo_analysis(tmp_path, monkeypatch):
    raw_path = tmp_path / "market.json"
    fixture_dir = tmp_path / "fixtures"
    db_path = tmp_path / "cli.db"
    raw_path.write_text(
        json.dumps(
            {
                "id": "m4",
                "question": "Will the high temperature in New York exceed 80°F on May 8, 2026?",
                "slug": "nyc-high-temp-cli-demo",
                "description": "Resolution source: NOAA station KNYC.",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes-token", "no-token"]',
                "events": [
                    {"title": "NYC weather", "category": "Climate", "tags": [{"label": "Weather"}]}
                ],
            }
        )
    )
    fixture_path = import_market_json(raw_path, fixture_dir)
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    result = runner.invoke(
        app, ["fixtures", "load-market-fixture", str(fixture_path), "--demo-analysis"]
    )

    assert result.exit_code == 0
    connection = Database(db_path).connect()
    try:
        repo = Repository(connection)
        assert repo.latest_analysis("m4")["decision"] == "trade"
    finally:
        connection.close()


def test_cli_fixture_to_trade_dry_run_flow(tmp_path, monkeypatch):
    raw_path = tmp_path / "market.json"
    fixture_dir = tmp_path / "fixtures"
    db_path = tmp_path / "flow.db"
    raw_path.write_text(
        json.dumps(
            {
                "id": "m5",
                "question": "Will the high temperature in New York exceed 80°F on May 8, 2026?",
                "slug": "nyc-high-temp-flow-demo",
                "description": "Resolution source: NOAA station KNYC.",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes-token", "no-token"]',
                "events": [
                    {"title": "NYC weather", "category": "Climate", "tags": [{"label": "Weather"}]}
                ],
            }
        )
    )
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    import_result = runner.invoke(
        app, ["fixtures", "import-market-json", str(raw_path), "--output-dir", str(fixture_dir)]
    )
    assert import_result.exit_code == 0
    load_result = runner.invoke(
        app,
        [
            "fixtures",
            "load-market-fixture",
            str(fixture_dir / "nyc-high-temp-flow-demo.json"),
            "--demo-analysis",
        ],
    )
    assert load_result.exit_code == 0
    trade_result = runner.invoke(app, ["trade", "--market", "m5", "--dry-run"])

    assert trade_result.exit_code == 0
    connection = Database(db_path).connect()
    try:
        rows = Repository(connection).list_recent_order_intents()
        assert rows[0]["market_id"] == "m5"
        assert rows[0]["dry_run"] == 1
        assert rows[0]["status"] == "dry_run"
    finally:
        connection.close()


def test_markets_cli_lists_loaded_fixture(tmp_path, monkeypatch):
    raw_path = tmp_path / "market.json"
    fixture_dir = tmp_path / "fixtures"
    db_path = tmp_path / "markets.db"
    raw_path.write_text(
        json.dumps(
            {
                "id": "m6",
                "question": "Will the high temperature in New York exceed 80°F on May 8, 2026?",
                "slug": "nyc-high-temp-market-list-demo",
                "description": "Resolution source: NOAA station KNYC.",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes-token", "no-token"]',
                "events": [
                    {"title": "NYC weather", "category": "Climate", "tags": [{"label": "Weather"}]}
                ],
            }
        )
    )
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    import_result = runner.invoke(
        app, ["fixtures", "import-market-json", str(raw_path), "--output-dir", str(fixture_dir)]
    )
    assert import_result.exit_code == 0
    load_result = runner.invoke(
        app,
        [
            "fixtures",
            "load-market-fixture",
            str(fixture_dir / "nyc-high-temp-market-list-demo.json"),
        ],
    )
    assert load_result.exit_code == 0
    markets_result = runner.invoke(app, ["markets"])

    assert markets_result.exit_code == 0
    assert "m6" in markets_result.output
    assert "True" in markets_result.output


def test_cli_live_trade_is_blocked_without_fresh_reconciliation(tmp_path, monkeypatch):
    raw_path = tmp_path / "market.json"
    fixture_dir = tmp_path / "fixtures"
    db_path = tmp_path / "live-block.db"
    raw_path.write_text(
        json.dumps(
            {
                "id": "m7",
                "question": "Will the high temperature in New York exceed 80°F on May 8, 2026?",
                "slug": "nyc-high-temp-live-block-demo",
                "description": "Resolution source: NOAA station KNYC.",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes-token", "no-token"]',
                "events": [
                    {"title": "NYC weather", "category": "Climate", "tags": [{"label": "Weather"}]}
                ],
            }
        )
    )
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "test-key")
    monkeypatch.setenv("POLYMARKET_FUNDER", "test-funder")
    monkeypatch.setenv("COMPLIANCE_CHECK_ENABLED", "false")
    monkeypatch.setenv("TRADING_DISABLED", "false")

    assert (
        runner.invoke(
            app, ["fixtures", "import-market-json", str(raw_path), "--output-dir", str(fixture_dir)]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            [
                "fixtures",
                "load-market-fixture",
                str(fixture_dir / "nyc-high-temp-live-block-demo.json"),
                "--demo-analysis",
            ],
        ).exit_code
        == 0
    )
    trade_result = runner.invoke(app, ["trade", "--market", "m7"])

    assert trade_result.exit_code == 2
    assert "reconciliation state is stale" in trade_result.output
    connection = Database(db_path).connect()
    try:
        rows = Repository(connection).list_recent_order_intents()
        assert rows[0]["status"] == "rejected"
        assert rows[0]["dry_run"] == 0
    finally:
        connection.close()


def test_cli_live_trade_blocks_signal_only_forecast_after_reconciliation(tmp_path, monkeypatch):
    raw_path = tmp_path / "market.json"
    fixture_dir = tmp_path / "fixtures"
    db_path = tmp_path / "signal-only-live.db"
    raw_path.write_text(
        json.dumps(
            {
                "id": "m_signal",
                "question": "Will the high temperature in New York exceed 80°F on May 8, 2026?",
                "slug": "nyc-high-temp-signal-only-demo",
                "description": "Resolution source: NOAA station KNYC.",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes-token", "no-token"]',
                "events": [
                    {"title": "NYC weather", "category": "Climate", "tags": [{"label": "Weather"}]}
                ],
            }
        )
    )
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "test-key")
    monkeypatch.setenv("POLYMARKET_FUNDER", "test-funder")
    monkeypatch.setenv("COMPLIANCE_CHECK_ENABLED", "false")
    monkeypatch.setenv("TRADING_DISABLED", "false")

    assert (
        runner.invoke(
            app, ["fixtures", "import-market-json", str(raw_path), "--output-dir", str(fixture_dir)]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            [
                "fixtures",
                "load-market-fixture",
                str(fixture_dir / "nyc-high-temp-signal-only-demo.json"),
                "--demo-analysis",
            ],
        ).exit_code
        == 0
    )
    database = Database(db_path)
    connection = database.connect()
    try:
        Repository(connection).save_reconciliation("ok", {"test": True})
        connection.commit()
    finally:
        connection.close()

    trade_result = runner.invoke(app, ["trade", "--market", "m_signal"])

    assert trade_result.exit_code == 2
    assert "official_forecast" in trade_result.output or "forecast source" in trade_result.output
    connection = Database(db_path).connect()
    try:
        rows = Repository(connection).list_recent_order_intents()
        assert rows[0]["status"] == "rejected"
        assert rows[0]["dry_run"] == 0
    finally:
        connection.close()


def test_candidates_cli_lists_and_marks_loaded_fixture(tmp_path, monkeypatch):
    raw_path = tmp_path / "market.json"
    fixture_dir = tmp_path / "fixtures"
    db_path = tmp_path / "candidates.db"
    raw_path.write_text(
        json.dumps(
            {
                "id": "m8",
                "question": "Will the high temperature in New York exceed 80°F on May 8, 2026?",
                "slug": "nyc-high-temp-candidate-demo",
                "description": "Resolution source: NOAA station KNYC.",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes-token", "no-token"]',
                "events": [
                    {"title": "NYC weather", "category": "Climate", "tags": [{"label": "Weather"}]}
                ],
            }
        )
    )
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    assert (
        runner.invoke(
            app, ["fixtures", "import-market-json", str(raw_path), "--output-dir", str(fixture_dir)]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            [
                "fixtures",
                "load-market-fixture",
                str(fixture_dir / "nyc-high-temp-candidate-demo.json"),
                "--demo-analysis",
            ],
        ).exit_code
        == 0
    )
    candidates_result = runner.invoke(app, ["candidates"])
    mark_result = runner.invoke(
        app, ["candidate-mark", "--market", "m8", "--status", "reviewed", "--notes", "looks good"]
    )
    reviewed_result = runner.invoke(app, ["candidates", "--status", "reviewed"])

    assert candidates_result.exit_code == 0
    assert "m8" in candidates_result.output
    assert "dry_run_ready" in candidates_result.output
    assert mark_result.exit_code == 0
    assert reviewed_result.exit_code == 0
    assert "reviewed" in reviewed_result.output
