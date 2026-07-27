"""First-run setup helpers: mode/risk mapping and read-only readiness probes.

Maps UI choices onto existing Settings + Autopilot modes. Never starts live
trading or places orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping

from polymarket_weather_arb.adapters.keychain import SECRET_ENV_KEYS, KeychainStore
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.desktop.config_store import (
    ConfigStoreError,
    merge_config_update,
    read_config_env,
    write_config_env,
)
from polymarket_weather_arb.desktop.paths import DesktopPaths, mark_setup_complete
from polymarket_weather_arb.domain.risk import (
    HARDCODED_MAX_DAILY_USDC,
    HARDCODED_MAX_MARKET_USDC,
    HARDCODED_MAX_ORDER_USDC,
)
from polymarket_weather_arb.services.autopilot_service import APP_MODES, DEFAULT_APP_MODE

# User-facing operating modes → existing Autopilot app_mode values.
SETUP_MODES = {
    "observe": "observe",
    "paper": "paper",
    "micro_live": "micro_live",
    "full_live": "full_live",
}
DEFAULT_SETUP_MODE = "paper"

# Risk presets configure existing MAX_* settings only.
RISK_PRESETS: dict[str, dict[str, str]] = {
    "paper": {
        "MAX_ORDER_USDC": "1",
        "MAX_DAILY_USDC": "5",
        "MAX_MARKET_USDC": "2",
        "TRADING_DISABLED": "true",
        "AUTO_EXIT_ENABLED": "false",
        "AUTO_EXIT_MAX_POSITION_USDC": "1",
    },
    "starter_live": {
        "MAX_ORDER_USDC": "1",
        "MAX_DAILY_USDC": "5",
        "MAX_MARKET_USDC": "2",
        "TRADING_DISABLED": "false",
        "AUTO_EXIT_ENABLED": "true",
        "AUTO_EXIT_MAX_POSITION_USDC": "1",
    },
    "cautious_live": {
        "MAX_ORDER_USDC": "2",
        "MAX_DAILY_USDC": "10",
        "MAX_MARKET_USDC": "5",
        "TRADING_DISABLED": "false",
        "AUTO_EXIT_ENABLED": "true",
        "AUTO_EXIT_MAX_POSITION_USDC": "2",
    },
}

SETUP_STEPS = (
    "health",
    "mode",
    "wallet",
    "connectivity",
    "risk",
    "weather",
    "review",
)


@dataclass(frozen=True)
class SetupReadinessItem:
    name: str
    ok: bool
    status: str
    detail: str
    kind: str  # local | read | not_yet_verified


def normalize_setup_mode(value: str | None) -> str:
    mode = (value or DEFAULT_SETUP_MODE).strip().lower().replace("-", "_")
    if mode not in SETUP_MODES:
        raise ValueError(f"unsupported setup mode: {value}")
    return mode


def mode_to_settings_values(app_mode: str) -> dict[str, str]:
    mode = normalize_setup_mode(app_mode)
    # Completing setup never starts trading. Runtime mode lives in SQLite and
    # remains paused until the user explicitly starts it from /app.
    values: dict[str, str] = {}
    if mode == "observe":
        values["TRADING_DISABLED"] = "true"
    elif mode == "paper":
        values["TRADING_DISABLED"] = "true"
    # micro_live / full_live: leave TRADING_DISABLED to risk preset step
    return values


def risk_preset_values(preset: str, *, custom: Mapping[str, str] | None = None) -> dict[str, str]:
    key = (preset or "paper").strip().lower()
    if key == "custom":
        if not custom:
            raise ValueError("custom risk preset requires explicit values")
        values = {
            "MAX_ORDER_USDC": str(custom.get("MAX_ORDER_USDC", "1")),
            "MAX_DAILY_USDC": str(custom.get("MAX_DAILY_USDC", "5")),
            "MAX_MARKET_USDC": str(custom.get("MAX_MARKET_USDC", "2")),
            "TRADING_DISABLED": str(custom.get("TRADING_DISABLED", "true")),
            "AUTO_EXIT_ENABLED": str(custom.get("AUTO_EXIT_ENABLED", "false")),
            "AUTO_EXIT_MAX_POSITION_USDC": str(
                custom.get("AUTO_EXIT_MAX_POSITION_USDC", custom.get("MAX_ORDER_USDC", "1"))
            ),
        }
    elif key in RISK_PRESETS:
        values = dict(RISK_PRESETS[key])
    else:
        raise ValueError(f"unknown risk preset: {preset}")

    order = Decimal(values["MAX_ORDER_USDC"])
    daily = Decimal(values["MAX_DAILY_USDC"])
    market = Decimal(values["MAX_MARKET_USDC"])
    auto_exit = Decimal(values["AUTO_EXIT_MAX_POSITION_USDC"])
    if order > HARDCODED_MAX_ORDER_USDC:
        raise ValueError(f"MAX_ORDER_USDC exceeds hard cap {HARDCODED_MAX_ORDER_USDC}")
    if daily > HARDCODED_MAX_DAILY_USDC:
        raise ValueError(f"MAX_DAILY_USDC exceeds hard cap {HARDCODED_MAX_DAILY_USDC}")
    if market > HARDCODED_MAX_MARKET_USDC:
        raise ValueError(f"MAX_MARKET_USDC exceeds hard cap {HARDCODED_MAX_MARKET_USDC}")
    # Newly entered positions must remain eligible for automatic exit.
    if auto_exit < order:
        values["AUTO_EXIT_MAX_POSITION_USDC"] = str(order)
    return values


def derive_signer_address(private_key: str) -> str:
    from eth_account import Account

    key = private_key.strip()
    if not key.startswith("0x"):
        key = "0x" + key
    account = Account.from_key(key)
    return account.address


def apply_setup_config(
    paths: DesktopPaths,
    updates: Mapping[str, str | None],
    *,
    secrets: Mapping[str, str] | None = None,
    delete_secrets: list[str] | None = None,
    keychain: KeychainStore | None = None,
    complete: bool = False,
) -> Settings:
    """Validate and persist non-secret config; optionally update Keychain secrets."""
    existing = read_config_env(paths.config_env)
    if not existing:
        from polymarket_weather_arb.desktop.config_store import default_desktop_config

        existing = default_desktop_config(paths)
    merged = merge_config_update(existing, updates)
    # Always pin database path to desktop layout unless explicitly overridden.
    merged.setdefault("DATABASE_PATH", str(paths.database_path))
    try:
        write_config_env(paths.config_env, merged)
    except ConfigStoreError:
        raise

    if keychain is not None:
        for account in delete_secrets or []:
            if account in SECRET_ENV_KEYS:
                keychain.delete_secret(account, missing_ok=True)
        for account, value in (secrets or {}).items():
            if account not in SECRET_ENV_KEYS:
                continue
            if value is None or not str(value).strip():
                # Blank means keep existing secret.
                continue
            keychain.set_secret(account, str(value).strip())

    if complete:
        mark_setup_complete(paths)

    # Reload with secrets for the returned Settings snapshot.
    secret_map = keychain.get_all_secrets() if keychain is not None else {}
    from polymarket_weather_arb.config import load_settings

    return load_settings(desktop=True, desktop_root=paths.root, secrets=secret_map)


def build_setup_readiness(
    settings: Settings,
    *,
    repository: Any | None = None,
    gamma_probe: Callable[[], bool] | None = None,
    clob_probe: Callable[[], bool] | None = None,
    weather_probe: Callable[[], bool] | None = None,
    compliance_probe: Callable[[], tuple[bool, str]] | None = None,
    secret_status: Mapping[str, bool] | None = None,
    client: Any | None = None,
    run_network_probes: bool = True,
) -> list[SetupReadinessItem]:
    """Setup review checklist.

    Reuses ``LiveReadinessService`` for credentials, compliance/geoblock, circuit
    breaker, balance-auth and order-signing path checks. Never places an order.
    Optional weather/Gamma/CLOB probes exercise real read endpoints when enabled.
    """
    items: list[SetupReadinessItem] = []

    db_ok = bool(settings.database_path)
    items.append(
        SetupReadinessItem(
            "database",
            db_ok,
            "ok" if db_ok else "missing",
            f"database path: {settings.database_path}",
            "local",
        )
    )

    # Weather reachability (real HTTP when probing).
    weather_ok = bool(settings.weather_provider)
    weather_detail = f"provider={settings.weather_provider}"
    weather_kind = "local"
    probe = weather_probe
    if probe is None and run_network_probes:
        probe = lambda: _default_weather_probe(settings)  # noqa: E731
    if probe is not None:
        weather_kind = "read"
        try:
            reachable = bool(probe())
            weather_ok = weather_ok and reachable
            weather_detail += "; reachable" if reachable else "; unreachable"
        except Exception as exc:  # noqa: BLE001
            weather_ok = False
            weather_detail += f"; probe failed: {exc}"
    items.append(
        SetupReadinessItem(
            "weather",
            weather_ok,
            "ok" if weather_ok else "failed",
            weather_detail,
            weather_kind,
        )
    )

    # Gamma / CLOB market reads.
    for name, base, probe_fn, default_probe in (
        (
            "polymarket_gamma",
            settings.polymarket_gamma_api_base,
            gamma_probe,
            lambda: _http_get_ok(f"{settings.polymarket_gamma_api_base.rstrip('/')}/markets", settings),
        ),
        (
            "polymarket_clob",
            settings.polymarket_clob_api_base,
            clob_probe,
            lambda: _http_get_ok(f"{settings.polymarket_clob_api_base.rstrip('/')}/", settings, allow_4xx=True),
        ),
    ):
        ok = bool(base)
        detail = f"{name} base configured"
        kind = "local"
        active_probe = probe_fn
        if active_probe is None and run_network_probes:
            active_probe = default_probe
        if active_probe is not None:
            kind = "read"
            try:
                ok = bool(active_probe())
                detail = f"{name} read ok" if ok else f"{name} read failed"
            except Exception as exc:  # noqa: BLE001
                ok = False
                detail = f"{name} probe failed: {exc}"
        items.append(SetupReadinessItem(name, ok, "ok" if ok else "failed", detail, kind))

    # Reuse LiveReadinessService for credentials, compliance/geoblock, circuit
    # breaker, SDK, and stored reconciliation. Never places an order.
    if repository is not None:
        from polymarket_weather_arb.services.compliance_service import ComplianceService
        from polymarket_weather_arb.services.live_readiness_service import LiveReadinessService

        has_creds = bool(settings.polymarket_private_key and settings.polymarket_funder)
        # Wire the same authenticated client path as CLI live-readiness when
        # credentials exist (read-only balance + signing checks; no order submit).
        auth_client = client
        auth_client_error: str | None = None
        if auth_client is None and has_creds:
            auth_client, auth_client_error = build_setup_auth_client(settings)

        compliance = ComplianceService(settings)
        live_service = LiveReadinessService(
            settings, repository, client=auth_client, compliance_service=compliance
        )
        # check_exchange=False avoids full reconcile(); we add auth path checks next.
        for check in live_service.check(check_exchange=False):
            if check.name == "compliance" and compliance_probe is not None:
                try:
                    ok, detail = compliance_probe()
                except Exception as exc:  # noqa: BLE001
                    ok, detail = False, f"compliance probe failed: {exc}"
                items.append(
                    SetupReadinessItem("compliance", ok, "ok" if ok else "blocked", detail, "read")
                )
                continue
            items.append(_from_live_check(check))

        if auth_client is not None and has_creds:
            # Read-only auth validators on the existing client — no order submit.
            items.append(_from_live_check(live_service._balance_auth_path_check(), force_kind="read"))
            items.append(
                _from_live_check(live_service._order_signing_auth_path_check(), force_kind="read")
            )
        elif has_creds:
            detail = auth_client_error or (
                "credentials present but authenticated client could not be constructed"
            )
            items.append(
                SetupReadinessItem(
                    "balance_auth_path",
                    False,
                    "failed",
                    detail,
                    "not_yet_verified",
                )
            )
            items.append(
                SetupReadinessItem(
                    "order_signing_auth_path",
                    False,
                    "failed",
                    detail,
                    "not_yet_verified",
                )
            )
        else:
            items.append(
                SetupReadinessItem(
                    "balance_auth_path",
                    False,
                    "missing",
                    "live credentials missing",
                    "not_yet_verified",
                )
            )
            items.append(
                SetupReadinessItem(
                    "order_signing_auth_path",
                    False,
                    "missing",
                    "live credentials missing",
                    "not_yet_verified",
                )
            )
    else:
        items.append(
            SetupReadinessItem(
                "compliance",
                False,
                "skipped",
                "no repository — LiveReadinessService not run",
                "local",
            )
        )

    items.append(
        SetupReadinessItem(
            "trading_disabled",
            True,
            "on" if settings.trading_disabled else "off",
            f"TRADING_DISABLED={settings.trading_disabled}",
            "local",
        )
    )
    items.append(
        SetupReadinessItem(
            "app_mode",
            True,
            _saved_app_mode(repository),
            "setup never starts Autopilot; start explicitly from /app",
            "local",
        )
    )

    live_ready = bool(settings.polymarket_private_key and settings.polymarket_funder)
    if secret_status is not None:
        live_ready = bool(secret_status.get("POLYMARKET_PRIVATE_KEY")) and bool(
            settings.polymarket_funder
        )
    items.append(
        SetupReadinessItem(
            "live_credentials",
            live_ready,
            "configured" if live_ready else "not configured",
            "private key status only; value never shown",
            "local",
        )
    )
    telegram_ok = bool(settings.telegram_chat_id) if settings.telegram_notify_enabled else True
    items.append(
        SetupReadinessItem(
            "telegram",
            telegram_ok,
            "enabled" if settings.telegram_notify_enabled else "disabled",
            "optional notifications",
            "local",
        )
    )
    return items


def _saved_app_mode(repository: Any | None) -> str:
    if repository is None:
        return DEFAULT_SETUP_MODE
    state = repository.get_autopilot_state()
    if state is None:
        return DEFAULT_SETUP_MODE
    value = str(dict(state).get("app_mode") or DEFAULT_SETUP_MODE)
    return value if value in SETUP_MODES else DEFAULT_SETUP_MODE


def build_setup_auth_client(settings: Settings) -> tuple[Any | None, str | None]:
    """Build the same Gamma authenticated client used by CLI live-readiness.

    Returns ``(client, error_detail)``. Never places orders; client is only used
    for read-only balance and local signing-path validation.
    """
    try:
        settings.ensure_live_trading_ready()
    except ValueError as exc:
        return None, str(exc)
    try:
        from polymarket_weather_arb.adapters.polymarket.client import GammaPolymarketClient

        return GammaPolymarketClient(settings), None
    except Exception as exc:  # noqa: BLE001 - surface construction failure in UI
        return None, f"authenticated client unavailable: {exc}"


def _from_live_check(check: Any, *, force_kind: str | None = None) -> SetupReadinessItem:
    kind = force_kind or (
        "read"
        if check.name
        in {"compliance", "balance_auth_path", "order_signing_auth_path", "reconciliation"}
        else "local"
    )
    return SetupReadinessItem(check.name, check.ok, check.status, check.detail, kind)


def _http_get_ok(url: str, settings: Settings, *, allow_4xx: bool = False) -> bool:
    from polymarket_weather_arb.adapters.http_client import build_httpx_client

    client = build_httpx_client(timeout=10.0, settings=settings)
    try:
        response = client.get(url)
        if allow_4xx:
            return response.status_code < 500
        return response.status_code < 400
    finally:
        client.close()


def _default_weather_probe(settings: Settings) -> bool:
    """Lightweight Open-Meteo reachability check (no market trading)."""
    provider = (settings.weather_provider or "open-meteo").lower()
    if provider in {"open-meteo", "auto"}:
        return _http_get_ok(
            "https://api.open-meteo.com/v1/forecast?latitude=40.7&longitude=-74.0&current=temperature_2m",
            settings,
        )
    if provider == "noaa":
        return _http_get_ok("https://api.weather.gov/", settings, allow_4xx=True)
    # china-official / others: treat configured as local ok without hard-coded URL.
    return bool(settings.weather_provider)


def saved_setup_mode(paths: DesktopPaths | None, repository: Any | None = None) -> str:
    """Resume the last selected app mode from config.env or autopilot state."""
    if paths is not None:
        values = read_config_env(paths.config_env)
        raw = values.get("DESKTOP_APP_MODE") or values.get("APP_MODE")
        if raw:
            try:
                return normalize_setup_mode(raw)
            except ValueError:
                pass
    if repository is not None:
        try:
            state = repository.get_autopilot_state()
            if state is not None and state["app_mode"]:
                return normalize_setup_mode(str(state["app_mode"]))
        except Exception:  # noqa: BLE001
            pass
    return DEFAULT_SETUP_MODE


def saved_risk_preset(paths: DesktopPaths | None, settings: Settings) -> str:
    """Resume risk preset selection from config.env or infer from caps."""
    if paths is not None:
        values = read_config_env(paths.config_env)
        raw = (values.get("DESKTOP_RISK_PRESET") or "").strip().lower()
        if raw in RISK_PRESETS or raw == "custom":
            return raw
    # Infer known presets from current settings.
    order = str(settings.max_order_usdc)
    daily = str(settings.max_daily_usdc)
    market = str(settings.max_market_usdc)
    for name, preset in RISK_PRESETS.items():
        if (
            preset["MAX_ORDER_USDC"] == order
            and preset["MAX_DAILY_USDC"] == daily
            and preset["MAX_MARKET_USDC"] == market
        ):
            return name
    return "custom"


def app_mode_for_setup(settings: Settings, form_mode: str | None = None) -> str:
    if form_mode:
        return normalize_setup_mode(form_mode)
    return DEFAULT_APP_MODE if DEFAULT_APP_MODE in APP_MODES else "paper"
