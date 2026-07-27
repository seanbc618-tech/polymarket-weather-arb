"""Real-browser verification of /app local stream lifecycle rows.

Requires playwright (dev dep) and a system Chrome installation.
Uses channel=chrome so no separate Chromium download is required.
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal
from http.server import ThreadingHTTPServer

import pytest

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.dashboard import DashboardHandler
from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def stream_server(tmp_path, monkeypatch):
    # Fast poll so the 3s acceptance window is comfortable.
    monkeypatch.setattr(
        "polymarket_weather_arb.dashboard_ui.stream_panel.STREAM_POLL_MS",
        400,
    )
    # Avoid meta refresh reloading the page mid-test.
    monkeypatch.setattr(
        "polymarket_weather_arb.dashboard_ui.app._app_shell",
        _wrap_shell_without_meta_refresh(),
    )

    settings = Settings(_env_file=None, database_path=tmp_path / "browser-stream.db")
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    repository = Repository(connection)
    repository.ensure_autopilot_state(mode="dry_run", tick_seconds=300)
    repository.update_autopilot_state(enabled=False, mode="dry_run", app_mode="paper")
    market = Market(
        id="m-browser",
        title="Will the highest temperature in NYC be above 80 on May 8?",
        description="NOAA",
        yes_token_id="y",
        no_token_id="n",
        is_weather=True,
    )
    repository.upsert_market(market, {"id": "m-browser"})
    connection.commit()

    def handler(*args, **kwargs):
        DashboardHandler(settings, *args, **kwargs)

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base = f"http://{host}:{port}"
    try:
        yield {
            "base": base,
            "settings": settings,
            "repository": repository,
            "connection": connection,
        }
    finally:
        server.shutdown()
        server.server_close()
        connection.close()


def _wrap_shell_without_meta_refresh():
    from polymarket_weather_arb.dashboard_ui import app as app_mod

    original = app_mod._app_shell

    def shell(*args, **kwargs):
        html = original(*args, **kwargs)
        # Drop meta refresh so the stream poller is not torn down mid-test.
        return html.replace('<meta http-equiv="refresh"', '<meta data-test-disabled-refresh')

    return shell


def _insert_lifecycle_rows(repository: Repository, connection) -> dict[str, int]:
    decision_id = repository.save_autopilot_decision(
        market_id="m-browser",
        action="trade",
        mode="dry_run",
        edge=Decimal("0.18"),
        reason="browser-decision-marker",
        blockers=[],
        status="ok",
    )
    connection.execute(
        """
        INSERT INTO order_intents (
            market_id, side, token_id, limit_price, size, notional,
            rationale, dry_run, status
        ) VALUES ('m-browser', 'buy_yes', 'y', 0.42, 2, 0.84, 'browser-intent', 1, 'submitted')
        """
    )
    intent_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    connection.execute(
        """
        INSERT INTO order_attempts (intent_id, request_payload, response_payload, status, error)
        VALUES (?, '{}', '{}', 'submitted', NULL)
        """,
        (intent_id,),
    )
    attempt_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    connection.execute(
        """
        INSERT INTO fills (
            exchange_fill_id, order_id, market_id, side, price, size, fee, filled_at
        ) VALUES ('fx-browser', 'o-browser', 'm-browser', 'buy', 0.42, 2, 0, CURRENT_TIMESTAMP)
        """
    )
    fill_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    connection.commit()
    return {
        "decision_id": decision_id,
        "intent_id": intent_id,
        "attempt_id": attempt_id,
        "fill_id": fill_id,
    }


def test_stream_dom_shows_decision_intent_attempt_fill_within_3s(stream_server):
    """After committing ledger rows, DOM shows all four kinds within 3 seconds."""
    base = stream_server["base"]
    repository = stream_server["repository"]
    connection = stream_server["connection"]

    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # pragma: no cover - environment specific
            pytest.skip(f"system Chrome unavailable for playwright: {exc}")
        page = browser.new_page()
        try:
            page.goto(f"{base}/app?lang=en", wait_until="domcontentloaded")
            page.wait_for_selector('[data-stream-root="1"]', timeout=5000)
            # Ensure the poller script bound.
            page.wait_for_function(
                "() => document.querySelector('[data-stream-root=\"1\"]').dataset.streamBound === '1'",
                timeout=5000,
            )

            ids = _insert_lifecycle_rows(repository, connection)
            deadline = time.monotonic() + 3.0

            def _seen(kind: str, row_id: int) -> bool:
                selector = f'[data-stream-kind="{kind}"][data-{kind}-id="{row_id}"]'
                return page.locator(selector).count() > 0

            last_error = None
            while time.monotonic() < deadline:
                try:
                    if (
                        _seen("decision", ids["decision_id"])
                        and _seen("intent", ids["intent_id"])
                        and _seen("attempt", ids["attempt_id"])
                        and _seen("fill", ids["fill_id"])
                    ):
                        # Also require the decision reason text for honesty.
                        assert page.locator("text=browser-decision-marker").count() >= 1
                        return
                except Exception as exc:  # pragma: no cover
                    last_error = exc
                page.wait_for_timeout(100)

            missing = []
            for kind, key in (
                ("decision", "decision_id"),
                ("intent", "intent_id"),
                ("attempt", "attempt_id"),
                ("fill", "fill_id"),
            ):
                if not _seen(kind, ids[key]):
                    missing.append(f"{kind}:{ids[key]}")
            feed_html = page.locator('[data-stream-feed="1"]').inner_html()
            raise AssertionError(
                f"lifecycle rows not in DOM within 3s; missing={missing}; "
                f"last_error={last_error}; feed={feed_html[:800]}"
            )
        finally:
            page.close()
            browser.close()
