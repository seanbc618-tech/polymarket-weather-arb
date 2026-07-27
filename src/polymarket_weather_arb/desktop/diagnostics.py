"""Redacted diagnostics export for the desktop app."""

from __future__ import annotations

import json
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_weather_arb.adapters.keychain import SECRET_ENV_KEYS, KeychainStore
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.desktop.paths import DesktopPaths, is_setup_complete
from polymarket_weather_arb.logging_config import redact_text


def build_diagnostics_payload(
    settings: Settings,
    paths: DesktopPaths,
    *,
    version: str,
    keychain: KeychainStore | None = None,
    recent_log_lines: int = 80,
) -> dict[str, Any]:
    secret_status: dict[str, str] = {}
    if keychain is not None:
        for key, present in keychain.secret_status().items():
            secret_status[key] = "configured" if present else "not configured"
    else:
        for key in SECRET_ENV_KEYS:
            secret_status[key] = "not checked"

    log_excerpt = _read_redacted_logs(paths.logs_dir, limit=recent_log_lines)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": version,
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "paths": {
            "root": str(paths.root),
            "config_env": str(paths.config_env),
            "database": str(paths.database_path),
            "logs": str(paths.logs_dir),
            "runtime": str(paths.runtime_dir),
        },
        "setup_complete": is_setup_complete(paths),
        "readiness": {
            "trading_disabled": bool(settings.trading_disabled),
            "weather_provider": settings.weather_provider,
            "live_credentials": (
                "configured"
                if settings.polymarket_private_key and settings.polymarket_funder
                else "not configured"
            ),
            "max_order_usdc": str(settings.max_order_usdc),
            "max_daily_usdc": str(settings.max_daily_usdc),
            "max_market_usdc": str(settings.max_market_usdc),
        },
        "secrets": secret_status,
        "recent_logs": log_excerpt,
    }


def write_diagnostics_export(
    settings: Settings,
    paths: DesktopPaths,
    *,
    version: str,
    keychain: KeychainStore | None = None,
) -> Path:
    paths.ensure_layout()
    payload = build_diagnostics_payload(settings, paths, version=version, keychain=keychain)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = paths.diagnostics_dir / f"diagnostics-{stamp}.json"
    text = json.dumps(payload, indent=2, sort_keys=True)
    # Belt-and-suspenders redaction.
    out.write_text(redact_text(text), encoding="utf-8")
    return out


def _read_redacted_logs(logs_dir: Path, *, limit: int) -> list[str]:
    log_file = logs_dir / "autopilot.log"
    if not log_file.is_file():
        return []
    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [redact_text(line) for line in lines[-limit:]]
