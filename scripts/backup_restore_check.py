#!/usr/bin/env python3
"""
Backup and restore verification script for Polymarket Weather Arb.

This script verifies that:
1. Backups are being created
2. Backups are not corrupted
3. Backups can be restored
4. Database integrity is maintained after restore

Usage:
    python scripts/backup_restore_check.py [--backup-dir backups] [--test-restore]
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path


def check_backup_exists(backup_dir: Path) -> bool:
    """Check if backup directory exists and has backups."""
    if not backup_dir.exists():
        print(f"✗ Backup directory does not exist: {backup_dir}")
        return False

    backups = _backup_files(backup_dir)
    if not backups:
        print(f"✗ No backup files found in {backup_dir}")
        return False

    latest = backups[0]
    size_mb = latest.stat().st_size / (1024 * 1024)
    mtime = datetime.fromtimestamp(latest.stat().st_mtime)
    age_hours = (datetime.now() - mtime).total_seconds() / 3600

    print(f"✓ Found {len(backups)} backup(s)")
    print(f"  Latest: {latest.name}")
    print(f"  Size: {size_mb:.2f} MB")
    print(f"  Age: {age_hours:.1f} hours")

    if age_hours > 48:
        print("  ⚠ Warning: Latest backup is older than 48 hours")

    return True


def _backup_files(backup_dir: Path) -> list[Path]:
    """Return backup files produced by current and legacy backup commands."""
    backups = [*backup_dir.glob("*.sqlite3"), *backup_dir.glob("*.db")]
    return sorted(backups, key=lambda p: p.stat().st_mtime, reverse=True)


def check_backup_integrity(backup_path: Path) -> bool:
    """Check if backup file is a valid SQLite database."""
    try:
        conn = sqlite3.connect(str(backup_path))
        cursor = conn.execute("PRAGMA integrity_check")
        result = cursor.fetchone()
        conn.close()

        if result[0] == "ok":
            print(f"✓ Backup integrity check passed: {backup_path.name}")
            return True
        else:
            print(f"✗ Backup integrity check failed: {backup_path.name}")
            print(f"  Result: {result[0]}")
            return False
    except sqlite3.Error as e:
        print(f"✗ Backup is not a valid SQLite database: {backup_path.name}")
        print(f"  Error: {e}")
        return False


def check_backup_tables(backup_path: Path) -> bool:
    """Check if backup has expected tables."""
    expected_tables = {
        "markets",
        "order_intents",
        "risk_decisions",
        "weather_forecasts",
        "reconciliations",
    }

    try:
        conn = sqlite3.connect(str(backup_path))
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()

        missing = expected_tables - tables
        if missing:
            print(f"✗ Backup missing tables: {missing}")
            return False

        print("✓ Backup has all expected tables")
        return True
    except sqlite3.Error as e:
        print(f"✗ Failed to check backup tables: {e}")
        return False


def test_restore(backup_path: Path) -> bool:
    """Test restoring backup to a temporary database."""
    print(f"\nTesting restore of {backup_path.name}...")

    with tempfile.TemporaryDirectory() as tmp_dir:
        restore_path = Path(tmp_dir) / "test_restore.db"

        try:
            # Copy backup to temp location
            shutil.copy2(backup_path, restore_path)

            # Verify restored database
            conn = sqlite3.connect(str(restore_path))

            # Check integrity
            cursor = conn.execute("PRAGMA integrity_check")
            if cursor.fetchone()[0] != "ok":
                print("✗ Restored database integrity check failed")
                conn.close()
                return False

            # Check tables exist
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]

            # Check some data exists
            for table in ["markets", "order_intents"]:
                if table in tables:
                    cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
                    count = cursor.fetchone()[0]
                    print(f"  {table}: {count} rows")

            conn.close()
            print("✓ Restore test passed")
            return True

        except Exception as e:
            print(f"✗ Restore test failed: {e}")
            return False


def main():
    parser = argparse.ArgumentParser(description="Verify Polymarket Weather Arb backups")
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path("backups"),
        help="Backup directory (default: backups)",
    )
    parser.add_argument(
        "--test-restore",
        action="store_true",
        help="Test restoring the latest backup",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path("data/polymarket_weather.db"),
        help="Main database path (default: data/polymarket_weather.db)",
    )
    args = parser.parse_args()

    print("=== Polymarket Weather Arb Backup Verification ===\n")

    # Check if main database exists
    if not args.db_path.exists():
        print(f"⚠ Main database not found: {args.db_path}")
        print("  Run 'polymarket-weather init-db' first\n")

    # Check backups
    if not check_backup_exists(args.backup_dir):
        print("\nTo create a backup, run:")
        print("  polymarket-weather backup-db")
        return 1

    # Find latest backup
    backups = _backup_files(args.backup_dir)
    if not backups:
        return 1

    latest_backup = backups[0]

    # Check integrity
    print("\n--- Integrity Checks ---")
    integrity_ok = check_backup_integrity(latest_backup)
    tables_ok = check_backup_tables(latest_backup)

    # Test restore if requested
    restore_ok = True
    if args.test_restore:
        print("\n--- Restore Test ---")
        restore_ok = test_restore(latest_backup)

    # Summary
    print("\n=== Summary ===")
    all_ok = integrity_ok and tables_ok and restore_ok
    if all_ok:
        print("✓ All backup checks passed")
        return 0
    else:
        print("✗ Some backup checks failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
