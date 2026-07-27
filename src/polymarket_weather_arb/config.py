from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Secret env keys mirrored in adapters.keychain.SECRET_ENV_KEYS.
# Kept here so Settings-aware code can strip secrets without importing desktop.
SECRET_ENV_KEYS: tuple[str, ...] = (
    "POLYMARKET_PRIVATE_KEY",
    "BUILDER_API_KEY",
    "BUILDER_SECRET",
    "BUILDER_PASS_PHRASE",
    "GOOGLE_WEATHER_API_KEY",
    "LLM_API_KEY",
    "TELEGRAM_BOT_TOKEN",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    database_path: Path = Field(default=Path("data/polymarket_weather.db"), alias="DATABASE_PATH")
    polymarket_gamma_api_base: str = Field(
        default="https://gamma-api.polymarket.com", alias="POLYMARKET_GAMMA_API_BASE"
    )
    polymarket_clob_api_base: str = Field(
        default="https://clob.polymarket.com", alias="POLYMARKET_CLOB_API_BASE"
    )
    polymarket_data_api_base: str = Field(
        default="https://data-api.polymarket.com", alias="POLYMARKET_DATA_API_BASE"
    )
    http_read_max_retries: int = Field(default=3, ge=0, alias="HTTP_READ_MAX_RETRIES")
    http_read_max_backoff_s: int = Field(default=30, ge=0, alias="HTTP_READ_MAX_BACKOFF_S")
    # Temporary outbound proxy (optional). Prefer PROXY_URL for a single catch-all.
    # Examples: http://127.0.0.1:7890  socks5://127.0.0.1:1080
    proxy_url: str | None = Field(default=None, alias="PROXY_URL")
    http_proxy: str | None = Field(default=None, alias="HTTP_PROXY")
    https_proxy: str | None = Field(default=None, alias="HTTPS_PROXY")
    all_proxy: str | None = Field(default=None, alias="ALL_PROXY")
    no_proxy: str | None = Field(default=None, alias="NO_PROXY")
    polymarket_private_key: str | None = Field(default=None, alias="POLYMARKET_PRIVATE_KEY")
    polymarket_funder: str | None = Field(default=None, alias="POLYMARKET_FUNDER")
    builder_api_key: str | None = Field(default=None, alias="BUILDER_API_KEY")
    builder_secret: str | None = Field(default=None, alias="BUILDER_SECRET")
    builder_pass_phrase: str | None = Field(default=None, alias="BUILDER_PASS_PHRASE")
    weather_provider: str = Field(default="open-meteo", alias="WEATHER_PROVIDER")
    google_weather_api_key: str | None = Field(default=None, alias="GOOGLE_WEATHER_API_KEY")
    china_weather_qingdao_url: str | None = Field(default=None, alias="CHINA_WEATHER_QINGDAO_URL")
    china_weather_chengdu_url: str | None = Field(default=None, alias="CHINA_WEATHER_CHENGDU_URL")
    china_weather_shanghai_url: str | None = Field(default=None, alias="CHINA_WEATHER_SHANGHAI_URL")
    china_weather_wuhan_url: str | None = Field(default=None, alias="CHINA_WEATHER_WUHAN_URL")
    china_weather_open_meteo_fallback: bool = Field(
        default=True, alias="CHINA_WEATHER_OPEN_METEO_FALLBACK"
    )
    max_order_usdc: Decimal = Field(default=Decimal("1"), alias="MAX_ORDER_USDC")
    max_daily_usdc: Decimal = Field(default=Decimal("5"), alias="MAX_DAILY_USDC")
    max_market_usdc: Decimal = Field(default=Decimal("2"), alias="MAX_MARKET_USDC")
    min_edge: Decimal = Field(default=Decimal("0.05"), alias="MIN_EDGE")
    slippage_buffer: Decimal = Field(default=Decimal("0.02"), alias="SLIPPAGE_BUFFER")
    stale_order_book_seconds: int = Field(default=300, alias="STALE_ORDER_BOOK_SECONDS")
    stale_forecast_seconds: int = Field(default=21600, alias="STALE_FORECAST_SECONDS")
    live_market_ids: str = Field(default="", alias="LIVE_MARKET_IDS")
    trading_disabled: bool = Field(default=True, alias="TRADING_DISABLED")
    compliance_check_enabled: bool = Field(default=True, alias="COMPLIANCE_CHECK_ENABLED")
    compliance_allowed_countries: str = Field(default="HK", alias="COMPLIANCE_ALLOWED_COUNTRIES")
    polymarket_geoblock_api_url: str = Field(
        default="https://polymarket.com/api/geoblock", alias="POLYMARKET_GEOBLOCK_API_URL"
    )
    # Guarded automatic exit (default OFF). Requires daemon --allow-auto-exit + micro-live/full-live.
    auto_exit_enabled: bool = Field(default=False, alias="AUTO_EXIT_ENABLED")
    max_auto_exits_per_tick: int = Field(default=1, ge=0, alias="MAX_AUTO_EXITS_PER_TICK")
    auto_exit_max_position_usdc: Decimal = Field(
        default=Decimal("1"), alias="AUTO_EXIT_MAX_POSITION_USDC"
    )
    auto_exit_max_slippage: Decimal = Field(
        default=Decimal("0.02"), alias="AUTO_EXIT_MAX_SLIPPAGE"
    )
    llm_enabled: bool = Field(default=False, alias="LLM_ENABLED")
    llm_provider: str = Field(default="openai", alias="LLM_PROVIDER")
    llm_api_base: str | None = Field(default=None, alias="LLM_API_BASE")
    llm_api_key: str | None = Field(default=None, alias="LLM_API_KEY")
    llm_model: str | None = Field(default=None, alias="LLM_MODEL")
    llm_min_confidence: Decimal = Field(default=Decimal("0.6"), alias="LLM_MIN_CONFIDENCE")
    llm_timeout_seconds: float = Field(default=60.0, alias="LLM_TIMEOUT_SECONDS")
    llm_language: str = Field(default="zh", alias="LLM_LANGUAGE")
    autopilot_tick_seconds: int = Field(default=300, alias="AUTOPILOT_TICK_SECONDS")
    # Weather markets do not need exchange-tick latency. Keep this optional
    # bridge off unless a future fast market module explicitly benefits from it.
    polymarket_market_stream_enabled: bool = Field(
        default=False, alias="POLYMARKET_MARKET_STREAM_ENABLED"
    )
    deploy_ssh_host: str | None = Field(default=None, alias="DEPLOY_SSH_HOST")
    deploy_ssh_user: str | None = Field(default=None, alias="DEPLOY_SSH_USER")
    deploy_ssh_port: int = Field(default=22, alias="DEPLOY_SSH_PORT")
    dashboard_public_origin: str | None = Field(
        default=None, alias="DASHBOARD_PUBLIC_ORIGIN"
    )
    # Telegram notifications (default OFF). Uses Bot API sendMessage.
    telegram_notify_enabled: bool = Field(default=False, alias="TELEGRAM_NOTIFY_ENABLED")
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")
    telegram_notify_min_level: str = Field(default="info", alias="TELEGRAM_NOTIFY_MIN_LEVEL")

    @field_validator("llm_enabled", mode="before")
    @classmethod
    def blank_llm_enabled_is_disabled(cls, value: object) -> object:
        """Treat blank optional LLM_ENABLED as disabled; do not coerce risk/money fields."""
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
        return value

    @field_validator("dashboard_public_origin", mode="before")
    @classmethod
    def public_dashboard_origin_must_be_exact_https(cls, value: object) -> str | None:
        if value is None or not str(value).strip():
            return None
        raw = str(value).strip()
        parsed = urlsplit(raw)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or parsed.port not in {None, 443}
        ):
            raise ValueError(
                "DASHBOARD_PUBLIC_ORIGIN must be an exact HTTPS origin "
                "such as https://weather.example.com"
            )
        return f"https://{parsed.hostname.rstrip('.').lower()}"

    @field_validator(
        "polymarket_private_key",
        "polymarket_funder",
        "builder_api_key",
        "builder_secret",
        "builder_pass_phrase",
        "google_weather_api_key",
        "china_weather_qingdao_url",
        "china_weather_chengdu_url",
        "china_weather_shanghai_url",
        "china_weather_wuhan_url",
        "telegram_bot_token",
        "telegram_chat_id",
        "proxy_url",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "llm_api_base",
        "llm_api_key",
        "llm_model",
        "deploy_ssh_host",
        "deploy_ssh_user",
        mode="before",
    )
    @classmethod
    def empty_string_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "max_order_usdc",
        "max_daily_usdc",
        "max_market_usdc",
        "min_edge",
        "slippage_buffer",
        "auto_exit_max_position_usdc",
        "auto_exit_max_slippage",
    )
    @classmethod
    def decimal_must_be_non_negative(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("value must be non-negative")
        return value

    @field_validator("llm_min_confidence")
    @classmethod
    def llm_confidence_in_unit_interval(cls, value: Decimal) -> Decimal:
        if value < 0 or value > 1:
            raise ValueError("LLM_MIN_CONFIDENCE must be between 0 and 1")
        return value

    @field_validator("llm_language")
    @classmethod
    def llm_language_must_be_supported(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"zh", "en"}:
            raise ValueError("LLM_LANGUAGE must be zh or en")
        return normalized

    @field_validator("telegram_notify_min_level")
    @classmethod
    def telegram_level_must_be_supported(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"info", "trade", "risk"}:
            raise ValueError("TELEGRAM_NOTIFY_MIN_LEVEL must be info, trade, or risk")
        return normalized

    def ensure_live_trading_ready(self) -> None:
        missing = []
        if not self.polymarket_private_key:
            missing.append("POLYMARKET_PRIVATE_KEY")
        if not self.polymarket_funder:
            missing.append("POLYMARKET_FUNDER")
        if missing:
            joined = ", ".join(missing)
            raise ValueError(f"live trading requires: {joined}")

    def builder_credentials_ready(self) -> bool:
        """True only when the complete official Builder credential triple exists."""
        return bool(
            self.builder_api_key
            and self.builder_secret
            and self.builder_pass_phrase
        )

    def builder_credentials_status(self) -> str:
        configured = [
            name
            for name, value in (
                ("BUILDER_API_KEY", self.builder_api_key),
                ("BUILDER_SECRET", self.builder_secret),
                ("BUILDER_PASS_PHRASE", self.builder_pass_phrase),
            )
            if value
        ]
        if len(configured) == 3:
            return "ready"
        if not configured:
            return "missing"
        missing = sorted(
            {
                "BUILDER_API_KEY",
                "BUILDER_SECRET",
                "BUILDER_PASS_PHRASE",
            }
            - set(configured)
        )
        return "partial; missing " + ", ".join(missing)

    def telegram_notify_ready(self) -> bool:
        return bool(
            self.telegram_notify_enabled
            and self.telegram_bot_token
            and self.telegram_chat_id
        )

# Secret env keys that desktop Keychain manages. When a value was injected by
# desktop reload, later Keychain updates/deletes must replace or clear it.
# Keys set by the operator outside desktop (true process env) are never overwritten.
_DESKTOP_MANAGED_SECRET_KEYS: set[str] = set()


def clear_desktop_managed_secrets() -> None:
    """Remove desktop-injected secret env keys (tests / clean shutdown)."""
    for key in list(_DESKTOP_MANAGED_SECRET_KEYS):
        os.environ.pop(key, None)
    _DESKTOP_MANAGED_SECRET_KEYS.clear()


def sync_desktop_secrets(secrets: Mapping[str, str] | None) -> None:
    """Apply Keychain secrets into process env for the desktop process.

    Precedence:
      - Operator-owned process env (not previously managed by desktop) wins.
      - Otherwise Keychain ``secrets`` map replaces the prior managed value.
      - Missing/deleted Keychain items clear a previously managed env entry so
        updates and deletes take effect without process restart.
    """
    secret_map = {str(k): str(v) for k, v in (secrets or {}).items() if v}
    for key in SECRET_ENV_KEYS:
        operator_owned = (
            key in os.environ
            and str(os.environ.get(key, "")).strip()
            and key not in _DESKTOP_MANAGED_SECRET_KEYS
        )
        if operator_owned:
            continue
        value = secret_map.get(key)
        if value:
            os.environ[key] = value
            _DESKTOP_MANAGED_SECRET_KEYS.add(key)
            continue
        # Keychain delete / absent managed secret: drop stale process env value.
        if key in _DESKTOP_MANAGED_SECRET_KEYS:
            os.environ.pop(key, None)
            _DESKTOP_MANAGED_SECRET_KEYS.discard(key)


def load_settings(
    *,
    env_file: str | Path | None = None,
    secrets: Mapping[str, str] | None = None,
    desktop: bool = False,
    desktop_root: str | Path | None = None,
) -> Settings:
    """Load Settings with optional desktop config + Keychain secrets.

    Precedence for the packaged desktop app:
      1. explicit process environment (operator-owned)
      2. secrets retrieved from macOS Keychain (``secrets`` mapping; reloads replace)
      3. non-secret values from the desktop ``config.env``
      4. existing Settings defaults

    CLI / source checkouts keep existing behavior (repo ``.env``) unless
    ``env_file`` or ``desktop=True`` is passed explicitly. CLI users are not
    silently migrated to Application Support.

    Desktop reloads must call this again after Keychain set/delete so the
    process env and returned Settings reflect the new material immediately.
    """
    temporary_inject: list[str] = []
    if desktop:
        # Always re-sync managed secrets so Keychain updates/deletes apply now.
        sync_desktop_secrets(secrets)
    elif secrets:
        for key, value in secrets.items():
            if not value:
                continue
            env_key = str(key)
            if env_key in os.environ and str(os.environ.get(env_key, "")).strip():
                continue
            os.environ[env_key] = str(value)
            temporary_inject.append(env_key)

    try:
        if desktop:
            settings = _load_desktop_settings(env_file=env_file, desktop_root=desktop_root)
        elif env_file is not None:
            settings = Settings(_env_file=str(env_file))
        else:
            settings = Settings()
    finally:
        # Non-desktop: never leave temporary Keychain material in process env.
        for key in temporary_inject:
            os.environ.pop(key, None)

    # Apply temporary proxy into process env for httpx/SDK clients (trust_env).
    from polymarket_weather_arb.adapters.http_client import apply_proxy_environment

    apply_proxy_environment(settings)
    return settings


def _load_desktop_settings(
    *,
    env_file: str | Path | None,
    desktop_root: str | Path | None,
) -> Settings:
    from polymarket_weather_arb.desktop.config_store import (
        default_desktop_config,
        write_config_env,
    )
    from polymarket_weather_arb.desktop.paths import resolve_desktop_paths

    paths = resolve_desktop_paths(root=Path(desktop_root) if desktop_root else None)
    paths.ensure_layout()
    config_path = Path(env_file) if env_file is not None else paths.config_env

    # Materialize defaults into config.env once so we do not mutate process-wide
    # environment (which would leak into CLI tests and other loaders).
    if not config_path.is_file():
        write_config_env(config_path, default_desktop_config(paths))

    # Prefer Application Support DB only when neither process env nor file set it.
    # Temporary env assignment is restored after Settings construction.
    previous_db = os.environ.get("DATABASE_PATH")
    injected_db = False
    if not str(os.environ.get("DATABASE_PATH", "")).strip():
        # config.env already contains DATABASE_PATH after write; still set for empty file edge.
        os.environ["DATABASE_PATH"] = str(paths.database_path)
        injected_db = True

    try:
        # pydantic-settings: process env wins over env_file; defaults live in the file.
        return Settings(_env_file=str(config_path))
    finally:
        if injected_db:
            if previous_db is None:
                os.environ.pop("DATABASE_PATH", None)
            else:
                os.environ["DATABASE_PATH"] = previous_db
