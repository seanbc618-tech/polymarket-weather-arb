from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from polymarket_weather_arb.config import Settings


@dataclass(frozen=True)
class DeployPlan:
    install_dir: str
    env_file: str
    service_user: str
    dashboard_port: int
    ssh_target: str | None
    tunnel_command: str | None
    local_app_url: str | None
    systemd_units: tuple[str, ...]


def build_deploy_plan(settings: Settings, *, dashboard_port: int = 8765) -> DeployPlan:
    install_dir = "/opt/polymarket-weather-arb"
    env_file = "/etc/polymarket-weather-arb.env"
    service_user = "polymarket-weather"
    ssh_target = None
    tunnel_command = None
    local_app_url = None
    if settings.deploy_ssh_host:
        user = settings.deploy_ssh_user or "root"
        port = settings.deploy_ssh_port
        ssh_target = f"{user}@{settings.deploy_ssh_host}"
        tunnel_command = (
            f"ssh -N -p {port} -L {dashboard_port}:127.0.0.1:{dashboard_port} {ssh_target}"
        )
        local_app_url = f"http://127.0.0.1:{dashboard_port}/app?lang=zh"
    return DeployPlan(
        install_dir=install_dir,
        env_file=env_file,
        service_user=service_user,
        dashboard_port=dashboard_port,
        ssh_target=ssh_target,
        tunnel_command=tunnel_command,
        local_app_url=local_app_url,
        systemd_units=(
            "polymarket-weather-autopilot.service",
            "polymarket-weather-backup.timer",
            "polymarket-weather-backup.service",
        ),
    )


def repo_deploy_script_path() -> Path:
    return Path(__file__).resolve().parents[3] / "scripts" / "deploy_hk_vps.sh"
