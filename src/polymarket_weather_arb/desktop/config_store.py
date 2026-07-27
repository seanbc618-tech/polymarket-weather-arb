"""Atomic non-secret config.env read/write for the desktop app.

Secrets never touch this file. Writes validate through Settings before replace.
A malformed write leaves the prior valid file untouched.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Mapping

from polymarket_weather_arb.adapters.keychain import SECRET_ENV_KEYS
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.desktop.paths import DesktopPaths, _chmod_user_only

_SECRET_KEY_SET = {key.upper() for key in SECRET_ENV_KEYS}
_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


class ConfigStoreError(ValueError):
    """Raised when desktop config cannot be validated or written."""


def read_config_env(path: Path) -> dict[str, str]:
    """Parse a dotenv-style file into a flat string map (no expansion)."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE_RE.match(raw_line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key.upper() in _SECRET_KEY_SET:
            # Defensive: never load secrets from disk even if present.
            continue
        values[key] = value
    return values


def strip_secrets(values: Mapping[str, object]) -> dict[str, str]:
    """Return only non-secret stringifiable config entries."""
    cleaned: dict[str, str] = {}
    for key, value in values.items():
        if key.upper() in _SECRET_KEY_SET:
            continue
        if value is None:
            continue
        if isinstance(value, bool):
            cleaned[key] = "true" if value else "false"
        else:
            text = str(value).strip()
            if text:
                cleaned[key] = text
    return cleaned


def validate_config_values(values: Mapping[str, str]) -> Settings:
    """Validate non-secret values through the existing Settings model."""
    safe = strip_secrets(values)
    try:
        # _env_file=None avoids pulling repo .env into the validation sample.
        return Settings(_env_file=None, **_settings_kwargs_from_env_map(safe))
    except Exception as exc:  # pydantic ValidationError and ValueError
        raise ConfigStoreError(f"configuration validation failed: {exc}") from exc


def write_config_env(path: Path, values: Mapping[str, str]) -> Settings:
    """Atomically write validated non-secret config with mode 0600."""
    safe = strip_secrets(values)
    settings = validate_config_values(safe)
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_user_only(path.parent)

    lines = [
        "# Polymarket Weather desktop config (non-secret).",
        "# Secrets are stored in macOS Keychain, never in this file.",
        "",
    ]
    for key in sorted(safe):
        lines.append(f"{key}={_escape_dotenv(safe[key])}")
    payload = "\n".join(lines) + "\n"

    fd, tmp_name = tempfile.mkstemp(
        prefix=".config.env.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_path, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    return settings


def merge_config_update(
    existing: Mapping[str, str],
    updates: Mapping[str, str | None],
) -> dict[str, str]:
    """Merge updates into existing non-secret config. None deletes a key."""
    merged = strip_secrets(existing)
    for key, value in updates.items():
        if key.upper() in _SECRET_KEY_SET:
            continue
        if value is None:
            merged.pop(key, None)
            continue
        text = str(value).strip()
        if not text:
            continue
        merged[key] = text
    return merged


def default_desktop_config(paths: DesktopPaths) -> dict[str, str]:
    """Baseline non-secret config for a fresh desktop install."""
    return {
        "DATABASE_PATH": str(paths.database_path),
        "TRADING_DISABLED": "true",
        "WEATHER_PROVIDER": "open-meteo",
        "MAX_ORDER_USDC": "1",
        "MAX_DAILY_USDC": "5",
        "MAX_MARKET_USDC": "2",
        "TELEGRAM_NOTIFY_ENABLED": "false",
    }


def _escape_dotenv(value: str) -> str:
    if re.search(r'[\s#"\']', value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _settings_kwargs_from_env_map(values: Mapping[str, str]) -> dict[str, object]:
    """Map env aliases to Settings field names for constructor validation."""
    alias_to_field: dict[str, str] = {}
    for name, field in Settings.model_fields.items():
        alias = field.alias or name
        alias_to_field[str(alias).upper()] = name
        alias_to_field[name.upper()] = name

    kwargs: dict[str, object] = {}
    for key, value in values.items():
        field_name = alias_to_field.get(key.upper())
        if field_name is None:
            continue
        kwargs[field_name] = value
    return kwargs
