"""macOS desktop composition root.

Owns process lifecycle only: data paths, settings load, single-instance lock,
dashboard server, browser open, and status-menu controls. Strategy, pricing,
risk, reconciliation, BUY, and SELL remain in existing services.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from polymarket_weather_arb.adapters.keychain import KeychainStore, is_keychain_available
from polymarket_weather_arb.config import Settings, load_settings
from polymarket_weather_arb.desktop.instance_lock import (
    InstanceLock,
    find_available_port,
    is_local_http_healthy,
    read_runtime_port,
    recover_stale_lock,
    write_runtime,
)
from polymarket_weather_arb.desktop.paths import (
    DesktopPaths,
    is_setup_complete,
    resolve_desktop_paths,
)

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


@dataclass
class LauncherConfig:
    host: str = DEFAULT_HOST
    preferred_port: int = DEFAULT_PORT
    open_browser: bool = True
    enable_status_menu: bool = True
    tick_seconds: int | None = None


class DesktopLauncher:
    """Composition root for the packaged app."""

    def __init__(
        self,
        paths: DesktopPaths | None = None,
        *,
        config: LauncherConfig | None = None,
        keychain: KeychainStore | None = None,
        server_factory: Callable[..., object] | None = None,
    ) -> None:
        self.paths = paths or resolve_desktop_paths()
        self.config = config or LauncherConfig()
        self.keychain = keychain
        if self.keychain is None and is_keychain_available():
            self.keychain = KeychainStore()
        self._lock = InstanceLock(self.paths)
        self._server: object | None = None
        self._server_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._port: int | None = None
        self._settings: Settings | None = None
        self._server_factory = server_factory

    @property
    def port(self) -> int | None:
        return self._port

    @property
    def settings(self) -> Settings | None:
        return self._settings

    def launch_url(self) -> str:
        port = self._port or self.config.preferred_port
        path = "/app" if is_setup_complete(self.paths) else "/setup"
        return f"http://{self.config.host}:{port}{path}?lang=zh"

    def run(self) -> int:
        """Main entry. Returns process exit code."""
        os.environ["POLYMARKET_DESKTOP"] = "1"
        os.environ.setdefault("POLYMARKET_DESKTOP_DATA_ROOT", str(self.paths.root))
        self.paths.ensure_layout()

        from polymarket_weather_arb.logging_config import setup_persistent_logging

        log_path = setup_persistent_logging(self.paths.root)
        logger.info("desktop launcher starting; log=%s root=%s", log_path, self.paths.root)

        if not self._lock.acquire():
            # Another instance may hold the lock — focus existing UI if healthy.
            if self._focus_existing_instance():
                logger.info("existing healthy instance focused; exiting duplicate")
                return 0
            # Stale lock recovery then retry once.
            recover_stale_lock(self.paths)
            if not self._lock.acquire():
                if self._focus_existing_instance():
                    return 0
                self._alert("Polymarket Weather is already running but not healthy.")
                return 1

        try:
            secrets = self.keychain.get_all_secrets() if self.keychain is not None else {}
            self._settings = load_settings(
                desktop=True,
                desktop_root=self.paths.root,
                secrets=secrets,
            )
            # First launch must remain non-live and stopped.
            if not is_setup_complete(self.paths):
                # Do not enable trading just because the app launched.
                logger.info("first-run launch; setup incomplete; autopilot stays stopped")

            self._port = self._select_port()
            write_runtime(self.paths, port=self._port)
            self._start_server()
            self._install_signal_handlers()
            if self.config.open_browser:
                self.open_dashboard()
            if self.config.enable_status_menu:
                self._run_status_menu()
            else:
                while not self._stop.is_set():
                    time.sleep(0.5)
            return 0
        except Exception as exc:
            logger.exception("desktop launcher failed")
            self._alert(f"Polymarket Weather failed to start:\n{exc}")
            return 1
        finally:
            self.shutdown()

    def open_dashboard(self) -> None:
        url = self.launch_url()
        logger.info("opening dashboard %s", url)
        try:
            webbrowser.open(url)
        except Exception:
            logger.exception("failed to open browser")

    def pause_autopilot(self) -> None:
        if self._settings is None:
            return
        from polymarket_weather_arb.storage.db import Database
        from polymarket_weather_arb.storage.repositories import Repository

        database = Database(self._settings.database_path)
        connection = database.connect()
        try:
            Repository(connection).update_autopilot_state(enabled=False)
            connection.commit()
            logger.info("autopilot paused from desktop menu")
        finally:
            connection.close()

    def view_logs(self) -> None:
        log_file = self.paths.logs_dir / "autopilot.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        if not log_file.is_file():
            log_file.write_text("", encoding="utf-8")
        try:
            if sys.platform == "darwin":
                subprocess.run(["/usr/bin/open", "-a", "Console", str(log_file)], check=False)
            else:
                webbrowser.open(log_file.resolve().as_uri())
        except Exception:
            logger.exception("failed to open logs")

    def shutdown(self) -> None:
        if self._stop.is_set() and self._server is None:
            # Idempotent: already fully shut down.
            self._lock.release()
            return
        self._stop.set()
        server = self._server
        self._server = None
        if server is not None:
            shutdown = getattr(server, "shutdown", None)
            if callable(shutdown):
                try:
                    shutdown()
                except Exception:
                    logger.exception("server shutdown failed")
            close = getattr(server, "server_close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        # The packaged app normally exits immediately after shutdown, but
        # tests and embedded launchers may keep the process alive. Do not let
        # their next dashboard inherit this launcher's database/settings.
        from polymarket_weather_arb.dashboard import clear_desktop_runtime

        clear_desktop_runtime()
        self._lock.release()
        for path in (self.paths.lock_file, self.paths.pid_file):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _select_port(self) -> int:
        preferred = self.config.preferred_port
        # Honor PORT env if set.
        env_port = os.environ.get("POLYMARKET_DESKTOP_PORT")
        if env_port:
            try:
                preferred = int(env_port)
            except ValueError:
                pass
        return find_available_port(self.config.host, preferred)

    def _start_server(self) -> None:
        assert self._settings is not None
        assert self._port is not None
        from http.server import ThreadingHTTPServer

        import polymarket_weather_arb.dashboard as dashboard_mod
        from polymarket_weather_arb.dashboard import (
            DashboardHandler,
            build_app_telegram_notifier,
            configure_desktop_runtime,
            _run_autopilot_background,
        )
        from polymarket_weather_arb.logging_config import setup_persistent_logging
        from polymarket_weather_arb.storage.db import Database

        settings = self._settings
        configure_desktop_runtime(
            settings=settings,
            paths=self.paths,
            keychain=self.keychain,
            shutdown=self.shutdown,
        )

        if self._server_factory is not None:
            self._server = self._server_factory(settings, host=self.config.host, port=self._port)
            return

        # Start the existing dashboard HTTP stack in a background thread.
        # Autopilot ticks run only when repository state.enabled is true —
        # first launch leaves it false. tick_seconds still arms the observer
        # so later explicit /app starts work without relaunch.
        tick = self.config.tick_seconds or settings.autopilot_tick_seconds
        dashboard_mod._APP_TELEGRAM_NOTIFIER = build_app_telegram_notifier(settings)
        database = Database(settings.database_path)
        database.init_schema()
        setup_persistent_logging(self.paths.root)

        def handler_factory(*args, **kwargs):
            DashboardHandler(settings, *args, **kwargs)

        server = ThreadingHTTPServer((self.config.host, self._port), handler_factory)
        self._server = server

        threading.Thread(
            target=_run_autopilot_background,
            kwargs={
                "database": database,
                "settings": settings,
                "notifier": dashboard_mod._APP_TELEGRAM_NOTIFIER,
                "tick_seconds": tick,
            },
            daemon=True,
            name="autopilot-background",
        ).start()

        def serve_forever() -> None:
            try:
                logger.info(
                    "Dashboard listening at http://%s:%s",
                    self.config.host,
                    self._port,
                )
                server.serve_forever(poll_interval=0.5)
            finally:
                server.server_close()

        self._server_thread = threading.Thread(
            target=serve_forever, name="dashboard-http", daemon=True
        )
        self._server_thread.start()

        for _ in range(50):
            if is_local_http_healthy(self.config.host, self._port, path="/setup"):
                break
            time.sleep(0.1)

    def _focus_existing_instance(self) -> bool:
        port = read_runtime_port(self.paths)
        if port is None:
            return False
        if not is_local_http_healthy(
            self.config.host, port, path="/app"
        ) and not is_local_http_healthy(self.config.host, port, path="/setup"):
            return False
        path = "/app" if is_setup_complete(self.paths) else "/setup"
        url = f"http://{self.config.host}:{port}{path}?lang=zh"
        try:
            webbrowser.open(url)
        except Exception:
            logger.exception("failed to open existing dashboard")
            return False
        return True

    def _install_signal_handlers(self) -> None:
        def _handle(signum: int, frame: object) -> None:
            logger.info("received signal %s; shutting down", signum)
            self.shutdown()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handle)
            except ValueError:
                # Not in main thread.
                pass

    def _run_status_menu(self) -> None:
        try:
            from polymarket_weather_arb.desktop.status_menu import run_status_menu

            run_status_menu(
                open_dashboard=self.open_dashboard,
                pause_autopilot=self.pause_autopilot,
                view_logs=self.view_logs,
                quit_app=self.shutdown,
                stop_event=self._stop,
            )
        except Exception:
            logger.exception("status menu unavailable; staying alive without tray")
            while not self._stop.is_set():
                time.sleep(0.5)

    def _alert(self, message: str) -> None:
        logger.error(message)
        if sys.platform == "darwin":
            # macOS-readable alert without extra deps.
            script = (
                f'display alert "Polymarket Weather" message {self._osa_quote(message)} as critical'
            )
            try:
                subprocess.run(["/usr/bin/osascript", "-e", script], check=False)
            except Exception:
                pass

    @staticmethod
    def _osa_quote(text: str) -> str:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    config = LauncherConfig()
    if "--no-browser" in argv:
        config.open_browser = False
    if "--no-status-menu" in argv:
        config.enable_status_menu = False
    if "--port" in argv:
        idx = argv.index("--port")
        config.preferred_port = int(argv[idx + 1])
    root = os.environ.get("POLYMARKET_DESKTOP_DATA_ROOT")
    paths = resolve_desktop_paths(root=Path(root) if root else None)
    return DesktopLauncher(paths, config=config).run()


if __name__ == "__main__":
    raise SystemExit(main())
