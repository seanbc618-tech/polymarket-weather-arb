"""Phase 2: expanded /setup flow, CSRF, mode/risk mapping, secret hygiene."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from polymarket_weather_arb.adapters.keychain import KeychainStore
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.dashboard import (
    clear_desktop_runtime,
    configure_desktop_runtime,
    handle_dashboard_post,
    render_dashboard_path,
)
from polymarket_weather_arb.dashboard_ui.html import render_page
from polymarket_weather_arb.desktop.config_store import read_config_env
from polymarket_weather_arb.desktop.csrf import (
    csrf_token,
    require_sensitive_post,
    verify_csrf,
)
from polymarket_weather_arb.desktop.paths import is_setup_complete, resolve_desktop_paths
from polymarket_weather_arb.desktop.setup_flow import (
    apply_setup_config,
    build_setup_readiness,
    mode_to_settings_values,
    normalize_setup_mode,
    risk_preset_values,
)
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


class FakeKeychain(KeychainStore):
    def __init__(self) -> None:
        super().__init__(service="test")
        self._data: dict[str, str] = {}

    def get_secret(self, account: str) -> str | None:
        return self._data.get(account)

    def set_secret(self, account: str, value: str) -> None:
        self._data[account] = value

    def delete_secret(self, account: str, *, missing_ok: bool = False) -> bool:
        return self._data.pop(account, None) is not None


@pytest.fixture(autouse=True)
def _clear_desktop_runtime() -> None:
    clear_desktop_runtime()
    yield
    clear_desktop_runtime()


def _desktop_env(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv("POLYMARKET_DESKTOP", "1")
    monkeypatch.setenv("POLYMARKET_DESKTOP_DATA_ROOT", str(root))
    for key in (
        "POLYMARKET_PRIVATE_KEY",
        "TELEGRAM_BOT_TOKEN",
        "LLM_API_KEY",
        "DATABASE_PATH",
        "TRADING_DISABLED",
        "WEATHER_PROVIDER",
        "MAX_ORDER_USDC",
        "MAX_DAILY_USDC",
        "MAX_MARKET_USDC",
    ):
        monkeypatch.delenv(key, raising=False)


def _form(**fields: str) -> bytes:
    from urllib.parse import urlencode

    payload = {"csrf_token": csrf_token(), "lang": "en", **fields}
    return urlencode(payload).encode()


def test_mode_and_risk_preset_mapping() -> None:
    assert normalize_setup_mode("paper") == "paper"
    assert mode_to_settings_values("paper") == {"TRADING_DISABLED": "true"}
    assert mode_to_settings_values("micro_live") == {}
    starter = risk_preset_values("starter_live")
    assert starter["MAX_ORDER_USDC"] == "1"
    assert starter["AUTO_EXIT_MAX_POSITION_USDC"] == "1"
    cautious = risk_preset_values("cautious_live")
    assert cautious["MAX_ORDER_USDC"] == "2"
    assert Decimal_ge(cautious["AUTO_EXIT_MAX_POSITION_USDC"], cautious["MAX_ORDER_USDC"])
    custom = risk_preset_values(
        "custom",
        custom={
            "MAX_ORDER_USDC": "3",
            "MAX_DAILY_USDC": "9",
            "MAX_MARKET_USDC": "6",
            "AUTO_EXIT_MAX_POSITION_USDC": "1",  # too low — must be raised
            "TRADING_DISABLED": "false",
            "AUTO_EXIT_ENABLED": "true",
        },
    )
    assert custom["AUTO_EXIT_MAX_POSITION_USDC"] == "3"


def Decimal_ge(a: str, b: str) -> bool:
    from decimal import Decimal

    return Decimal(a) >= Decimal(b)


def test_setup_first_run_progression_and_no_live_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "desk"
    _desktop_env(monkeypatch, root)
    paths = resolve_desktop_paths(root=root)
    paths.ensure_layout()
    keychain = FakeKeychain()
    settings = Settings(_env_file=None, database_path=paths.database_path, trading_disabled=True)
    Database(paths.database_path).init_schema()
    configure_desktop_runtime(settings=settings, paths=paths, keychain=keychain)

    # Step health -> mode
    resp = handle_dashboard_post(
        settings,
        "/setup/health",
        _form(),
        host_header="127.0.0.1:8765",
    )
    assert resp.status.value == 303
    assert "step=mode" in resp.headers["Location"]

    resp = handle_dashboard_post(
        settings,
        "/setup/mode",
        _form(app_mode="paper"),
        host_header="127.0.0.1:8765",
    )
    assert "step=wallet" in resp.headers["Location"]

    secret = "0x" + "11" * 32
    resp = handle_dashboard_post(
        settings,
        "/setup/wallet",
        _form(
            polymarket_private_key=secret,
            derive_funder="1",
        ),
        host_header="127.0.0.1:8765",
    )
    assert "step=connectivity" in resp.headers["Location"]
    assert keychain.get_secret("POLYMARKET_PRIVATE_KEY") == secret
    cfg = read_config_env(paths.config_env)
    assert "POLYMARKET_PRIVATE_KEY" not in cfg
    assert secret not in paths.config_env.read_text(encoding="utf-8")

    resp = handle_dashboard_post(
        settings,
        "/setup/connectivity",
        _form(run_probes="0"),
        host_header="127.0.0.1:8765",
    )
    assert "step=risk" in resp.headers["Location"]

    resp = handle_dashboard_post(
        settings,
        "/setup/risk",
        _form(risk_preset="paper"),
        host_header="127.0.0.1:8765",
    )
    assert "step=weather" in resp.headers["Location"]

    resp = handle_dashboard_post(
        settings,
        "/setup/weather",
        _form(weather_provider="open-meteo"),
        host_header="127.0.0.1:8765",
    )
    assert "step=review" in resp.headers["Location"]

    resp = handle_dashboard_post(
        settings,
        "/setup/complete",
        _form(),
        host_header="127.0.0.1:8765",
    )
    assert resp.headers["Location"].startswith("/app")
    assert is_setup_complete(paths)

    # Autopilot must remain stopped after setup completion.
    connection = Database(paths.database_path).connect()
    try:
        state = Repository(connection).get_autopilot_state()
        assert state is not None
        assert not bool(state["enabled"])
        assert state["app_mode"] == "paper"
    finally:
        connection.close()


def test_csrf_rejection_and_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "desk"
    _desktop_env(monkeypatch, root)
    paths = resolve_desktop_paths(root=root)
    paths.ensure_layout()
    settings = Settings(_env_file=None, database_path=paths.database_path)
    Database(paths.database_path).init_schema()
    configure_desktop_runtime(settings=settings, paths=paths, keychain=FakeKeychain())

    bad = handle_dashboard_post(
        settings,
        "/setup/mode",
        b"lang=en&app_mode=paper&csrf_token=not-the-token",
        host_header="127.0.0.1:8765",
    )
    assert "level=error" in bad.headers["Location"] or "flash=flash.error" in bad.headers["Location"]

    good = handle_dashboard_post(
        settings,
        "/setup/mode",
        _form(app_mode="observe"),
        host_header="127.0.0.1:8765",
    )
    assert "level=error" not in good.headers["Location"]
    assert "step=wallet" in good.headers["Location"]


def test_public_dashboard_origin_requires_exact_https_origin() -> None:
    settings = Settings(
        _env_file=None,
        dashboard_public_origin="https://Weather.Example.com/",
    )
    assert settings.dashboard_public_origin == "https://weather.example.com"

    for invalid in (
        "http://weather.example.com",
        "https://weather.example.com/path",
        "https://weather.example.com:8443",
        "https://user@weather.example.com",
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, dashboard_public_origin=invalid)


def test_render_page_injects_csrf_into_every_post_form_only() -> None:
    page = render_page(
        "Security",
        '<form method="post" action="/one"></form>'
        '<form class="x" method="POST" action="/two"></form>'
        '<form method="get" action="/read"></form>',
        "en",
        "/app",
    )
    assert page.count('name="csrf_token"') == 2


def test_public_dashboard_post_requires_token_host_and_origin(tmp_path: Path) -> None:
    database_path = tmp_path / "public-dashboard.db"
    settings = Settings(
        _env_file=None,
        database_path=database_path,
        dashboard_public_origin="https://weather.example.com",
    )
    Database(database_path).init_schema()

    accepted = handle_dashboard_post(
        settings,
        "/app/toggle",
        _form(enabled="0"),
        host_header="weather.example.com",
        origin_header="https://weather.example.com",
    )
    assert accepted.status.value == 303
    assert "level=error" not in accepted.headers["Location"]

    for body, host, origin in (
        (b"enabled=0", "weather.example.com", "https://weather.example.com"),
        (_form(enabled="0"), "evil.example.com", "https://weather.example.com"),
        (_form(enabled="0"), "weather.example.com", None),
        (_form(enabled="0"), "weather.example.com", "https://weather.example.com.evil.test"),
    ):
        rejected = handle_dashboard_post(
            settings,
            "/app/toggle",
            body,
            host_header=host,
            origin_header=origin,
        )
        assert "level=error" in rejected.headers["Location"]


def test_local_origin_check_does_not_accept_hostname_prefix() -> None:
    with pytest.raises(ValueError, match="non-local Origin"):
        require_sensitive_post(
            {"csrf_token": csrf_token()},
            host_header="127.0.0.1:8765",
            origin_header="http://localhost.evil.test",
        )


def test_setup_html_never_leaks_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "desk"
    _desktop_env(monkeypatch, root)
    paths = resolve_desktop_paths(root=root)
    paths.ensure_layout()
    secret = "0x" + "cd" * 32
    keychain = FakeKeychain()
    keychain.set_secret("POLYMARKET_PRIVATE_KEY", secret)
    settings = Settings(_env_file=None, database_path=paths.database_path)
    Database(paths.database_path).init_schema()
    configure_desktop_runtime(settings=settings, paths=paths, keychain=keychain)

    page = render_dashboard_path(settings, "/setup?step=wallet&lang=en")
    assert page.status.value == 200
    assert secret not in page.body
    assert "type=\"password\"" in page.body or 'type="password"' in page.body
    assert "csrf_token" in page.body
    assert "configured" in page.body


def test_readiness_is_read_only_and_labels_kinds(tmp_path: Path) -> None:
    db = Database(tmp_path / "r.db")
    db.init_schema()
    connection = db.connect()
    try:
        repo = Repository(connection)
        settings = Settings(_env_file=None, database_path=db.path, trading_disabled=True)
        items = build_setup_readiness(
            settings,
            repository=repo,
            gamma_probe=lambda: True,
            clob_probe=lambda: True,
            weather_probe=lambda: True,
            compliance_probe=lambda: (True, "country=HK allowed"),
            run_network_probes=False,
        )
        names = {i.name for i in items}
        assert "database" in names
        assert "compliance" in names
        assert "resolution_circuit_breaker" in names or "circuit_breaker" in names or any(
            "circuit" in n for n in names
        )
        assert "order_signing_auth_path" in names
        signing = next(i for i in items if i.name == "order_signing_auth_path")
        assert signing.kind == "not_yet_verified"
        assert signing.status in {"missing", "not_yet_verified"}
        assert "trade" not in signing.detail.lower() or "not" in signing.detail.lower()
    finally:
        connection.close()


def test_setup_readiness_wires_authenticated_client_for_balance_and_signing(
    tmp_path: Path,
) -> None:
    """With live credentials, review must exercise auth paths via a real client object."""
    db = Database(tmp_path / "r.db")
    db.init_schema()
    connection = db.connect()

    class AuthClient:
        def __init__(self) -> None:
            self.balance_calls = 0
            self.signing_calls = 0

        def get_balances(self):
            self.balance_calls += 1
            return {"balance": "1.25"}

        def validate_order_signing(self):
            self.signing_calls += 1
            return {
                "ok": True,
                "status": "wallet-path-configured",
                "detail": "signing path ok (test)",
            }

    try:
        repo = Repository(connection)
        settings = Settings(
            _env_file=None,
            database_path=db.path,
            polymarket_private_key="0x" + "11" * 32,
            polymarket_funder="0x" + "22" * 20,
            trading_disabled=True,
        )
        client = AuthClient()
        items = build_setup_readiness(
            settings,
            repository=repo,
            client=client,
            gamma_probe=lambda: True,
            clob_probe=lambda: True,
            weather_probe=lambda: True,
            compliance_probe=lambda: (True, "country=HK allowed"),
            run_network_probes=False,
        )
        assert client.balance_calls == 1
        assert client.signing_calls == 1
        balance = next(i for i in items if i.name == "balance_auth_path")
        signing = next(i for i in items if i.name == "order_signing_auth_path")
        assert balance.ok is True
        assert balance.kind == "read"
        assert signing.ok is True
        assert signing.kind == "read"
        assert "trade" not in signing.detail.lower() or "not" in signing.detail.lower()
    finally:
        connection.close()


def test_build_setup_auth_client_requires_credentials() -> None:
    from polymarket_weather_arb.desktop.setup_flow import build_setup_auth_client

    client, err = build_setup_auth_client(Settings(_env_file=None))
    assert client is None
    assert err is not None
    assert "POLYMARKET" in err


def test_blank_secret_keeps_existing(tmp_path: Path) -> None:
    paths = resolve_desktop_paths(root=tmp_path / "desk")
    paths.ensure_layout()
    keychain = FakeKeychain()
    keychain.set_secret("POLYMARKET_PRIVATE_KEY", "keep-me")
    apply_setup_config(
        paths,
        {"WEATHER_PROVIDER": "open-meteo"},
        secrets={"POLYMARKET_PRIVATE_KEY": ""},  # blank = keep
        keychain=keychain,
    )
    assert keychain.get_secret("POLYMARKET_PRIVATE_KEY") == "keep-me"


def test_verify_csrf_helper() -> None:
    token = csrf_token()
    assert verify_csrf(token)
    assert not verify_csrf("nope")
    assert not verify_csrf(None)


def test_setup_resume_step_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "desk"
    _desktop_env(monkeypatch, root)
    paths = resolve_desktop_paths(root=root)
    paths.ensure_layout()
    settings = Settings(_env_file=None, database_path=paths.database_path)
    Database(paths.database_path).init_schema()
    configure_desktop_runtime(settings=settings, paths=paths, keychain=FakeKeychain())
    page = render_dashboard_path(settings, "/setup?step=risk&lang=en")
    assert "MAX_ORDER_USDC" in page.body
    assert "risk_preset" in page.body or "Starter" in page.body or "starter" in page.body.lower()


def test_setup_resumes_saved_mode_and_risk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "desk"
    _desktop_env(monkeypatch, root)
    paths = resolve_desktop_paths(root=root)
    paths.ensure_layout()
    from polymarket_weather_arb.desktop.config_store import write_config_env

    write_config_env(
        paths.config_env,
        {
            "DATABASE_PATH": str(paths.database_path),
            "DESKTOP_APP_MODE": "observe",
            "DESKTOP_RISK_PRESET": "cautious_live",
            "MAX_ORDER_USDC": "2",
            "MAX_DAILY_USDC": "10",
            "MAX_MARKET_USDC": "5",
            "WEATHER_PROVIDER": "open-meteo",
        },
    )
    settings = Settings(_env_file=None, database_path=paths.database_path, max_order_usdc="2")
    Database(paths.database_path).init_schema()
    configure_desktop_runtime(settings=settings, paths=paths, keychain=FakeKeychain())
    mode_page = render_dashboard_path(settings, "/setup?step=mode&lang=en")
    assert 'value="observe" checked' in mode_page.body or 'value="observe"  checked' in mode_page.body
    risk_page = render_dashboard_path(settings, "/setup?step=risk&lang=en")
    assert 'value="cautious_live" checked' in risk_page.body
