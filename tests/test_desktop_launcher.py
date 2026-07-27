"""Phase 3: desktop launcher lifecycle — single instance, port fallback, routing."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.desktop.instance_lock import (
    InstanceLock,
    find_available_port,
    is_local_http_healthy,
    recover_stale_lock,
    write_runtime,
)
from polymarket_weather_arb.desktop.launcher import DesktopLauncher, LauncherConfig
from polymarket_weather_arb.desktop.paths import (
    mark_setup_complete,
    resolve_desktop_paths,
)
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def test_single_instance_lock_and_stale_recovery(tmp_path: Path) -> None:
    paths = resolve_desktop_paths(root=tmp_path / "desk")
    paths.ensure_layout()
    first = InstanceLock(paths)
    assert first.acquire() is True
    second = InstanceLock(paths)
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()

    # Stale PID recovery.
    write_runtime(paths, port=9999, pid=999_999_999)
    paths.lock_file.write_text("stale\n", encoding="utf-8")
    assert recover_stale_lock(paths) is True
    assert not paths.pid_file.exists()


def test_port_conflict_selects_fallback() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupied.bind(("127.0.0.1", 0))
        # A bound-but-not-listening SO_REUSEADDR socket may be rebound on Linux.
        occupied.listen(1)
        busy_port = occupied.getsockname()[1]
        alt = find_available_port("127.0.0.1", preferred=busy_port)
        assert alt != busy_port


def test_browser_routing_setup_vs_app(tmp_path: Path) -> None:
    paths = resolve_desktop_paths(root=tmp_path / "desk")
    paths.ensure_layout()
    launcher = DesktopLauncher(
        paths,
        config=LauncherConfig(open_browser=False, enable_status_menu=False, preferred_port=8765),
    )
    launcher._port = 8765
    assert launcher.launch_url().endswith("/setup?lang=zh") or "/setup?" in launcher.launch_url()
    mark_setup_complete(paths)
    assert "/app" in launcher.launch_url()


def test_first_launch_does_not_enable_autopilot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "desk"
    paths = resolve_desktop_paths(root=root)
    paths.ensure_layout()
    monkeypatch.setenv("POLYMARKET_DESKTOP", "1")
    monkeypatch.setenv("POLYMARKET_DESKTOP_DATA_ROOT", str(root))
    for key in ("POLYMARKET_PRIVATE_KEY", "TRADING_DISABLED", "DATABASE_PATH"):
        monkeypatch.delenv(key, raising=False)

    opened: list[str] = []
    launcher = DesktopLauncher(
        paths,
        config=LauncherConfig(
            open_browser=True,
            enable_status_menu=False,
            preferred_port=find_available_port(),
            tick_seconds=3600,
        ),
        keychain=None,
    )

    with patch(
        "polymarket_weather_arb.desktop.launcher.webbrowser.open",
        side_effect=lambda u: opened.append(u),
    ):
        thread = threading.Thread(target=launcher.run, daemon=True)
        thread.start()
        # Wait until the HTTP server actually answers (port is set before bind completes).
        deadline = time.time() + 15
        healthy = False
        while time.time() < deadline:
            if launcher.port is not None and is_local_http_healthy(
                "127.0.0.1", launcher.port, path="/setup"
            ):
                healthy = True
                break
            time.sleep(0.05)
        assert healthy, f"launcher never became healthy port={launcher.port}"
        browser_deadline = time.time() + 2
        while not opened and time.time() < browser_deadline:
            time.sleep(0.01)
        assert opened
        assert "/setup" in opened[0]

        settings = launcher.settings
        assert settings is not None
        assert settings.trading_disabled is True
        connection = Database(settings.database_path).connect()
        try:
            state = Repository(connection).get_autopilot_state()
            # ensure schema seed exists; enabled must not be forced on
            if state is not None:
                assert not bool(state["enabled"])
        finally:
            connection.close()

        launcher.pause_autopilot()
        launcher.shutdown()
        thread.join(timeout=5)


def test_duplicate_launch_focuses_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "desk"
    paths = resolve_desktop_paths(root=root)
    paths.ensure_layout()
    monkeypatch.setenv("POLYMARKET_DESKTOP", "1")
    monkeypatch.setenv("POLYMARKET_DESKTOP_DATA_ROOT", str(root))

    port = find_available_port()
    primary = DesktopLauncher(
        paths,
        config=LauncherConfig(
            open_browser=False,
            enable_status_menu=False,
            preferred_port=port,
            tick_seconds=3600,
        ),
        keychain=None,
    )
    t = threading.Thread(target=primary.run, daemon=True)
    t.start()
    deadline = time.time() + 15
    while time.time() < deadline:
        if primary.port is not None and is_local_http_healthy(
            "127.0.0.1", primary.port, path="/setup"
        ):
            break
        time.sleep(0.05)
    assert primary.port is not None
    assert is_local_http_healthy("127.0.0.1", primary.port, path="/setup")

    focused: list[str] = []
    secondary = DesktopLauncher(
        paths,
        config=LauncherConfig(open_browser=True, enable_status_menu=False, preferred_port=port),
        keychain=None,
    )
    with patch(
        "polymarket_weather_arb.desktop.launcher.webbrowser.open",
        side_effect=lambda u: focused.append(u),
    ):
        code = secondary.run()
    assert code == 0
    assert focused
    assert str(primary.port) in focused[0]

    primary.shutdown()
    t.join(timeout=5)


def test_background_uses_replaced_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Autopilot background must not freeze the launch-time Settings snapshot."""
    from polymarket_weather_arb.dashboard import (
        _run_autopilot_background,
        configure_desktop_runtime,
        clear_desktop_runtime,
        _replace_dashboard_settings,
    )
    from polymarket_weather_arb.services.autopilot_service import AutopilotService

    root = tmp_path / "desk"
    paths = resolve_desktop_paths(root=root)
    paths.ensure_layout()
    db = Database(paths.database_path)
    db.init_schema()
    connection = db.connect()
    try:
        Repository(connection).update_autopilot_state(enabled=True, app_mode="paper")
        connection.commit()
    finally:
        connection.close()

    seen: list[str] = []
    original_init = AutopilotService.__init__

    def tracking_init(self, settings, repository, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(str(settings.weather_provider))
        return original_init(self, settings, repository, **kwargs)

    old = Settings(
        _env_file=None, database_path=paths.database_path, weather_provider="old-provider"
    )
    new = Settings(
        _env_file=None, database_path=paths.database_path, weather_provider="new-provider"
    )
    clear_desktop_runtime()
    configure_desktop_runtime(settings=old, paths=paths)
    with patch.object(AutopilotService, "__init__", tracking_init):
        with patch.object(AutopilotService, "tick", return_value=None):
            _run_autopilot_background(
                db,
                old,
                None,
                tick_seconds=0,
                sleep=lambda _: None,
                max_cycles=1,
                use_pulse=False,
            )
            _replace_dashboard_settings(new)
            _run_autopilot_background(
                db,
                old,
                None,
                tick_seconds=0,
                sleep=lambda _: None,
                max_cycles=1,
                use_pulse=False,
            )
    clear_desktop_runtime()
    assert "old-provider" in seen
    assert "new-provider" in seen


def test_pause_and_quit_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "desk"
    paths = resolve_desktop_paths(root=root)
    paths.ensure_layout()
    monkeypatch.setenv("POLYMARKET_DESKTOP", "1")
    monkeypatch.setenv("POLYMARKET_DESKTOP_DATA_ROOT", str(root))
    port = find_available_port()
    launcher = DesktopLauncher(
        paths,
        config=LauncherConfig(
            open_browser=False,
            enable_status_menu=False,
            preferred_port=port,
            tick_seconds=3600,
        ),
        keychain=None,
    )
    t = threading.Thread(target=launcher.run, daemon=True)
    t.start()
    deadline = time.time() + 15
    while time.time() < deadline:
        if launcher.port is not None and is_local_http_healthy(
            "127.0.0.1", launcher.port, path="/setup"
        ):
            break
        time.sleep(0.05)
    assert launcher.port is not None

    settings = launcher.settings
    assert settings is not None
    db = Database(settings.database_path)
    db.init_schema()
    connection = db.connect()
    try:
        Repository(connection).update_autopilot_state(enabled=True, app_mode="paper")
        connection.commit()
    finally:
        connection.close()

    launcher.pause_autopilot()
    connection = db.connect()
    try:
        state = Repository(connection).get_autopilot_state()
        assert state is not None
        assert not bool(state["enabled"])
    finally:
        connection.close()

    bound_port = launcher.port
    launcher.shutdown()
    t.join(timeout=5)
    # After quit, health check should fail.
    time.sleep(0.3)
    assert not is_local_http_healthy("127.0.0.1", bound_port or port, timeout=0.3)


def test_shutdown_clears_dashboard_runtime(tmp_path: Path) -> None:
    from polymarket_weather_arb.dashboard import (
        active_dashboard_settings,
        configure_desktop_runtime,
    )

    paths = resolve_desktop_paths(root=tmp_path / "desk")
    paths.ensure_layout()
    desktop = Settings(_env_file=None, database_path=paths.database_path)
    fallback = Settings(_env_file=None, database_path=tmp_path / "fallback.db")
    launcher = DesktopLauncher(
        paths,
        config=LauncherConfig(open_browser=False, enable_status_menu=False),
    )

    configure_desktop_runtime(settings=desktop, paths=paths)
    assert active_dashboard_settings(fallback).database_path == paths.database_path

    launcher.shutdown()

    assert active_dashboard_settings(fallback).database_path == fallback.database_path
