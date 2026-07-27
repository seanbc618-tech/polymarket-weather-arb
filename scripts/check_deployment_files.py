#!/usr/bin/env python3
"""
Check deployment files for completeness and correctness.

This script verifies that all required deployment files exist and have
the expected content. It can be run in CI or as a pre-deployment check.

Usage:
    python scripts/check_deployment_files.py
"""

from __future__ import annotations

import sys
from pathlib import Path


def check_file_exists(path: Path, description: str) -> bool:
    """Check if a file exists."""
    if path.exists():
        print(f"✓ {description}: {path}")
        return True
    else:
        print(f"✗ {description}: {path} (missing)")
        return False


def check_file_contains(path: Path, pattern: str, description: str) -> bool:
    """Check if file contains a pattern."""
    if not path.exists():
        print(f"✗ {description}: {path} (file missing)")
        return False

    content = path.read_text()
    if pattern in content:
        print(f"✓ {description}")
        return True
    else:
        print(f"✗ {description}: pattern '{pattern}' not found")
        return False


def main():
    print("=== Deployment Files Check ===\n")

    root = Path(__file__).parent.parent
    checks = []

    # Check systemd units
    print("--- systemd units ---")
    systemd_dir = root / "deploy" / "systemd"
    checks.append(
        check_file_exists(systemd_dir / "polymarket-weather-autopilot.service", "Autopilot service")
    )
    checks.append(
        check_file_exists(systemd_dir / "polymarket-weather-daemon.service", "Daemon service")
    )
    checks.append(
        check_file_exists(systemd_dir / "polymarket-weather-dashboard.service", "Dashboard service")
    )
    checks.append(
        check_file_exists(systemd_dir / "polymarket-weather-backup.service", "Backup service")
    )
    checks.append(
        check_file_exists(systemd_dir / "polymarket-weather-backup.timer", "Backup timer")
    )
    checks.append(check_file_exists(systemd_dir / "README.md", "systemd README"))

    # Check environment examples
    print("\n--- Environment files ---")
    env_dir = root / "deploy" / "env"
    checks.append(check_file_exists(env_dir / "hk-live.example.env", "HK live example env"))

    # Check scripts
    print("\n--- Scripts ---")
    scripts_dir = root / "scripts"
    checks.append(check_file_exists(scripts_dir / "install_systemd_units.sh", "Install script"))
    checks.append(check_file_exists(scripts_dir / "deploy_hk_vps.sh", "HK VPS deploy script"))
    checks.append(check_file_exists(scripts_dir / "backup_restore_check.py", "Backup check script"))
    checks.append(
        check_file_exists(scripts_dir / "rehearse_live_readiness.py", "Live readiness rehearsal")
    )

    # Check documentation
    print("\n--- Documentation ---")
    docs_dir = root / "docs"
    checks.append(
        check_file_exists(docs_dir / "hk-vps-production-checklist.md", "HK VPS checklist")
    )
    checks.append(check_file_exists(docs_dir / "claude-code-handoff.md", "Claude Code handoff"))

    # Check key content in systemd units
    print("\n--- Content checks ---")
    daemon_service = systemd_dir / "polymarket-weather-daemon.service"
    checks.append(
        check_file_contains(
            daemon_service, "EnvironmentFile", "Daemon service uses EnvironmentFile"
        )
    )

    autopilot_service = systemd_dir / "polymarket-weather-autopilot.service"
    checks.append(
        check_file_contains(
            autopilot_service,
            "autopilot start --host 127.0.0.1 --port 8765 --full-auto",
            "Autopilot service uses the canonical full-auto entry point",
        )
    )

    backup_service = systemd_dir / "polymarket-weather-backup.service"
    checks.append(
        check_file_contains(backup_service, "backup-db", "Backup service runs backup-db command")
    )
    checks.append(
        check_file_contains(
            backup_service,
            "--retention 3",
            "Backup service retains three local daily snapshots",
        )
    )
    checks.append(
        check_file_contains(
            backup_service,
            "ReadWritePaths=/opt/polymarket-weather-arb/data "
            "/opt/polymarket-weather-arb/backups",
            "Backup service permits SQLite WAL access and backup output",
        )
    )

    # Check example env has required variables
    example_env = env_dir / "hk-live.example.env"
    for var in [
        "DATABASE_PATH",
        "TRADING_DISABLED",
        "MAX_ORDER_USDC",
        "MAX_DAILY_USDC",
        "AUTOPILOT_MODE",
        "AUTOPILOT_TICK_SECONDS",
    ]:
        checks.append(check_file_contains(example_env, var, f"Example env has {var}"))

    # Summary
    print("\n=== Summary ===")
    passed = sum(1 for c in checks if c)
    failed = sum(1 for c in checks if not c)
    print(f"Passed: {passed}/{len(checks)}")

    if failed:
        print(f"Failed: {failed}")
        return 1
    else:
        print("✓ All deployment file checks passed")
        return 0


if __name__ == "__main__":
    sys.exit(main())
