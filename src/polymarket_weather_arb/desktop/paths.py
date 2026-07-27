"""Desktop Application Support layout for the packaged macOS app.

Mutable runtime data never lives inside the .app bundle. CLI checkouts keep
their existing defaults unless an explicit desktop data root is supplied.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

APP_SUPPORT_NAME = "Polymarket Weather"
ENV_DATA_ROOT = "POLYMARKET_DESKTOP_DATA_ROOT"
ENV_HOME = "HOME"


@dataclass(frozen=True)
class DesktopPaths:
    root: Path
    config_env: Path
    data_dir: Path
    database_path: Path
    logs_dir: Path
    runtime_dir: Path
    pid_file: Path
    port_file: Path
    lock_file: Path
    setup_complete_marker: Path
    diagnostics_dir: Path

    def ensure_layout(self) -> None:
        """Create directories with user-only permissions where practical."""
        for path in (
            self.root,
            self.data_dir,
            self.logs_dir,
            self.runtime_dir,
            self.diagnostics_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
            _chmod_user_only(path)


def resolve_desktop_paths(*, home: Path | None = None, root: Path | None = None) -> DesktopPaths:
    """Resolve desktop data paths without requiring a real user home when overridden."""
    if root is not None:
        base = Path(root).expanduser()
    else:
        env_root = os.environ.get(ENV_DATA_ROOT)
        if env_root:
            base = Path(env_root).expanduser()
        else:
            home_path = Path(home) if home is not None else Path(os.environ.get(ENV_HOME, Path.home()))
            base = home_path / "Library" / "Application Support" / APP_SUPPORT_NAME
    base = base.resolve() if base.exists() else base
    data_dir = base / "data"
    runtime_dir = base / "runtime"
    logs_dir = base / "logs"
    return DesktopPaths(
        root=base,
        config_env=base / "config.env",
        data_dir=data_dir,
        database_path=data_dir / "polymarket_weather.db",
        logs_dir=logs_dir,
        runtime_dir=runtime_dir,
        pid_file=runtime_dir / "app.pid",
        port_file=runtime_dir / "port",
        lock_file=runtime_dir / "app.lock",
        setup_complete_marker=runtime_dir / "setup_complete",
        diagnostics_dir=base / "diagnostics",
    )


def is_setup_complete(paths: DesktopPaths) -> bool:
    return paths.setup_complete_marker.is_file()


def mark_setup_complete(paths: DesktopPaths) -> None:
    paths.ensure_layout()
    paths.setup_complete_marker.write_text("1\n", encoding="utf-8")
    _chmod_user_only(paths.setup_complete_marker)


def clear_setup_complete(paths: DesktopPaths) -> None:
    if paths.setup_complete_marker.exists():
        paths.setup_complete_marker.unlink()


def _chmod_user_only(path: Path) -> None:
    try:
        mode = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR if path.is_dir() else (
            stat.S_IRUSR | stat.S_IWUSR
        )
        if path.is_dir():
            os.chmod(path, mode)
        elif path.is_file():
            os.chmod(path, mode)
    except OSError:
        # Best-effort on platforms or filesystems that reject chmod.
        pass
