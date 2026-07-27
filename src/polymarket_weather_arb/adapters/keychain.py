"""macOS Keychain adapter for desktop secret storage.

Uses /usr/bin/security with an argv list (never shell=True). Unit tests mock
subprocess entirely. Non-macOS platforms raise KeychainError so callers can
fall back or surface a clear message.
"""

from __future__ import annotations

import logging
import platform
import subprocess
from typing import Sequence

logger = logging.getLogger(__name__)

DEFAULT_SERVICE = "com.seanbc.polymarket-weather"
SECURITY_BIN = "/usr/bin/security"

# Env aliases that must never land in config.env or HTML.
SECRET_ENV_KEYS: tuple[str, ...] = (
    "POLYMARKET_PRIVATE_KEY",
    "BUILDER_API_KEY",
    "BUILDER_SECRET",
    "BUILDER_PASS_PHRASE",
    "GOOGLE_WEATHER_API_KEY",
    "LLM_API_KEY",
    "TELEGRAM_BOT_TOKEN",
)


class KeychainError(RuntimeError):
    """Raised when a Keychain operation fails or is unavailable."""


def is_keychain_available() -> bool:
    return platform.system() == "Darwin"


def _redact_argv(argv: Sequence[str]) -> list[str]:
    """Redact secret payloads from argv before logging or surfacing errors."""
    redacted: list[str] = []
    hide_next = False
    for part in argv:
        if hide_next:
            redacted.append("[REDACTED]")
            hide_next = False
            continue
        if part in {"-w", "-p"}:
            redacted.append(part)
            hide_next = True
            continue
        redacted.append(part)
    return redacted


def _run_security(argv: list[str], *, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    if not is_keychain_available():
        raise KeychainError("macOS Keychain is only available on Darwin")
    try:
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise KeychainError("Keychain operation timed out") from exc
    except OSError as exc:
        raise KeychainError(f"Keychain subprocess failed: {exc}") from exc


class KeychainStore:
    """Narrow get/set/delete store for named account secrets under one service."""

    def __init__(self, *, service: str = DEFAULT_SERVICE, security_bin: str = SECURITY_BIN) -> None:
        self.service = service
        self.security_bin = security_bin

    def get_secret(self, account: str) -> str | None:
        argv = [
            self.security_bin,
            "find-generic-password",
            "-s",
            self.service,
            "-a",
            account,
            "-w",
        ]
        result = _run_security(argv)
        if result.returncode != 0:
            # Item not found is normal; treat as missing without leaking stderr.
            stderr = (result.stderr or "").lower()
            if "could not be found" in stderr or result.returncode == 44:
                return None
            raise KeychainError(
                f"Keychain get failed for {account}: {_safe_error(result)}"
            )
        value = (result.stdout or "").rstrip("\n")
        return value or None

    def set_secret(self, account: str, value: str) -> None:
        if not value:
            raise KeychainError("refusing to store empty secret")
        # delete + add is the portable replace pattern for generic passwords
        self.delete_secret(account, missing_ok=True)
        argv = [
            self.security_bin,
            "add-generic-password",
            "-U",
            "-s",
            self.service,
            "-a",
            account,
            "-w",
            value,
        ]
        result = _run_security(argv)
        if result.returncode != 0:
            raise KeychainError(
                f"Keychain set failed for {account}: {_safe_error(result)}"
            )

    def delete_secret(self, account: str, *, missing_ok: bool = False) -> bool:
        argv = [
            self.security_bin,
            "delete-generic-password",
            "-s",
            self.service,
            "-a",
            account,
        ]
        result = _run_security(argv)
        if result.returncode == 0:
            return True
        stderr = (result.stderr or "").lower()
        if missing_ok or "could not be found" in stderr or result.returncode == 44:
            return False
        raise KeychainError(
            f"Keychain delete failed for {account}: {_safe_error(result)}"
        )

    def get_all_secrets(self, accounts: Sequence[str] | None = None) -> dict[str, str]:
        keys = list(accounts) if accounts is not None else list(SECRET_ENV_KEYS)
        out: dict[str, str] = {}
        for account in keys:
            try:
                value = self.get_secret(account)
            except KeychainError:
                logger.warning("keychain unavailable for %s", account)
                continue
            if value:
                out[account] = value
        return out

    def delete_all_app_secrets(self, accounts: Sequence[str] | None = None) -> list[str]:
        """Delete only this app's named items. Returns accounts that were removed."""
        keys = list(accounts) if accounts is not None else list(SECRET_ENV_KEYS)
        removed: list[str] = []
        for account in keys:
            if self.delete_secret(account, missing_ok=True):
                removed.append(account)
        return removed

    def secret_status(self, accounts: Sequence[str] | None = None) -> dict[str, bool]:
        keys = list(accounts) if accounts is not None else list(SECRET_ENV_KEYS)
        status: dict[str, bool] = {}
        for account in keys:
            try:
                status[account] = self.get_secret(account) is not None
            except KeychainError:
                status[account] = False
        return status


def _safe_error(result: subprocess.CompletedProcess[str]) -> str:
    raw = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
    # Never surface potential secret material from security(1) output.
    if len(raw) > 200:
        raw = raw[:200] + "…"
    lowered = raw.lower()
    for needle in ("password", "keychain", "private"):
        if needle in lowered and len(raw) > 80:
            return f"security exit {result.returncode}"
    return raw or f"security exit {result.returncode}"
