"""Phase 1: desktop paths, config store, Keychain adapter, settings precedence."""

from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from polymarket_weather_arb.adapters.keychain import (
    SECRET_ENV_KEYS,
    KeychainError,
    KeychainStore,
)
from polymarket_weather_arb.config import Settings, load_settings
from polymarket_weather_arb.desktop.config_store import (
    ConfigStoreError,
    default_desktop_config,
    merge_config_update,
    read_config_env,
    strip_secrets,
    write_config_env,
)
from polymarket_weather_arb.desktop.diagnostics import build_diagnostics_payload
from polymarket_weather_arb.desktop.paths import (
    clear_setup_complete,
    is_setup_complete,
    mark_setup_complete,
    resolve_desktop_paths,
)


def test_resolve_desktop_paths_isolated_from_real_home(tmp_path: Path) -> None:
    paths = resolve_desktop_paths(home=tmp_path)
    assert paths.root == tmp_path / "Library" / "Application Support" / "Polymarket Weather"
    assert paths.database_path == paths.data_dir / "polymarket_weather.db"
    assert paths.config_env.name == "config.env"
    assert "Library" in str(paths.root)
    # Must not touch the real user Application Support tree.
    assert Path.home() not in paths.root.parents or tmp_path in paths.root.parents


def test_resolve_desktop_paths_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYMARKET_DESKTOP_DATA_ROOT", str(tmp_path / "custom-root"))
    paths = resolve_desktop_paths()
    assert paths.root == (tmp_path / "custom-root").resolve() or paths.root == tmp_path / "custom-root"
    assert paths.root.name == "custom-root" or "custom-root" in str(paths.root)


def test_desktop_layout_permissions(tmp_path: Path) -> None:
    paths = resolve_desktop_paths(root=tmp_path / "app-support")
    paths.ensure_layout()
    assert paths.data_dir.is_dir()
    assert paths.logs_dir.is_dir()
    assert paths.runtime_dir.is_dir()
    mode = stat.S_IMODE(paths.data_dir.stat().st_mode)
    # User-only bits preferred; at least owner rwx.
    assert mode & stat.S_IRWXU == stat.S_IRWXU


def test_config_atomic_write_mode_0600_and_no_secrets(tmp_path: Path) -> None:
    path = tmp_path / "config.env"
    values = {
        "WEATHER_PROVIDER": "open-meteo",
        "MAX_ORDER_USDC": "1",
        "POLYMARKET_PRIVATE_KEY": "0x" + "ab" * 32,
        "TELEGRAM_BOT_TOKEN": "123456:ABCDEF",
        "DATABASE_PATH": str(tmp_path / "db.sqlite"),
    }
    settings = write_config_env(path, values)
    assert path.is_file()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    text = path.read_text(encoding="utf-8")
    assert "POLYMARKET_PRIVATE_KEY" not in text
    assert "TELEGRAM_BOT_TOKEN" not in text
    assert "0x" not in text
    assert "ABCDEF" not in text
    assert "WEATHER_PROVIDER=open-meteo" in text
    assert settings.weather_provider == "open-meteo"
    loaded = read_config_env(path)
    assert "POLYMARKET_PRIVATE_KEY" not in loaded
    assert loaded["WEATHER_PROVIDER"] == "open-meteo"


def test_config_validation_failure_leaves_prior_file(tmp_path: Path) -> None:
    path = tmp_path / "config.env"
    write_config_env(path, {"WEATHER_PROVIDER": "open-meteo", "MAX_ORDER_USDC": "1"})
    original = path.read_text(encoding="utf-8")
    with pytest.raises(ConfigStoreError):
        write_config_env(path, {"MAX_ORDER_USDC": "-5"})
    assert path.read_text(encoding="utf-8") == original


def test_strip_secrets_and_merge() -> None:
    merged = merge_config_update(
        {"WEATHER_PROVIDER": "open-meteo", "MAX_ORDER_USDC": "1"},
        {
            "MAX_ORDER_USDC": "2",
            "POLYMARKET_PRIVATE_KEY": "should-not-land",
            "TELEGRAM_CHAT_ID": "99",
        },
    )
    assert merged["MAX_ORDER_USDC"] == "2"
    assert merged["TELEGRAM_CHAT_ID"] == "99"
    assert "POLYMARKET_PRIVATE_KEY" not in merged
    assert "should-not-land" not in strip_secrets(
        {"POLYMARKET_PRIVATE_KEY": "x", "A": "1"}
    ).get("POLYMARKET_PRIVATE_KEY", "")


def test_settings_precedence_process_env_over_keychain_over_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from polymarket_weather_arb.config import clear_desktop_managed_secrets

    clear_desktop_managed_secrets()
    paths = resolve_desktop_paths(root=tmp_path / "desk")
    paths.ensure_layout()
    write_config_env(
        paths.config_env,
        {
            "WEATHER_PROVIDER": "from-file",
            "MAX_ORDER_USDC": "1",
            "DATABASE_PATH": str(paths.database_path),
        },
    )
    # Clear competing env from the developer shell.
    for key in (
        "WEATHER_PROVIDER",
        "MAX_ORDER_USDC",
        "DATABASE_PATH",
        "POLYMARKET_PRIVATE_KEY",
        "TELEGRAM_BOT_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)

    # Keychain secret + file non-secret.
    settings = load_settings(
        desktop=True,
        desktop_root=paths.root,
        secrets={"POLYMARKET_PRIVATE_KEY": "0x" + "11" * 32},
    )
    assert settings.weather_provider == "from-file"
    assert settings.polymarket_private_key == "0x" + "11" * 32
    assert settings.database_path == paths.database_path

    # Process env wins over file and over keychain injection when operator-owned.
    monkeypatch.setenv("WEATHER_PROVIDER", "from-env")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "22" * 32)
    # Mark private key as operator-owned by clearing managed set then setting env.
    clear_desktop_managed_secrets()
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "22" * 32)
    settings2 = load_settings(
        desktop=True,
        desktop_root=paths.root,
        secrets={"POLYMARKET_PRIVATE_KEY": "0x" + "11" * 32},
    )
    assert settings2.weather_provider == "from-env"
    assert settings2.polymarket_private_key == "0x" + "22" * 32
    clear_desktop_managed_secrets()
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)


def test_desktop_keychain_update_and_delete_take_effect_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from polymarket_weather_arb.config import clear_desktop_managed_secrets

    clear_desktop_managed_secrets()
    paths = resolve_desktop_paths(root=tmp_path / "desk")
    paths.ensure_layout()
    write_config_env(
        paths.config_env,
        {"WEATHER_PROVIDER": "open-meteo", "DATABASE_PATH": str(paths.database_path)},
    )
    for key in ("POLYMARKET_PRIVATE_KEY", "DATABASE_PATH", "WEATHER_PROVIDER"):
        monkeypatch.delenv(key, raising=False)

    old = "0x" + "aa" * 32
    new = "0x" + "bb" * 32
    first = load_settings(desktop=True, desktop_root=paths.root, secrets={"POLYMARKET_PRIVATE_KEY": old})
    assert first.polymarket_private_key == old
    # Update must replace sticky env from the previous desktop load.
    second = load_settings(desktop=True, desktop_root=paths.root, secrets={"POLYMARKET_PRIVATE_KEY": new})
    assert second.polymarket_private_key == new
    # Delete must clear immediately.
    third = load_settings(desktop=True, desktop_root=paths.root, secrets={})
    assert third.polymarket_private_key is None
    clear_desktop_managed_secrets()


def test_cli_load_settings_does_not_force_desktop_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("POLYMARKET_DESKTOP_DATA_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    # Default load must not create Application Support under an isolated home.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "local.db"))
    settings = load_settings()
    assert settings.database_path == tmp_path / "local.db"
    app_support = fake_home / "Library" / "Application Support" / "Polymarket Weather"
    assert not app_support.exists()


def test_keychain_get_set_delete_argv_mocked() -> None:
    store = KeychainStore(service="com.seanbc.polymarket-weather-test")
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        cmd = argv[1] if len(argv) > 1 else ""
        if cmd == "find-generic-password":
            return SimpleNamespace(returncode=0, stdout="sekret-value\n", stderr="")
        if cmd == "delete-generic-password":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "add-generic-password":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unknown")

    with patch("polymarket_weather_arb.adapters.keychain.is_keychain_available", return_value=True):
        with patch("polymarket_weather_arb.adapters.keychain.subprocess.run", side_effect=fake_run):
            assert store.get_secret("POLYMARKET_PRIVATE_KEY") == "sekret-value"
            store.set_secret("POLYMARKET_PRIVATE_KEY", "new-secret")
            assert store.delete_secret("GOOGLE_WEATHER_API_KEY") is True

    assert any(c[1] == "find-generic-password" for c in calls)
    add_calls = [c for c in calls if c[1] == "add-generic-password"]
    assert add_calls
    # argv list form, no shell string.
    assert all(isinstance(c, list) for c in calls)
    assert "new-secret" in add_calls[0]
    assert "-s" in add_calls[0]
    assert "com.seanbc.polymarket-weather-test" in add_calls[0]


def test_keychain_missing_item_returns_none() -> None:
    store = KeychainStore()

    def fake_run(argv, **kwargs):
        return SimpleNamespace(
            returncode=44,
            stdout="",
            stderr="The specified item could not be found in the keychain.",
        )

    with patch("polymarket_weather_arb.adapters.keychain.is_keychain_available", return_value=True):
        with patch("polymarket_weather_arb.adapters.keychain.subprocess.run", side_effect=fake_run):
            assert store.get_secret("LLM_API_KEY") is None


def test_keychain_unavailable_on_non_darwin() -> None:
    store = KeychainStore()
    with patch("polymarket_weather_arb.adapters.keychain.is_keychain_available", return_value=False):
        with pytest.raises(KeychainError, match="Darwin"):
            store.get_secret("GOOGLE_WEATHER_API_KEY")


def test_secret_env_keys_cover_settings_secrets() -> None:
    # Every listed secret must be a Settings env alias.
    aliases = set()
    for name, field in Settings.model_fields.items():
        aliases.add((field.alias or name).upper())
        aliases.add(name.upper())
    for key in SECRET_ENV_KEYS:
        assert key.upper() in aliases


def test_setup_complete_marker(tmp_path: Path) -> None:
    paths = resolve_desktop_paths(root=tmp_path / "desk")
    assert not is_setup_complete(paths)
    mark_setup_complete(paths)
    assert is_setup_complete(paths)
    clear_setup_complete(paths)
    assert not is_setup_complete(paths)


def test_diagnostics_never_includes_secret_values(tmp_path: Path) -> None:
    paths = resolve_desktop_paths(root=tmp_path / "desk")
    paths.ensure_layout()
    (paths.logs_dir / "autopilot.log").write_text(
        "POLYMARKET_PRIVATE_KEY=0x" + "ab" * 32 + "\nhello\n",
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        polymarket_private_key="0x" + "cd" * 32,
        weather_provider="open-meteo",
    )
    payload = build_diagnostics_payload(settings, paths, version="0.1.0")
    blob = str(payload)
    assert "0x" + "cd" * 32 not in blob
    assert "0x" + "ab" * 32 not in blob
    assert "configured" in payload["readiness"]["live_credentials"] or "not configured" in payload[
        "readiness"
    ]["live_credentials"]
    assert any("REDACTED" in line for line in payload["recent_logs"]) or payload["recent_logs"]


def test_default_desktop_config_is_non_live(tmp_path: Path) -> None:
    paths = resolve_desktop_paths(root=tmp_path / "desk")
    cfg = default_desktop_config(paths)
    assert cfg["TRADING_DISABLED"] == "true"
    assert "AUTOPILOT_MODE" not in cfg
    assert "POLYMARKET_PRIVATE_KEY" not in cfg
