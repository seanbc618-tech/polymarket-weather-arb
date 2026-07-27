"""Single-instance lock and runtime port/PID files for the desktop app."""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass
from typing import TextIO

from polymarket_weather_arb.desktop.paths import DesktopPaths


@dataclass
class InstanceLock:
    paths: DesktopPaths
    _handle: TextIO | None = None

    def acquire(self) -> bool:
        """Try to acquire the lock. Returns False if another healthy instance holds it."""
        self.paths.ensure_layout()
        lock_path = self.paths.lock_file
        handle = open(lock_path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                # Best-effort on Windows; primary target is macOS.
                self._handle = handle
                return True
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                return False
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n")
            handle.flush()
            self._handle = handle
            return True
        except Exception:
            handle.close()
            raise

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if os.name != "nt" and not handle.closed:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass
        finally:
            try:
                if not handle.closed:
                    handle.close()
            except OSError:
                pass


def write_runtime(paths: DesktopPaths, *, port: int, pid: int | None = None) -> None:
    paths.ensure_layout()
    paths.port_file.write_text(f"{port}\n", encoding="utf-8")
    paths.pid_file.write_text(f"{pid or os.getpid()}\n", encoding="utf-8")


def read_runtime_port(paths: DesktopPaths) -> int | None:
    if not paths.port_file.is_file():
        return None
    try:
        return int(paths.port_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def read_runtime_pid(paths: DesktopPaths) -> int | None:
    if not paths.pid_file.is_file():
        return None
    try:
        return int(paths.pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def is_local_http_healthy(host: str, port: int, *, path: str = "/app", timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            request = (
                f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n"
            ).encode()
            sock.sendall(request)
            data = sock.recv(64)
            return data.startswith(b"HTTP/1.")
    except OSError:
        return False


def find_available_port(host: str = "127.0.0.1", preferred: int = 8765) -> int:
    for port in [preferred, *range(preferred + 1, preferred + 30)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    # Fall back to ephemeral assignment.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def recover_stale_lock(paths: DesktopPaths) -> bool:
    """Remove lock/runtime files when the recorded PID is dead. Returns True if cleaned."""
    pid = read_runtime_pid(paths)
    if pid is not None and is_pid_alive(pid):
        return False
    cleaned = False
    for path in (paths.lock_file, paths.pid_file, paths.port_file):
        if path.exists():
            path.unlink(missing_ok=True)
            cleaned = True
    return cleaned


def wait_for_healthy(host: str, port: int, *, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_local_http_healthy(host, port):
            return True
        time.sleep(0.2)
    return False
