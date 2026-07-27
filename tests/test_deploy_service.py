from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.services.deploy_service import (
    build_deploy_plan,
    repo_deploy_script_path,
)


def test_build_deploy_plan_without_ssh_host():
    settings = Settings(_env_file=None)
    plan = build_deploy_plan(settings)
    assert plan.tunnel_command is None
    assert "polymarket-weather-autopilot.service" in plan.systemd_units


def test_build_deploy_plan_with_ssh_host():
    settings = Settings(
        _env_file=None,
        deploy_ssh_host="203.0.113.10",
        deploy_ssh_user="ubuntu",
        deploy_ssh_port=2222,
    )
    plan = build_deploy_plan(settings, dashboard_port=8765)
    assert plan.tunnel_command == "ssh -N -p 2222 -L 8765:127.0.0.1:8765 ubuntu@203.0.113.10"
    assert plan.local_app_url == "http://127.0.0.1:8765/app?lang=zh"


def test_repo_deploy_script_exists():
    assert repo_deploy_script_path().exists()
