from typer.testing import CliRunner

from polymarket_weather_arb.cli import app


runner = CliRunner()


def test_autopilot_tunnel_requires_host():
    result = runner.invoke(app, ["autopilot", "tunnel"])
    assert result.exit_code != 0


def test_autopilot_tunnel_prints_command():
    result = runner.invoke(
        app,
        ["autopilot", "tunnel", "--host", "203.0.113.10", "--user", "root"],
    )
    assert result.exit_code == 0
    assert "ssh -N" in result.stdout
    assert "203.0.113.10" in result.stdout
    assert "Open:" in result.stdout
    assert "8765" in result.stdout


def test_autopilot_deploy_plan():
    result = runner.invoke(app, ["autopilot", "deploy-plan"])
    assert result.exit_code == 0
    assert "deploy_hk_vps.sh" in result.stdout.replace("\n", "")
    assert "polymarket-weather-autopilot.service" in result.stdout
