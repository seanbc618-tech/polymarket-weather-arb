from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from decimal import Decimal
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from sqlite3 import OperationalError, Row
from typing import Callable
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from polymarket_weather_arb.adapters.polymarket.client import GammaPolymarketClient
from polymarket_weather_arb.adapters.weather.noaa import NoaaProvider
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.rules import parse_resolution_rule
from polymarket_weather_arb.modules.registry import get_module
from polymarket_weather_arb.profiles import get_profile
from polymarket_weather_arb.services.automation_service import (
    AutomationService,
    normalize_kind,
    validate_action_id,
)
from polymarket_weather_arb.services.china_bucket_discovery_service import (
    ChinaBucketDiscoveryOptions,
    ChinaTemperatureBucketDiscoveryService,
)
from polymarket_weather_arb.services.discovery_service import DiscoveryService
from polymarket_weather_arb.services.fixture_service import load_market_fixture
from polymarket_weather_arb.services.live_launchpad_service import (
    build_live_launchpad_snapshot,
    live_market_ids_from_settings,
)
from polymarket_weather_arb.services.market_workflow_service import MarketWorkflowService
from polymarket_weather_arb.services.operator_daemon import OperatorDaemon
from polymarket_weather_arb.services.module_workflows import resolve_module_workflow
from polymarket_weather_arb.services.order_lifecycle_service import OrderLifecycleService
from polymarket_weather_arb.services.reconciliation_service import ReconciliationService
from polymarket_weather_arb.services.settlement_service import SettlementService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository
from polymarket_weather_arb.services.circuit_breaker_service import CircuitBreakerService
from polymarket_weather_arb.dashboard_ui.i18n import _t
from polymarket_weather_arb.dashboard_ui.html import (
    _e,
    _href,
    _single,
    _table as _table,
    render_page,
)
from polymarket_weather_arb.dashboard_ui.overview import (
    _cockpit_action_label as _cockpit_action_label,
    _cockpit_actions as _cockpit_actions,
    _cockpit_blockers as _cockpit_blockers,
    _cockpit_label as _cockpit_label,
    _cockpit_mode_card as _cockpit_mode_card,
    _cockpit_next_action_card as _cockpit_next_action_card,
    _cockpit_pipeline as _cockpit_pipeline,
    _cockpit_runs as _cockpit_runs,
    _cockpit_step_label as _cockpit_step_label,
    _cockpit_top_candidates as _cockpit_top_candidates,
    _reconciliation_card as _reconciliation_card,
    _render_market_readiness as _render_market_readiness,
    _workbench_label as _workbench_label,
    render_overview as render_overview,
)
from polymarket_weather_arb.dashboard_ui.exchange import (
    render_fills as render_fills,
    render_open_orders as render_open_orders,
    render_positions as render_positions,
    render_reconciliation as render_reconciliation,
    render_risk as render_risk,
)
from polymarket_weather_arb.dashboard_ui.markets import (
    CANDIDATE_STATUSES as CANDIDATE_STATUSES,
    _module_label as _module_label,
    _orders_table as _orders_table,
    _valid_module_filter as _valid_module_filter,
    render_candidates as render_candidates,
    render_market_detail as render_market_detail,
    render_markets as render_markets,
)
from polymarket_weather_arb.dashboard_ui.automation import (
    _action_controls as _action_controls,
    _counts_card as _counts_card,
    render_action_detail as render_action_detail,
    render_actions as render_actions,
    render_operator as render_operator,
    render_overrides as render_overrides,
    render_runs as render_runs,
)
from polymarket_weather_arb.dashboard_ui.app import render_app
from polymarket_weather_arb.dashboard_ui.stream_panel import build_app_stream_payload
from polymarket_weather_arb.dashboard_ui.beginner import render_beginner
from polymarket_weather_arb.dashboard_ui.calibration import render_calibration
from polymarket_weather_arb.dashboard_ui.live import render_live_launchpad
from polymarket_weather_arb.dashboard_ui.admin import (
    _resolve_fixture_path as _resolve_fixture_path,
    render_discovery as render_discovery,
    render_doctor as render_doctor,
    render_fixtures as render_fixtures,
    render_modules as render_modules,
    render_orders as render_orders,
    render_profiles as render_profiles,
    render_setup as render_setup,
)

SUPPORTED_LANGS = {"zh", "en"}
DEFAULT_LANG = "zh"
LANG_COOKIE = "pwa_lang"
NON_LIVE_UI_KINDS = {"dry_run", "refresh_weather", "analyze"}
logger = logging.getLogger(__name__)


class DashboardResponse:
    def __init__(
        self, status: HTTPStatus, body: str, headers: dict[str, str] | None = None
    ) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {}


class DashboardError(Exception):
    def __init__(self, message_key: str, detail: str | None = None) -> None:
        self.message_key = message_key
        self.detail = detail
        super().__init__(detail or message_key)


# Process-scoped notifier for /app Autopilot (background + manual tick).
# Set by serve_dashboard(); None when Telegram is not configured.
_APP_TELEGRAM_NOTIFIER: object | None = None

# Optional desktop runtime (set by the macOS launcher). Holds mutable settings
# so /setup can reload config without restarting the HTTP server.
_DESKTOP_RUNTIME: dict[str, object] = {}


def configure_desktop_runtime(
    *,
    settings: Settings,
    paths: object | None = None,
    keychain: object | None = None,
    shutdown: Callable[[], None] | None = None,
) -> None:
    """Register desktop composition-root handles for setup/lifecycle routes."""
    _DESKTOP_RUNTIME.clear()
    _DESKTOP_RUNTIME["enabled"] = True
    _DESKTOP_RUNTIME["settings"] = settings
    if paths is not None:
        _DESKTOP_RUNTIME["paths"] = paths
    if keychain is not None:
        _DESKTOP_RUNTIME["keychain"] = keychain
    if shutdown is not None:
        _DESKTOP_RUNTIME["shutdown"] = shutdown


def clear_desktop_runtime() -> None:
    """Drop desktop process state (tests and clean shutdown)."""
    _DESKTOP_RUNTIME.clear()


def active_dashboard_settings(fallback: Settings) -> Settings:
    if not _DESKTOP_RUNTIME.get("enabled"):
        return fallback
    current = _DESKTOP_RUNTIME.get("settings")
    return current if isinstance(current, Settings) else fallback


def _replace_dashboard_settings(settings: Settings) -> None:
    if _DESKTOP_RUNTIME.get("enabled"):
        _DESKTOP_RUNTIME["settings"] = settings
        # Keep process-global notifier aligned with the latest Settings so
        # Autopilot ticks and /app actions pick up Telegram/wallet/risk changes.
        global _APP_TELEGRAM_NOTIFIER
        _APP_TELEGRAM_NOTIFIER = build_app_telegram_notifier(settings)


def build_app_telegram_notifier(settings: Settings) -> object | None:
    """Create one TelegramNotifier when env is armed; else None."""
    if not settings.telegram_notify_ready():
        return None
    from polymarket_weather_arb.services.telegram_notifier import TelegramNotifier

    return TelegramNotifier(settings)


def _run_autopilot_background(
    database: Database,
    settings: Settings,
    notifier: object | None,
    *,
    tick_seconds: int,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    max_cycles: int | None = None,
    use_pulse: bool = True,
) -> None:
    """Run dashboard Autopilot without letting one DB/API failure kill the thread.

    Each cycle resolves the current Settings (desktop runtime may replace them
    after /setup writes) so wallet/risk/proxy/Telegram changes apply without a
    process restart.

    Owns one shared ``GammaPolymarketClient`` across enabled cycles while Settings
    identity is unchanged. Fresh SQLite connections/repositories are still opened
    per cycle. Disabled cycles never create an authenticated SDK client.

    Phase 2 multi-cadence: when ``use_pulse`` is True (default), wakes about every
    ``PULSE_SECONDS`` and runs at most one work class via ``AutopilotService.pulse``.
    ``tick_seconds`` remains the strategy cruise interval stored in autopilot_state
    for slow discovery/weather budgets; it is not the wake interval.
    Set ``use_pulse=False`` to restore legacy full ``tick()`` every ``tick_seconds``
    (tests that characterize the old cadence).
    """
    from polymarket_weather_arb.adapters.polymarket.stream import PolymarketStreamBridge
    from polymarket_weather_arb.services.autopilot_service import (
        PULSE_SECONDS,
        AutopilotPulseState,
        AutopilotService,
        remaining_cycle_delay,
    )

    shared_client: GammaPolymarketClient | None = None
    shared_settings: Settings | None = None
    stream_bridge: PolymarketStreamBridge | None = None
    # Process-memory cadence/queue; restart rebuilds from SQLite candidates.
    pulse_state = AutopilotPulseState()
    cycles = 0
    try:
        while max_cycles is None or cycles < max_cycles:
            cycle_started = monotonic()
            connection = None
            try:
                current_settings = active_dashboard_settings(settings)
                # Desktop runtime refreshes notifier on settings replace; CLI keeps bootstrap.
                current_notifier = (
                    _APP_TELEGRAM_NOTIFIER if _DESKTOP_RUNTIME.get("enabled") else notifier
                )
                # Settings object identity changes after desktop setup writes.
                if shared_client is not None and shared_settings is not current_settings:
                    shared_client.close()
                    shared_client = None
                    shared_settings = None
                    if stream_bridge is not None:
                        stream_bridge.stop()
                        stream_bridge = None
                connection = database.connect()
                repository = Repository(connection)
                state = repository.get_autopilot_state()
                if state is not None and bool(state["enabled"]):
                    if shared_client is None:
                        shared_client = GammaPolymarketClient(current_settings)
                        shared_settings = current_settings
                    if stream_bridge is not None and not current_settings.polymarket_market_stream_enabled:
                        stream_bridge.stop()
                        stream_bridge = None
                    if (
                        use_pulse
                        and current_settings.polymarket_market_stream_enabled
                        and stream_bridge is None
                    ):
                        # Exchange WS is optional optimization; failures stay degraded.
                        try:
                            pk = current_settings.polymarket_private_key
                            funder = current_settings.polymarket_funder
                            api_credentials = (
                                shared_client.stream_api_credentials() if pk and funder else None
                            )
                            stream_bridge = PolymarketStreamBridge(
                                private_key=pk,
                                funder=funder,
                                api_credentials=api_credentials,
                                enable_user_channel=bool(pk and funder),
                            )
                            stream_bridge.start()
                        except Exception:
                            logger.exception(
                                "exchange stream bridge failed to start; REST fallback active"
                            )
                            stream_bridge = None
                    # Keep strategy cruise interval visible on state for UI/ops.
                    state_tick = int(state["tick_seconds"] or tick_seconds or 300)
                    if state_tick != tick_seconds and tick_seconds > 0:
                        repository.update_autopilot_state(tick_seconds=tick_seconds)
                    service = AutopilotService(
                        current_settings,
                        repository,
                        client=shared_client,
                        notifier=current_notifier,  # type: ignore[arg-type]
                    )
                    if use_pulse:
                        service.pulse(
                            pulse_state,
                            monotonic=monotonic,
                            stream_bridge=stream_bridge,
                        )
                    else:
                        service.tick()
                    connection.commit()
                else:
                    # Autopilot disabled: close the exchange stream if any.
                    if stream_bridge is not None:
                        stream_bridge.stop()
                        stream_bridge = None
            except Exception:
                if connection is not None:
                    connection.rollback()
                logger.exception("Autopilot background cycle failed; retrying on the next interval")
            finally:
                if connection is not None:
                    connection.close()
            cycles += 1
            if max_cycles is None or cycles < max_cycles:
                wake_seconds = PULSE_SECONDS if use_pulse else tick_seconds
                sleep(
                    remaining_cycle_delay(
                        tick_seconds=wake_seconds,
                        cycle_started=cycle_started,
                        monotonic=monotonic,
                    )
                )
    finally:
        if stream_bridge is not None:
            try:
                stream_bridge.stop()
            except Exception:
                logger.exception("exchange stream bridge stop failed")
            stream_bridge = None
            # Mark exchange stream stopped for /app cross-thread readers.
            try:
                from polymarket_weather_arb.services.autopilot_service import _now_iso

                conn = database.connect()
                Repository(conn).update_autopilot_state(
                    exchange_stream_status="disabled",
                    exchange_stream_updated_at=_now_iso(),
                    exchange_stream_detail=(
                        '{"subscribed_token_count":0,"rest_fallback_active":true,'
                        '"local_transport":"sqlite","stopped":true}'
                    ),
                )
                conn.commit()
                conn.close()
            except Exception:
                logger.exception("failed to reset exchange stream health on shutdown")
        if shared_client is not None:
            shared_client.close()
            shared_client = None
            shared_settings = None


def serve_dashboard(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    autopilot_tick_seconds: int | None = None,
) -> None:
    import threading

    from polymarket_weather_arb.logging_config import setup_persistent_logging
    from polymarket_weather_arb.services.autopilot_service import _now_iso

    global _APP_TELEGRAM_NOTIFIER
    # One notifier (and HTTP client path) for the whole dashboard process.
    _APP_TELEGRAM_NOTIFIER = build_app_telegram_notifier(settings)

    database = Database(settings.database_path)
    database.init_schema()
    setup_persistent_logging(Path(settings.database_path).parent)

    if autopilot_tick_seconds is not None:
        # Mark process liveness for /app (same field OperatorDaemon writes).
        try:
            connection = database.connect()
            Repository(connection).update_autopilot_state(process_started_at=_now_iso())
            connection.commit()
            connection.close()
        except Exception:
            logger.exception("failed to record process_started_at for dashboard autopilot")
        threading.Thread(
            target=_run_autopilot_background,
            kwargs={
                "database": database,
                "settings": settings,
                "notifier": _APP_TELEGRAM_NOTIFIER,
                "tick_seconds": autopilot_tick_seconds,
            },
            daemon=True,
            name="autopilot-background",
        ).start()

    def handler_factory(*args, **kwargs):
        DashboardHandler(settings, *args, **kwargs)

    server = ThreadingHTTPServer((host, port), handler_factory)
    print(f"Dashboard listening at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


class DashboardHandler(BaseHTTPRequestHandler):
    def __init__(self, settings: Settings, *args, **kwargs) -> None:
        self._bootstrap_settings = settings
        super().__init__(*args, **kwargs)

    @property
    def settings(self) -> Settings:
        return active_dashboard_settings(self._bootstrap_settings)

    def do_GET(self) -> None:
        try:
            response = render_dashboard_path(self.settings, self.path, self.headers.get("Cookie"))
            if response.status == HTTPStatus.OK and "<main>" in response.body:
                database = Database(self.settings.database_path)
                connection = database.connect()
                try:
                    repository = Repository(connection)
                    cb = CircuitBreakerService(repository).status()
                    if cb.tripped:
                        alert = f'<div class="flash flash-error" style="margin-bottom: 20px;"><strong>Fail-Safe Mode Active:</strong> Circuit Breaker Tripped - Live operations are hard-blocked. Reason: {cb.reason}</div>'
                        response.body = response.body.replace("<main>", f"<main>{alert}", 1)
                finally:
                    connection.close()
        except OperationalError:
            logger.exception("Dashboard database read failed")
            response = DashboardResponse(
                HTTPStatus.SERVICE_UNAVAILABLE,
                render_page(
                    "Dashboard temporarily unavailable",
                    "<p>The local database could not be opened. Please retry shortly.</p>",
                    DEFAULT_LANG,
                    self.path,
                ),
            )
        self._send_dashboard_response(response)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        try:
            response = handle_dashboard_post(
                self.settings,
                self.path,
                body,
                self.headers.get("Cookie"),
                host_header=self.headers.get("Host"),
                origin_header=self.headers.get("Origin"),
            )
        except OperationalError:
            logger.exception("Dashboard database write failed")
            response = DashboardResponse(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Database temporarily unavailable; no action was applied.",
                {"Content-Type": "text/plain; charset=utf-8"},
            )
        self._send_dashboard_response(response)

    def _send_dashboard_response(self, response: DashboardResponse) -> None:
        try:
            encoded = response.body.encode("utf-8")
            self.send_response(response.status.value)
            headers = dict(response.headers)
            has_content_type = any(key.lower() == "content-type" for key in headers)
            if not has_content_type:
                headers["Content-Type"] = "text/html; charset=utf-8"
            if not any(key.lower() == "cache-control" for key in headers):
                headers["Cache-Control"] = "no-store, max-age=0"
            if not any(key.lower() == "pragma" for key in headers):
                headers["Pragma"] = "no-cache"
            if not any(key.lower() == "expires" for key in headers):
                headers["Expires"] = "0"
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Browsers routinely cancel superseded stream polls or page loads.
            # The response is read-only, so a disconnected client needs no retry.
            logger.debug("Dashboard client disconnected before response completed")

    def log_message(self, format: str, *args) -> None:
        return


def render_dashboard_path(
    settings: Settings, raw_path: str, cookie_header: str | None = None
) -> DashboardResponse:
    parsed = urlparse(raw_path)
    query = parse_qs(parsed.query)
    alias = urlparse(_module_alias_path(parsed.path))
    route_path = alias.path
    alias_query = parse_qs(alias.query)
    query = {**alias_query, **query}
    lang = _lang_from_request(query, cookie_header)
    headers = _language_headers(query, lang)
    database = Database(settings.database_path)
    connection = database.connect()
    current_path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    try:
        repository = Repository(connection)
        if route_path == "/app":
            return DashboardResponse(
                HTTPStatus.OK, render_app(repository, settings, lang, current_path), headers
            )
        if route_path == "/app/stream":
            return _render_app_stream(repository, query, headers)
        if route_path in ("/beginner", "/"):
            return DashboardResponse(
                HTTPStatus.FOUND,
                "",
                {**headers, "Location": _href("/app", lang)},
            )
        if route_path == "/beginner-legacy":
            return DashboardResponse(
                HTTPStatus.OK, render_beginner(repository, settings, lang, current_path), headers
            )
        if route_path == "/live":
            preview_market_id = _single(query, "preview_market_id")
            snapshot = build_live_launchpad_snapshot(
                repository,
                settings,
                check_exchange=False,
                live_market_ids=live_market_ids_from_settings(settings),
                preview_market_id=preview_market_id,
            )
            return DashboardResponse(
                HTTPStatus.OK,
                render_live_launchpad(snapshot, lang, current_path, query),
                headers,
            )
        if route_path == "/calibration":
            return DashboardResponse(
                HTTPStatus.OK,
                render_calibration(repository, lang, current_path, query),
                headers,
            )
        if route_path == "/overview-legacy":
            return DashboardResponse(
                HTTPStatus.OK, render_overview(repository, settings, lang, current_path), headers
            )
        if route_path == "/actions":
            status = _single(query, "status")
            kind = _single(query, "kind")
            return DashboardResponse(
                HTTPStatus.OK,
                render_actions(
                    repository, lang, current_path, status=status, kind=kind, query=query
                ),
                headers,
            )
        if route_path.startswith("/actions/"):
            action_id = unquote(route_path.removeprefix("/actions/"))
            try:
                validate_action_id(action_id)
            except ValueError:
                return DashboardResponse(
                    HTTPStatus.NOT_FOUND,
                    render_page(
                        _t(lang, "error.not_found"),
                        f"<p>{_t(lang, 'error.unknown_action')}</p>",
                        lang,
                        current_path,
                    ),
                    headers,
                )
            action = repository.get_automation_action(action_id)
            if action is None:
                return DashboardResponse(
                    HTTPStatus.NOT_FOUND,
                    render_page(
                        _t(lang, "error.not_found"),
                        f"<p>{_t(lang, 'error.unknown_action')}</p>",
                        lang,
                        current_path,
                    ),
                    headers,
                )
            events = repository.list_automation_audit_events(action_id)
            return DashboardResponse(
                HTTPStatus.OK,
                render_action_detail(action, events, lang, current_path, query),
                headers,
            )
        if route_path == "/runs":
            return DashboardResponse(
                HTTPStatus.OK,
                render_runs(repository.list_recent_runs(limit=50), lang, current_path),
                headers,
            )
        if route_path == "/open-orders":
            return DashboardResponse(
                HTTPStatus.OK, render_open_orders(repository, lang, current_path), headers
            )
        if route_path == "/positions":
            return DashboardResponse(
                HTTPStatus.OK, render_positions(repository, lang, current_path), headers
            )
        if route_path == "/fills":
            return DashboardResponse(
                HTTPStatus.OK, render_fills(repository, lang, current_path), headers
            )
        if route_path == "/discovery":
            return DashboardResponse(
                HTTPStatus.OK, render_discovery(lang, current_path, query), headers
            )
        if route_path == "/markets":
            module_id = _valid_module_filter(_single(query, "module"))
            return DashboardResponse(
                HTTPStatus.OK,
                render_markets(repository, lang, current_path, module_id=module_id),
                headers,
            )
        if route_path.startswith("/markets/"):
            market_id = unquote(route_path.removeprefix("/markets/"))
            market = repository.get_market(market_id)
            if market is None:
                return DashboardResponse(
                    HTTPStatus.NOT_FOUND,
                    render_page(
                        _t(lang, "error.not_found"),
                        f"<p>{_t(lang, 'error.unknown_market')}</p>",
                        lang,
                        current_path,
                    ),
                    headers,
                )
            return DashboardResponse(
                HTTPStatus.OK,
                render_market_detail(repository, market, lang, current_path, query),
                headers,
            )
        if route_path == "/candidates":
            status = _single(query, "status")
            module_id = _valid_module_filter(_single(query, "module"))
            return DashboardResponse(
                HTTPStatus.OK,
                render_candidates(
                    repository, lang, current_path, status=status, module_id=module_id, query=query
                ),
                headers,
            )
        if route_path == "/orders":
            return DashboardResponse(
                HTTPStatus.OK, render_orders(repository, lang, current_path), headers
            )
        if route_path == "/risk":
            return DashboardResponse(
                HTTPStatus.OK, render_risk(repository, lang, current_path), headers
            )
        if route_path == "/reconciliation":
            return DashboardResponse(
                HTTPStatus.OK, render_reconciliation(repository, lang, current_path, query), headers
            )
        if route_path == "/profiles":
            return DashboardResponse(HTTPStatus.OK, render_profiles(lang, current_path), headers)
        if route_path == "/doctor":
            return DashboardResponse(
                HTTPStatus.OK, render_doctor(settings, lang, current_path), headers
            )
        if route_path == "/fixtures":
            return DashboardResponse(
                HTTPStatus.OK, render_fixtures(lang, current_path, query), headers
            )
        if route_path == "/operator":
            return DashboardResponse(
                HTTPStatus.OK, render_operator(repository, lang, current_path, query), headers
            )
        if route_path == "/modules":
            return DashboardResponse(HTTPStatus.OK, render_modules(lang, current_path), headers)
        if route_path == "/setup":
            keychain = _DESKTOP_RUNTIME.get("keychain")
            return DashboardResponse(
                HTTPStatus.OK,
                render_setup(
                    settings,
                    lang,
                    current_path,
                    query,
                    repository=repository,
                    keychain=keychain,
                ),
                headers,
            )
        if route_path == "/overrides":
            return DashboardResponse(
                HTTPStatus.OK, render_overrides(repository, lang, current_path, query), headers
            )
        return DashboardResponse(
            HTTPStatus.NOT_FOUND,
            render_page(
                _t(lang, "error.not_found"),
                f"<p>{_t(lang, 'error.unknown_page')}</p>",
                lang,
                current_path,
            ),
            headers,
        )
    finally:
        connection.close()


def _parse_stream_cursor(query: dict[str, list[str]], key: str) -> int:
    raw = _single(query, key)
    if raw is None or raw == "":
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _render_app_stream(
    repository: Repository,
    query: dict[str, list[str]],
    headers: dict[str, str],
) -> DashboardResponse:
    """Read-only local SQLite delta for /app stream polling (no upstream I/O)."""
    payload = build_app_stream_payload(
        repository,
        after_decision_id=_parse_stream_cursor(query, "after_decision_id"),
        after_fill_id=_parse_stream_cursor(query, "after_fill_id"),
        after_intent_id=_parse_stream_cursor(query, "after_intent_id"),
        after_attempt_id=_parse_stream_cursor(query, "after_attempt_id"),
        after_analysis_id=_parse_stream_cursor(query, "after_analysis_id"),
    )
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return DashboardResponse(
        HTTPStatus.OK,
        body,
        {
            **headers,
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store, max-age=0",
        },
    )


def handle_dashboard_post(
    settings: Settings,
    raw_path: str,
    body: bytes,
    cookie_header: str | None = None,
    automation_service_factory: Callable[[Repository], AutomationService] | None = None,
    discovery_service_factory: Callable[[Repository], DiscoveryService] | None = None,
    market_workflow_service_factory: Callable[[Repository], MarketWorkflowService] | None = None,
    reconciliation_service_factory: Callable[[Repository], ReconciliationService] | None = None,
    order_lifecycle_service_factory: Callable[[Repository], OrderLifecycleService] | None = None,
    operator_daemon_factory: Callable[[Repository, object], OperatorDaemon] | None = None,
    settlement_service_factory: Callable[[Repository], SettlementService] | None = None,
    autopilot_service_factory: Callable[[Repository], object] | None = None,
    host_header: str | None = None,
    origin_header: str | None = None,
) -> DashboardResponse:
    parsed = urlparse(raw_path)
    query = parse_qs(parsed.query)
    form = _parse_form(body)
    lang = _lang_from_form(form, query, cookie_header)
    headers = _language_headers({"lang": [lang]}, lang)
    settings = active_dashboard_settings(settings)
    database = Database(settings.database_path)
    connection = database.connect()
    repository = Repository(connection)
    service = (
        automation_service_factory(repository)
        if automation_service_factory
        else AutomationService(repository)
    )
    try:
        if settings.dashboard_public_origin:
            from polymarket_weather_arb.desktop.csrf import require_sensitive_post

            require_sensitive_post(
                form,
                host_header=host_header,
                origin_header=origin_header,
                allowed_public_origin=settings.dashboard_public_origin,
            )
        location, flash = _handle_dashboard_post(
            settings,
            repository,
            service,
            parsed.path,
            form,
            lang,
            discovery_service_factory=discovery_service_factory,
            market_workflow_service_factory=market_workflow_service_factory,
            reconciliation_service_factory=reconciliation_service_factory,
            order_lifecycle_service_factory=order_lifecycle_service_factory,
            operator_daemon_factory=operator_daemon_factory,
            settlement_service_factory=settlement_service_factory,
            autopilot_service_factory=autopilot_service_factory,
            host_header=host_header,
            origin_header=origin_header,
        )
        connection.commit()
        return _redirect(location, lang, flash, headers=headers)
    except (DashboardError, ValueError) as exc:
        connection.rollback()
        key = exc.message_key if isinstance(exc, DashboardError) else "flash.error"
        detail = exc.detail if isinstance(exc, DashboardError) else str(exc)
        # Never put secret material into flash/query redirects.
        from polymarket_weather_arb.logging_config import redact_text

        detail = redact_text(detail) if detail else detail
        return _redirect(
            _fallback_post_location(parsed.path),
            lang,
            key,
            level="error",
            detail=detail,
            headers=headers,
        )
    finally:
        connection.close()


def _autopilot_service(
    settings: Settings,
    repository: Repository,
    autopilot_service_factory: Callable[[Repository], object] | None,
):
    if autopilot_service_factory is not None:
        return autopilot_service_factory(repository)
    from polymarket_weather_arb.services.autopilot_service import AutopilotService

    current = active_dashboard_settings(settings)
    return AutopilotService(
        current,
        repository,
        notifier=_APP_TELEGRAM_NOTIFIER,  # type: ignore[arg-type]
    )


def _handle_dashboard_post(
    settings: Settings,
    repository: Repository,
    service: AutomationService,
    path: str,
    form: dict[str, str],
    lang: str,
    *,
    discovery_service_factory: Callable[[Repository], DiscoveryService] | None = None,
    market_workflow_service_factory: Callable[[Repository], MarketWorkflowService] | None = None,
    reconciliation_service_factory: Callable[[Repository], ReconciliationService] | None = None,
    order_lifecycle_service_factory: Callable[[Repository], OrderLifecycleService] | None = None,
    operator_daemon_factory: Callable[[Repository, object], OperatorDaemon] | None = None,
    settlement_service_factory: Callable[[Repository], SettlementService] | None = None,
    autopilot_service_factory: Callable[[Repository], object] | None = None,
    host_header: str | None = None,
    origin_header: str | None = None,
) -> tuple[str, str]:
    if path.startswith("/setup/") or path in {"/desktop/quit", "/desktop/pause"}:
        return _handle_setup_post(
            settings,
            repository,
            path,
            form,
            lang,
            host_header=host_header,
            origin_header=origin_header,
            autopilot_service_factory=autopilot_service_factory,
        )
    if path == "/app/toggle":
        enabled = form.get("enabled", "0") == "1"
        repository.update_autopilot_state(enabled=enabled)
        if enabled:
            return "/app", "flash.autopilot_toggled"
        return "/app", "flash.autopilot_paused"
    if path == "/app/mode":
        app_mode = form.get("app_mode", "")
        _autopilot_service(settings, repository, autopilot_service_factory).set_app_mode(app_mode)
        return "/app", "flash.autopilot_mode_saved"
    if path == "/app/tick":
        _autopilot_service(settings, repository, autopilot_service_factory).tick()
        return "/app", "flash.autopilot_tick_complete"
    if path == "/app/reset-history":
        repository.clear_autopilot_history()
        return "/app", "flash.autopilot_history_cleared"
    if path == "/beginner/rehearse":
        market_id = load_market_fixture(
            Path(__file__).resolve().parents[2]
            / "fixtures"
            / "markets"
            / "demo-weather-nyc-high-2026-05-08.json",
            repository,
            settings,
            demo_analysis=True,
        )
        workflow = resolve_module_workflow(settings, repository, module_id="weather")
        workflow.dry_run(market_id)
        return "/beginner", "flash.beginner_rehearsal_complete"
    if path == "/beginner/safe-tick":
        profile = get_profile("dry-run-demo")
        daemon = OperatorDaemon(
            repository=repository,
            client=GammaPolymarketClient(settings),
            profile=profile,
            settings=settings,
            dry_run_only=True,
            allow_live_auto=False,
            block_live_on_positions=True,
        )
        result = daemon.tick(
            discover=False,
            propose=True,
            auto_dry_run=True,
            risk_guard=True,
        )
        return "/beginner", "flash.beginner_safe_tick_complete"
    if path == "/live/refresh":
        reconciliation = (
            reconciliation_service_factory(repository)
            if reconciliation_service_factory
            else ReconciliationService(GammaPolymarketClient(settings), repository)
        )
        reconciliation.reconcile()
        return "/live", "flash.live_refreshed"
    if path == "/live/preview":
        market_id = form.get("market_id", "").strip()
        if not market_id:
            raise DashboardError("error.live_market_required")
        return f"/live?preview_market_id={quote(market_id)}", "flash.live_preview_ready"
    if path == "/live/override":
        market_id = form.get("market_id", "").strip()
        if not market_id:
            raise DashboardError("error.live_market_required")
        if repository.get_market(market_id) is None:
            raise DashboardError("error.unknown_market")
        repository.upsert_strategy_override(
            market_id=market_id,
            profile="micro-live",
            max_order_usdc=form.get("max_order_usdc", "").strip() or "2",
            live_auto_enabled=True,
            notes="Live Launchpad micro-live override",
        )
        return "/live", "flash.live_override_saved"
    if path == "/live/propose":
        market_id = form.get("market_id", "").strip()
        confirmation = form.get("confirmation", "").strip()
        ack = form.get("ack") == "true"
        if not market_id:
            raise DashboardError("error.live_market_required")
        if not ack or confirmation != "LIVE 2 USDC":
            raise DashboardError("error.live_confirmation_required")
        snapshot = build_live_launchpad_snapshot(
            repository,
            settings,
            check_exchange=False,
            live_market_ids=live_market_ids_from_settings(settings),
            preview_market_id=market_id,
        )
        if snapshot.preview is None or not snapshot.preview.accepted:
            raise DashboardError("error.live_preview_blocked")
        service.propose(
            kind="trade_live",
            market_id=market_id,
            reason="Live Launchpad pending micro-live proposal",
            ttl_minutes=10,
            requested_by="live-launchpad",
        )
        return "/live", "flash.live_action_proposed"
    if path == "/live/execute":
        raise DashboardError("error.live_execution_locked")
    if path == "/live/cancel-stale-orders":
        settings.ensure_live_trading_ready()
        lifecycle = (
            order_lifecycle_service_factory(repository)
            if order_lifecycle_service_factory
            else OrderLifecycleService(GammaPolymarketClient(settings), repository)
        )
        lifecycle.cancel_stale_orders()
        return "/live", "flash.live_stale_orders_cancelled"
    if path == "/live/cancel-all-orders":
        if form.get("confirmation", "").strip() != "CANCEL ALL":
            raise DashboardError("error.live_cancel_confirmation_required")
        settings.ensure_live_trading_ready()
        lifecycle = (
            order_lifecycle_service_factory(repository)
            if order_lifecycle_service_factory
            else OrderLifecycleService(GammaPolymarketClient(settings), repository)
        )
        lifecycle.cancel_all_open_orders()
        return "/live", "flash.live_all_orders_cancelled"
    if path == "/calibration/settle":
        market_id = form.get("market_id", "").strip()
        outcome = form.get("outcome", "").strip().lower()
        if not market_id:
            raise DashboardError("error.calibration_market_required")
        if outcome not in {"yes", "no"}:
            raise DashboardError("error.calibration_outcome_required")
        value = form.get("settlement_value", "").strip()
        repository.settle_model_signals_for_market(
            market_id,
            resolved_outcome=outcome,
            settlement_value=Decimal(value) if value else None,
            settlement_source=_blank_to_none(form.get("settlement_source")),
        )
        return "/calibration", "flash.calibration_settled"
    if path == "/calibration/backfill":
        market_id = form.get("market_id", "").strip()
        if not market_id:
            raise DashboardError("error.calibration_market_required")
        settlement = (
            settlement_service_factory(repository)
            if settlement_service_factory
            else SettlementService(repository, NoaaProvider())
        )
        settlement.backfill_market(market_id)
        return "/calibration", "flash.calibration_backfilled"
    if path == "/calibration/backfill-preview":
        market_id = form.get("market_id", "").strip()
        if not market_id:
            raise DashboardError("error.calibration_market_required")
        settlement = (
            settlement_service_factory(repository)
            if settlement_service_factory
            else SettlementService(repository, NoaaProvider())
        )
        preview = settlement.preview_market(market_id)
        preview_params = {
            "preview_market_id": preview.market_id,
            "preview_station": preview.station or "-",
            "preview_variable": preview.variable or "-",
            "preview_value": str(preview.observed_value),
            "preview_unit": preview.unit,
            "preview_quality": preview.quality_status or "-",
            "preview_outcome": preview.would_resolve_outcome,
            "preview_source": preview.settlement_source,
            "preview_warnings": "|".join(preview.warnings),
        }
        return f"/calibration?{urlencode(preview_params)}", "flash.calibration_previewed"
    if path == "/open-orders/refresh":
        try:
            settings.ensure_live_trading_ready()
            OrderLifecycleService(GammaPolymarketClient(settings), repository).refresh_open_orders()
            return "/open-orders", "flash.orders_refreshed"
        except ValueError as exc:
            raise DashboardError("flash.error", str(exc)) from exc
    if path == "/open-orders/cancel":
        order_id = form.get("order_id", "")
        if not order_id:
            raise DashboardError("error.order_id_required")
        try:
            settings.ensure_live_trading_ready()
            OrderLifecycleService(GammaPolymarketClient(settings), repository).cancel_order(
                order_id
            )
            return "/open-orders", "flash.order_cancelled"
        except ValueError as exc:
            raise DashboardError("flash.error", str(exc)) from exc
    if path == "/discovery/run":
        limit = _positive_int(form.get("limit"), default=50, maximum=200)
        pages = _positive_int(form.get("pages"), default=30, maximum=50)
        module_id = form.get("module") or "weather"
        if module_id == "china_temp_bucket":
            max_ask = _positive_decimal(
                form.get("max_ask"), default=Decimal("0.10"), maximum=Decimal("1")
            )
            event_date = _date_value(form.get("event_date"))
            discovery = ChinaTemperatureBucketDiscoveryService(
                GammaPolymarketClient(settings), repository
            )
            count = discovery.discover(
                limit=limit,
                pages=pages,
                options=ChinaBucketDiscoveryOptions(
                    max_ask=max_ask,
                    include_unsupported=form.get("include_unsupported") == "true",
                    event_dates=(event_date,) if event_date else (),
                ),
            )
            return "/candidates", "flash.discovery_complete" if count else "flash.discovery_empty"
        discovery = (
            discovery_service_factory(repository)
            if discovery_service_factory
            else DiscoveryService(GammaPolymarketClient(settings), repository)
        )
        count = discovery.discover(
            limit=limit,
            pages=pages,
            include_unsupported=form.get("include_unsupported") == "true",
        )
        return "/candidates", "flash.discovery_complete" if count else "flash.discovery_empty"
    if path.startswith("/candidates/") and path.endswith("/mark"):
        market_id = unquote(path.removeprefix("/candidates/").removesuffix("/mark")).strip("/")
        _require_market(repository, market_id)
        status = form.get("status", "").strip()
        if status not in CANDIDATE_STATUSES:
            raise DashboardError("error.invalid_candidate_status")
        repository.mark_candidate(market_id, status, _blank_to_none(form.get("notes")))
        return "/candidates", "flash.candidate_marked"
    if path.startswith("/markets/"):
        market_id, operation = _parse_market_operation(path)
        market = _require_market(repository, market_id)
        workflow = resolve_module_workflow(
            settings,
            repository,
            module_id=market["module_id"],
            workflow_service_factory=(
                (
                    lambda workflow_settings, workflow_repository: market_workflow_service_factory(
                        workflow_repository
                    )
                )
                if market_workflow_service_factory
                else None
            ),
        )
        if operation == "inspect":
            workflow.inspect(market_id)
            return f"/markets/{quote(market_id)}", "flash.market_inspected"
        if operation == "refresh-weather":
            _require_analysis_ready(market)
            workflow.refresh_signal(market_id)
            return f"/markets/{quote(market_id)}", "flash.weather_refreshed"
        if operation == "research":
            workflow.research(market_id)
            return f"/markets/{quote(market_id)}", "flash.market_researched"
        if operation == "analyze":
            if repository.latest_market_snapshot(market_id) is None:
                raise DashboardError("error.market_needs_snapshot")
            _require_analysis_ready(market)
            workflow.analyze(market_id)
            return f"/markets/{quote(market_id)}", "flash.market_analyzed"
        if operation == "trade-dry-run":
            if repository.latest_analysis(market_id) is None:
                if repository.latest_market_snapshot(market_id) is None:
                    raise DashboardError("error.market_needs_snapshot")
                workflow.research(market_id)
            result = workflow.dry_run(market_id)
            flash = (
                "flash.dry_run_recorded"
                if result.summary.startswith("Order intent:")
                else "flash.dry_run_skipped"
            )
            return f"/markets/{quote(market_id)}", flash
        raise DashboardError("error.invalid_post")
    if path == "/reconciliation/run":
        reconciliation = (
            reconciliation_service_factory(repository)
            if reconciliation_service_factory
            else ReconciliationService(GammaPolymarketClient(settings), repository)
        )
        reconciliation.reconcile()
        return "/reconciliation", "flash.reconciliation_complete"
    if path == "/fixtures/load":
        relative = form.get("fixture", "").strip()
        fixture_path = _resolve_fixture_path(relative)
        market_id = load_market_fixture(
            fixture_path,
            repository,
            settings,
            demo_analysis=form.get("demo_analysis") == "true",
        )
        return f"/markets/{quote(market_id)}", "flash.fixture_loaded"
    if path == "/operator/tick":
        profile = get_profile(form.get("profile") or "dry-run-demo")
        daemon = (
            operator_daemon_factory(repository, profile)
            if operator_daemon_factory
            else OperatorDaemon(
                repository=repository,
                client=GammaPolymarketClient(settings),
                profile=profile,
                settings=settings,
                dry_run_only=True,
                allow_live_auto=False,
            )
        )
        daemon.tick(
            discover=False,
            propose=True,
            auto_dry_run=True,
            risk_guard=True,
            include_reconciliation=form.get("include_reconciliation") == "true",
            auto_live=False,
        )
        return "/operator", "flash.operator_tick_complete"
    if path == "/actions/propose-next":
        kind = normalize_kind(form.get("kind") or "analyze")
        if kind not in NON_LIVE_UI_KINDS:
            raise DashboardError("error.live_propose_blocked")
        service.propose_next(
            kind=kind,
            reason=_blank_to_none(form.get("reason")),
            ttl_minutes=_optional_int(form.get("ttl_minutes")),
            requested_by="dashboard",
        )
        return "/actions", "flash.action_proposed"
    if path == "/overrides/set":
        repository.upsert_strategy_override(
            market_id=form.get("market", "").strip() or "*",
            profile=form.get("profile", "").strip() or "*",
            min_edge=_blank_to_none(form.get("min_edge")),
            max_order_usdc=_blank_to_none(form.get("max_order_usdc")),
            max_daily_usdc=_blank_to_none(form.get("max_daily_usdc")),
            max_market_usdc=_blank_to_none(form.get("max_market_usdc")),
            live_auto_enabled=_live_auto_value(form.get("live_auto")),
            notes=_blank_to_none(form.get("notes")),
        )
        return "/overrides", "flash.override_saved"
    if path == "/overrides/delete":
        repository.delete_strategy_override(
            form.get("market", "").strip(), form.get("profile", "").strip()
        )
        return "/overrides", "flash.override_deleted"
    action_id, operation = _parse_action_operation(path)
    if operation == "approve":
        action = _require_action(repository, action_id)
        if action["kind"] == "trade_live":
            raise DashboardError("error.live_approve_blocked")
        service.approve(action_id, "dashboard")
        return f"/actions/{quote(action_id)}", "flash.action_approved"
    if operation == "reject":
        _require_action(repository, action_id)
        service.reject(action_id, "dashboard", _blank_to_none(form.get("reason")))
        return f"/actions/{quote(action_id)}", "flash.action_rejected"
    if operation == "run":
        action = _require_action(repository, action_id)
        if action["kind"] == "trade_live":
            raise DashboardError("error.live_run_blocked")
        if action["status"] != "approved":
            raise DashboardError("error.only_approved_runs")
        service.execute(action_id)
        return f"/actions/{quote(action_id)}", "flash.action_executed"
    raise DashboardError("error.invalid_post")


def _module_alias_path(path: str) -> str:
    parts = path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "modules":
        module_id = parts[1]
        get_module(module_id)
        module_query = f"module={quote(module_id)}"
        if len(parts) == 3 and parts[2] == "markets":
            return f"/markets?{module_query}"
        if len(parts) == 3 and parts[2] == "candidates":
            return f"/candidates?{module_query}"
        if len(parts) == 3 and parts[2] == "discovery":
            return "/discovery"
        if len(parts) >= 4 and parts[2] == "markets":
            return "/markets/" + "/".join(parts[3:])
    return path


def _redirect(
    path: str,
    lang: str,
    flash: str,
    *,
    level: str = "ok",
    detail: str | None = None,
    headers: dict[str, str] | None = None,
) -> DashboardResponse:
    location = _href(path, lang, flash=flash, level=level, detail=detail)
    response_headers = dict(headers or {})
    response_headers["Location"] = location
    return DashboardResponse(
        HTTPStatus.SEE_OTHER,
        f'<p><a href="{_e(location)}">{_e(location)}</a></p>',
        response_headers,
    )


def _parse_form(body: bytes) -> dict[str, str]:
    parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}


def _parse_market_operation(path: str) -> tuple[str, str]:
    parts = path.strip("/").split("/")
    if len(parts) != 3 or parts[0] != "markets":
        raise DashboardError("error.invalid_post")
    return unquote(parts[1]), parts[2]


def _require_market(repository: Repository, market_id: str) -> Row:
    market = repository.get_market(market_id)
    if market is None:
        raise DashboardError("error.unknown_market")
    return market


def _require_analysis_ready(market: Row) -> None:
    if market["module_id"] == "china_temp_bucket":
        return
    rule = parse_resolution_rule(market["title"], market["description"])
    if not rule.location:
        raise DashboardError("error.market_missing_location", rule.rejection_reason)
    if not rule.tradable:
        raise DashboardError("error.market_not_analysis_ready", rule.rejection_reason)


def _parse_action_operation(path: str) -> tuple[str, str]:
    parts = path.strip("/").split("/")
    if len(parts) != 3 or parts[0] != "actions":
        raise DashboardError("error.invalid_post")
    action_id = unquote(parts[1])
    validate_action_id(action_id)
    return action_id, parts[2]


def _require_action(repository: Repository, action_id: str) -> Row:
    action = repository.get_automation_action(action_id)
    if action is None:
        raise DashboardError("error.unknown_action")
    return action


def _fallback_post_location(path: str) -> str:
    if path.startswith("/actions/"):
        parts = path.strip("/").split("/")
        if len(parts) >= 2:
            return f"/actions/{quote(unquote(parts[1]))}"
    if path.startswith("/open-orders"):
        return "/open-orders"
    if path.startswith("/live"):
        return "/live"
    if path.startswith("/calibration"):
        return "/calibration"
    if path.startswith("/overrides"):
        return "/overrides"
    if path.startswith("/setup"):
        step = {
            "/setup/health": "health",
            "/setup/mode": "mode",
            "/setup/wallet": "wallet",
            "/setup/connectivity": "connectivity",
            "/setup/risk": "risk",
            "/setup/weather": "weather",
            "/setup/complete": "review",
            "/setup/init-db": "health",
        }.get(path, "health")
        return f"/setup?step={step}"
    return "/actions"


def _handle_setup_post(
    settings: Settings,
    repository: Repository,
    path: str,
    form: dict[str, str],
    lang: str,
    *,
    host_header: str | None,
    origin_header: str | None,
    autopilot_service_factory: Callable[[Repository], object] | None,
) -> tuple[str, str]:
    from polymarket_weather_arb.desktop.csrf import require_sensitive_post
    from polymarket_weather_arb.desktop.paths import resolve_desktop_paths
    from polymarket_weather_arb.desktop.setup_flow import (
        apply_setup_config,
        build_setup_readiness,
        derive_signer_address,
        mode_to_settings_values,
        normalize_setup_mode,
        risk_preset_values,
    )
    from polymarket_weather_arb.dashboard_ui.setup import desktop_runtime_active

    # Legacy init-db remains available; protect with CSRF when token present,
    # and always when desktop runtime is active.
    if path == "/setup/init-db":
        if desktop_runtime_active() or form.get("csrf_token"):
            require_sensitive_post(
                form,
                host_header=host_header,
                origin_header=origin_header,
                allowed_public_origin=settings.dashboard_public_origin,
            )
        Database(settings.database_path).init_schema()
        return "/setup?step=health", "flash.database_initialized"

    require_sensitive_post(
        form,
        host_header=host_header,
        origin_header=origin_header,
        allowed_public_origin=settings.dashboard_public_origin,
    )

    if path == "/desktop/pause":
        repository.update_autopilot_state(enabled=False)
        return "/app", "flash.autopilot_paused"

    if path == "/desktop/quit":
        shutdown = _DESKTOP_RUNTIME.get("shutdown")
        if callable(shutdown):
            shutdown()
        return "/app", "flash.ok"

    paths = _DESKTOP_RUNTIME.get("paths")
    if paths is None and desktop_runtime_active():
        paths = resolve_desktop_paths()
    keychain = _DESKTOP_RUNTIME.get("keychain")

    if path == "/setup/health":
        Database(settings.database_path).init_schema()
        if paths is not None:
            apply_setup_config(
                paths,  # type: ignore[arg-type]
                {"DATABASE_PATH": str(getattr(paths, "database_path", settings.database_path))},
                keychain=keychain,  # type: ignore[arg-type]
            )
        return "/setup?step=mode", "flash.ok"

    if path == "/setup/mode":
        app_mode = normalize_setup_mode(form.get("app_mode"))
        updates = mode_to_settings_values(app_mode)
        updates["DESKTOP_APP_MODE"] = app_mode  # ignored by Settings (extra=ignore) but stored
        if paths is not None:
            settings = apply_setup_config(paths, updates, keychain=keychain)  # type: ignore[arg-type]
            _replace_dashboard_settings(settings)
        # Persist preferred mode into autopilot_state without enabling live/start.
        _autopilot_service(settings, repository, autopilot_service_factory).set_app_mode(app_mode)
        return "/setup?step=wallet", "flash.ok"

    if path == "/setup/wallet":
        updates: dict[str, str | None] = {}
        funder = _blank_to_none(form.get("polymarket_funder"))
        secrets: dict[str, str] = {}
        delete_secrets: list[str] = []
        private_key = (form.get("polymarket_private_key") or "").strip()
        if form.get("delete_private_key") == "1":
            delete_secrets.append("POLYMARKET_PRIVATE_KEY")
        elif private_key:
            secrets["POLYMARKET_PRIVATE_KEY"] = private_key
            if form.get("derive_funder") == "1" or not funder:
                try:
                    address = derive_signer_address(private_key)
                except Exception as exc:  # noqa: BLE001
                    raise ValueError(f"invalid private key: {exc}") from exc
                if form.get("derive_funder") == "1" or not funder:
                    funder = funder or address
        if funder:
            updates["POLYMARKET_FUNDER"] = funder
        if paths is not None:
            settings = apply_setup_config(
                paths,  # type: ignore[arg-type]
                updates,
                secrets=secrets,
                delete_secrets=delete_secrets,
                keychain=keychain,  # type: ignore[arg-type]
            )
            _replace_dashboard_settings(settings)
        return "/setup?step=connectivity", "flash.ok"

    if path == "/setup/connectivity":
        updates = {}
        proxy = form.get("proxy_url")
        if proxy is not None:
            updates["PROXY_URL"] = proxy.strip() or None
        if paths is not None and updates:
            settings = apply_setup_config(paths, updates, keychain=keychain)  # type: ignore[arg-type]
            _replace_dashboard_settings(settings)
        # Optional read-only probes — never mutate exchange state.
        if form.get("run_probes") == "1":
            try:
                from polymarket_weather_arb.adapters.http_client import build_httpx_client

                client = build_httpx_client(timeout=10.0, settings=settings)
                try:
                    gamma = client.get(f"{settings.polymarket_gamma_api_base.rstrip('/')}/markets")
                    if gamma.status_code >= 400:
                        raise ValueError(
                            f"Gamma market read failed (HTTP {gamma.status_code}). "
                            "Check network/proxy, then retry."
                        )
                    clob = client.get(f"{settings.polymarket_clob_api_base.rstrip('/')}/")
                    if clob.status_code >= 500:
                        raise ValueError(
                            f"CLOB read failed (HTTP {clob.status_code}). "
                            "Check network/proxy, then retry."
                        )
                finally:
                    client.close()
            except ValueError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise ValueError(
                    f"Connectivity probe failed: {exc}. Configuration was kept; fix network and retry."
                ) from exc
        return "/setup?step=risk", "flash.ok"

    if path == "/setup/risk":
        preset = form.get("risk_preset") or "paper"
        custom = {
            "MAX_ORDER_USDC": form.get("max_order_usdc") or "1",
            "MAX_DAILY_USDC": form.get("max_daily_usdc") or "5",
            "MAX_MARKET_USDC": form.get("max_market_usdc") or "2",
            "AUTO_EXIT_MAX_POSITION_USDC": form.get("auto_exit_max_position_usdc") or "1",
            "TRADING_DISABLED": form.get("trading_disabled") or "true",
            "AUTO_EXIT_ENABLED": form.get("auto_exit_enabled") or "false",
        }
        values = risk_preset_values(preset, custom=custom if preset == "custom" else None)
        values["DESKTOP_RISK_PRESET"] = preset
        if paths is not None:
            settings = apply_setup_config(paths, values, keychain=keychain)  # type: ignore[arg-type]
            _replace_dashboard_settings(settings)
        return "/setup?step=weather", "flash.ok"

    if path == "/setup/weather":
        provider = (form.get("weather_provider") or "open-meteo").strip()
        if provider == "auto":
            provider = "open-meteo"
        updates = {
            "WEATHER_PROVIDER": provider,
            "TELEGRAM_NOTIFY_ENABLED": "true"
            if form.get("telegram_notify_enabled") == "true"
            else "false",
            "TELEGRAM_CHAT_ID": _blank_to_none(form.get("telegram_chat_id")),
        }
        secrets = {}
        google_weather_key = (form.get("google_weather_api_key") or "").strip()
        if google_weather_key:
            secrets["GOOGLE_WEATHER_API_KEY"] = google_weather_key
        tg_token = (form.get("telegram_bot_token") or "").strip()
        if tg_token:
            secrets["TELEGRAM_BOT_TOKEN"] = tg_token
        if paths is not None:
            settings = apply_setup_config(
                paths,  # type: ignore[arg-type]
                updates,
                secrets=secrets,
                keychain=keychain,  # type: ignore[arg-type]
            )
            _replace_dashboard_settings(settings)
        if form.get("send_test_telegram") == "1":
            # Read-only/test path: does not start Autopilot.
            if not settings.telegram_notify_ready():
                raise ValueError(
                    "Telegram test requires TELEGRAM_NOTIFY_ENABLED, bot token, and chat id"
                )
            from polymarket_weather_arb.services.telegram_notifier import TelegramNotifier

            notifier = TelegramNotifier(settings)
            notifier(
                {
                    "daemon_event": "desktop_setup_test",
                    "kind": "info",
                    "status": "ok",
                    "summary": "Polymarket Weather setup test notification",
                }
            )
            notifier.flush()
        return "/setup?step=review", "flash.ok"

    if path == "/setup/complete":
        # Final persistence + marker. Never enable Autopilot / live.
        if paths is not None:
            settings = apply_setup_config(
                paths,  # type: ignore[arg-type]
                {},
                keychain=keychain,  # type: ignore[arg-type]
                complete=True,
            )
            _replace_dashboard_settings(settings)
        repository.update_autopilot_state(enabled=False)
        # Touch readiness for audit/logging only (no exchange mutations).
        build_setup_readiness(settings, repository=repository)
        return "/app", "flash.ok"

    raise DashboardError("error.invalid_post")


def _lang_from_request(query: dict[str, list[str]], cookie_header: str | None = None) -> str:
    explicit = _single(query, "lang")
    if explicit in SUPPORTED_LANGS:
        return explicit
    if cookie_header:
        cookie = SimpleCookie(cookie_header)
        value = cookie.get(LANG_COOKIE)
        if value and value.value in SUPPORTED_LANGS:
            return value.value
    return DEFAULT_LANG


def _lang_from_form(
    form: dict[str, str], query: dict[str, list[str]], cookie_header: str | None
) -> str:
    value = form.get("lang") or _single(query, "lang")
    if value in SUPPORTED_LANGS:
        return value
    return _lang_from_request(query, cookie_header)


def _language_headers(query: dict[str, list[str]], lang: str) -> dict[str, str]:
    explicit = _single(query, "lang")
    if explicit in SUPPORTED_LANGS:
        return {"Set-Cookie": f"{LANG_COOKIE}={lang}; Path=/; SameSite=Lax"}
    return {}


def _live_auto_value(value: str | None) -> bool | None:
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def _positive_int(value: str | None, *, default: int, maximum: int) -> int:
    parsed = int(_blank_to_none(value) or default)
    if parsed < 1:
        raise ValueError("value must be positive")
    return min(parsed, maximum)


def _optional_int(value: str | None) -> int | None:
    raw = _blank_to_none(value)
    if raw is None:
        return None
    parsed = int(raw)
    if parsed < 1:
        raise ValueError("value must be positive")
    return parsed


def _positive_decimal(value: str | None, *, default: Decimal, maximum: Decimal) -> Decimal:
    parsed = Decimal(_blank_to_none(value) or str(default))
    if parsed <= 0:
        raise ValueError("value must be positive")
    return min(parsed, maximum)


def _date_value(value: str | None) -> str | None:
    raw = _blank_to_none(value)
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw).date().isoformat()
    except ValueError as exc:
        raise ValueError("invalid date") from exc


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _live_credentials_configured(settings: Settings) -> bool:
    return bool(settings.polymarket_private_key and settings.polymarket_funder)


def _row_value(row: Row, key: str) -> object | None:
    return row[key] if key in row.keys() else None
