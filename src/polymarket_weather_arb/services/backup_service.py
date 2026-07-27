from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class BackupResult:
    source: Path
    destination: Path
    retained: int
    deleted: list[Path]


class DatabaseBackupService:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def backup(
        self,
        *,
        output_dir: Path,
        retention: int = 14,
        label: str | None = None,
    ) -> BackupResult:
        if retention < 1:
            raise ValueError("retention must be at least 1")
        source = self.database_path
        if not source.exists():
            raise FileNotFoundError(f"database does not exist: {source}")

        output_dir.mkdir(parents=True, exist_ok=True)
        prefix = _safe_label(label or source.stem)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = output_dir / f"{prefix}-{timestamp}.sqlite3"

        source_connection = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
        try:
            destination_connection = sqlite3.connect(destination)
            try:
                source_connection.backup(destination_connection)
            finally:
                destination_connection.close()
        finally:
            source_connection.close()

        deleted = _prune_backups(output_dir, prefix, retention)
        retained = len(sorted(output_dir.glob(f"{prefix}-*.sqlite3")))
        return BackupResult(
            source=source, destination=destination, retained=retained, deleted=deleted
        )


def _safe_label(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._")
    return safe or "polymarket-weather"


def _prune_backups(output_dir: Path, prefix: str, retention: int) -> list[Path]:
    backups = sorted(output_dir.glob(f"{prefix}-*.sqlite3"), key=lambda path: path.name)
    stale = backups[:-retention]
    for path in stale:
        path.unlink()
    return stale
