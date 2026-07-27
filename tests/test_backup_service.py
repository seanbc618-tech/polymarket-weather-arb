import sqlite3
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from typer.testing import CliRunner

from polymarket_weather_arb.cli import app
from polymarket_weather_arb.services.backup_service import DatabaseBackupService
from polymarket_weather_arb.storage.db import Database

runner = CliRunner()


def _seed_database(path):
    database = Database(path)
    database.init_schema()
    connection = database.connect()
    try:
        connection.execute(
            "INSERT INTO reconciliations(status, details) VALUES (?, ?)",
            ("ok", "{}"),
        )
        connection.commit()
    finally:
        connection.close()


def test_database_backup_creates_readable_sqlite_copy(tmp_path):
    db_path = tmp_path / "live.db"
    output_dir = tmp_path / "backups"
    _seed_database(db_path)

    result = DatabaseBackupService(db_path).backup(output_dir=output_dir, label="prod", retention=3)

    assert result.destination.exists()
    assert result.destination.name.startswith("prod-")
    copied = sqlite3.connect(result.destination)
    try:
        count = copied.execute("SELECT COUNT(*) FROM reconciliations").fetchone()[0]
    finally:
        copied.close()
    assert count == 1
    assert result.deleted == []
    assert result.retained == 1


def test_database_backup_prunes_old_files(tmp_path):
    db_path = tmp_path / "live.db"
    output_dir = tmp_path / "backups"
    _seed_database(db_path)
    for name in ["prod-20260101T000000Z.sqlite3", "prod-20260102T000000Z.sqlite3"]:
        (output_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (output_dir / name).write_text("old")

    result = DatabaseBackupService(db_path).backup(output_dir=output_dir, label="prod", retention=2)

    names = sorted(path.name for path in output_dir.glob("prod-*.sqlite3"))
    assert len(names) == 2
    assert "prod-20260101T000000Z.sqlite3" not in names
    assert [path.name for path in result.deleted] == ["prod-20260101T000000Z.sqlite3"]


def test_backup_restore_check_finds_sqlite3_backups(tmp_path):
    module = _load_backup_restore_check_module()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "prod-20260102T000000Z.sqlite3").write_text("sqlite backup")

    assert module.check_backup_exists(backup_dir) is True


def test_backup_db_cli_uses_configured_database(tmp_path, monkeypatch):
    db_path = tmp_path / "operator.db"
    output_dir = tmp_path / "ops-backups"
    _seed_database(db_path)
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    result = runner.invoke(app, ["backup-db", "--output-dir", str(output_dir), "--retention", "1"])

    assert result.exit_code == 0
    assert "Backup:" in result.stdout
    assert len(list(output_dir.glob("operator-*.sqlite3"))) == 1


def _load_backup_restore_check_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "backup_restore_check.py"
    spec = spec_from_file_location("backup_restore_check", path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
