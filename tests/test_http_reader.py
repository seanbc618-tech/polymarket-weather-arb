import httpx
import logging
import threading
import time
from datetime import datetime, timezone

import pytest
from httpx import Request, Response, TimeoutException

from polymarket_weather_arb.adapters.http_reader import (
    cached_json_read,
    open_meteo_usage_snapshot,
    safe_http_read,
)
from polymarket_weather_arb.config import Settings


class MockSleep:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds: float):
        self.calls.append(seconds)


def test_safe_http_read_rejects_non_get():
    settings = Settings(
        http_read_max_retries=1,
        http_read_max_backoff_s=1,
    )
    with httpx.Client() as client:
        with pytest.raises(ValueError, match="safe_http_read only supports GET"):
            safe_http_read(client, "POST", "http://test.com", settings)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("http_read_max_retries", -1),
        ("http_read_max_backoff_s", -1),
    ],
)
def test_http_read_retry_settings_must_be_non_negative(field, value):
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        Settings(_env_file=None, **{field: value})


def test_safe_http_read_success():
    settings = Settings(HTTP_READ_MAX_RETRIES=3, HTTP_READ_MAX_BACKOFF_S=5)
    sleep = MockSleep()

    with httpx.Client() as client:
        # We need to mock client.get
        client.get = lambda url, **kwargs: Response(200, request=Request("GET", url))

        response = safe_http_read(client, "GET", "http://test.com", settings, sleep_func=sleep)

    assert response.status_code == 200
    assert len(sleep.calls) == 0


def test_safe_http_read_429_success():
    settings = Settings(HTTP_READ_MAX_RETRIES=3, HTTP_READ_MAX_BACKOFF_S=10)
    sleep = MockSleep()

    attempts = 0

    def mock_get(url, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return Response(429, headers={"Retry-After": "15"}, request=Request("GET", url))
        return Response(200, request=Request("GET", url))

    with httpx.Client() as client:
        client.get = mock_get
        response = safe_http_read(client, "GET", "http://test.com", settings, sleep_func=sleep)

    assert response.status_code == 200
    assert attempts == 2
    assert len(sleep.calls) == 1
    # max_backoff is 10, retry-after is 15, so it should sleep 10.
    assert sleep.calls[0] == 10


def test_open_meteo_daily_quota_stops_retries_and_cools_all_free_hosts():
    settings = Settings(HTTP_READ_MAX_RETRIES=3, HTTP_READ_MAX_BACKOFF_S=10)
    calls = 0
    now_m = [100.0]

    class Client:
        def get(self, url, **kwargs):
            nonlocal calls
            calls += 1
            return Response(
                429,
                json={"reason": "Daily API request limit exceeded. Please try again tomorrow."},
                request=Request("GET", url),
            )

    client = Client()
    first = safe_http_read(
        client,
        "GET",
        "https://ensemble-api.open-meteo.com/v1/ensemble",
        settings,
        sleep_func=lambda _: None,
        monotonic_func=lambda: now_m[0],
        utcnow_func=lambda: datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
    )
    blocked = safe_http_read(
        client,
        "GET",
        "https://api.open-meteo.com/v1/forecast",
        settings,
        sleep_func=lambda _: None,
        monotonic_func=lambda: now_m[0],
    )

    assert first.status_code == 429
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 12 * 60 * 60
    assert calls == 1
    usage = open_meteo_usage_snapshot()
    assert usage["network_requests"] == 1
    assert usage["estimated_units"] == 4
    assert usage["responses_429"] == 1


def test_open_meteo_cooldown_skips_are_counted_without_warning_spam(caplog):
    settings = Settings(HTTP_READ_MAX_RETRIES=0, HTTP_READ_MAX_BACKOFF_S=1)
    now_m = [100.0]

    class Client:
        def get(self, url, **kwargs):
            return Response(
                429,
                json={"reason": "Daily API request limit exceeded. Please try again tomorrow."},
                request=Request("GET", url),
            )

    client = Client()
    safe_http_read(
        client,
        "GET",
        "https://ensemble-api.open-meteo.com/v1/ensemble",
        settings,
        monotonic_func=lambda: now_m[0],
        utcnow_func=lambda: datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
    )
    caplog.clear()

    with caplog.at_level(logging.WARNING):
        for _ in range(25):
            response = safe_http_read(
                client,
                "GET",
                "https://geocoding-api.open-meteo.com/v1/search",
                settings,
                monotonic_func=lambda: now_m[0],
            )
            assert response.status_code == 429

    assert not [record for record in caplog.records if "cooldown active" in record.message]
    usage = open_meteo_usage_snapshot()
    assert usage["network_requests"] == 1
    assert usage["responses_429"] == 1
    assert usage["cooldown_skips"] == 25


def test_cached_json_read_coalesces_identical_open_meteo_requests():
    calls = 0
    now_m = [100.0]

    class Client:
        def get(self, url, **kwargs):
            nonlocal calls
            calls += 1
            return Response(
                200, json={"daily": {"time": ["2026-07-20"]}}, request=Request("GET", url)
            )

    client = Client()
    kwargs = {
        "cache_namespace": "test-daily",
        "ttl_seconds": 3600,
        "stale_if_error_seconds": 7200,
        "monotonic_func": lambda: now_m[0],
        "params": {"latitude": 1, "longitude": 2},
    }
    first = cached_json_read(client, "https://api.open-meteo.com/v1/forecast", **kwargs)
    second = cached_json_read(client, "https://api.open-meteo.com/v1/forecast", **kwargs)

    assert first[0] == second[0]
    assert first[2] == "network_fresh"
    assert second[2] == "fresh_cache"
    assert calls == 1
    usage = open_meteo_usage_snapshot()
    assert usage["network_requests"] == 1
    assert usage["cache_misses"] == 1
    assert usage["cache_hits"] == 1


def test_cached_json_read_coalesces_concurrent_weather_groups():
    calls = 0
    network_started = threading.Event()
    release_network = threading.Event()
    results = []

    class Client:
        def get(self, url, **kwargs):
            nonlocal calls
            calls += 1
            network_started.set()
            assert release_network.wait(timeout=2)
            return Response(
                200,
                json={"daily": {"time": ["2026-07-20"]}},
                request=Request("GET", url),
            )

    client = Client()

    def read():
        results.append(
            cached_json_read(
                client,
                "https://ensemble-api.open-meteo.com/v1/ensemble",
                cache_namespace="parallel-dates",
                ttl_seconds=3600,
                stale_if_error_seconds=7200,
                params={"latitude": 1, "longitude": 2, "forecast_days": 8},
            )
        )

    first = threading.Thread(target=read)
    second = threading.Thread(target=read)
    first.start()
    assert network_started.wait(timeout=2)
    second.start()
    time.sleep(0.05)

    assert calls == 1
    release_network.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert calls == 1
    assert {result[2] for result in results} == {"network_fresh", "fresh_cache"}


def test_safe_http_read_shares_429_cooldown_between_same_host_workers():
    settings = Settings(HTTP_READ_MAX_RETRIES=1, HTTP_READ_MAX_BACKOFF_S=1)
    first_sleep_started = threading.Event()
    release_first_sleep = threading.Event()
    calls: list[str] = []

    def blocking_sleep(seconds: float):
        first_sleep_started.set()
        assert release_first_sleep.wait(timeout=2)

    class Client:
        def get(self, url, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                return Response(429, request=Request("GET", url))
            return Response(200, request=Request("GET", url))

    client = Client()
    first = threading.Thread(
        target=safe_http_read,
        args=(client, "GET", "https://weather.test/one", settings, blocking_sleep),
    )
    second = threading.Thread(
        target=safe_http_read,
        args=(client, "GET", "https://weather.test/two", settings, lambda _: None),
    )
    first.start()
    assert first_sleep_started.wait(timeout=2)
    second.start()
    time.sleep(0.05)

    assert calls == ["https://weather.test/one"]

    release_first_sleep.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive()
    assert not second.is_alive()
    assert calls == [
        "https://weather.test/one",
        "https://weather.test/one",
        "https://weather.test/two",
    ]


def test_safe_http_read_opens_cross_request_cooldown_after_429_exhaustion():
    settings = Settings(HTTP_READ_MAX_RETRIES=0, HTTP_READ_MAX_BACKOFF_S=1)
    now = [100.0]
    calls = 0

    class Client:
        def get(self, url, **kwargs):
            nonlocal calls
            calls += 1
            status = 429 if calls == 1 else 200
            return Response(status, request=Request("GET", url))

    client = Client()
    url = "https://cooldown-weather.test/ensemble"

    first = safe_http_read(
        client,
        "GET",
        url,
        settings,
        sleep_func=lambda _: None,
        monotonic_func=lambda: now[0],
    )
    second = safe_http_read(
        client,
        "GET",
        url,
        settings,
        sleep_func=lambda _: None,
        monotonic_func=lambda: now[0],
    )

    assert first.status_code == 429
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "900"
    assert calls == 1

    now[0] += 901
    recovered = safe_http_read(
        client,
        "GET",
        url,
        settings,
        sleep_func=lambda _: None,
        monotonic_func=lambda: now[0],
    )
    assert recovered.status_code == 200
    assert calls == 2


def test_safe_http_read_cooldown_honors_retry_after():
    settings = Settings(HTTP_READ_MAX_RETRIES=0, HTTP_READ_MAX_BACKOFF_S=1)
    now = [500.0]
    calls = 0

    class Client:
        def get(self, url, **kwargs):
            nonlocal calls
            calls += 1
            return Response(
                429,
                headers={"Retry-After": "42"},
                request=Request("GET", url),
            )

    client = Client()
    url = "https://retry-after-weather.test/ensemble"
    safe_http_read(
        client,
        "GET",
        url,
        settings,
        sleep_func=lambda _: None,
        monotonic_func=lambda: now[0],
    )
    blocked = safe_http_read(
        client,
        "GET",
        url,
        settings,
        sleep_func=lambda _: None,
        monotonic_func=lambda: now[0],
    )

    assert blocked.headers["Retry-After"] == "42"
    assert calls == 1


def test_safe_http_read_5xx_exhausts():
    settings = Settings(HTTP_READ_MAX_RETRIES=2, HTTP_READ_MAX_BACKOFF_S=5)
    sleep = MockSleep()

    attempts = 0

    def mock_get(url, **kwargs):
        nonlocal attempts
        attempts += 1
        return Response(502, request=Request("GET", url))

    with httpx.Client() as client:
        client.get = mock_get
        response = safe_http_read(
            client, "GET", "http://test.com?secret=123", settings, sleep_func=sleep
        )

    assert response.status_code == 502
    assert attempts == 3  # Initial + 2 retries
    assert len(sleep.calls) == 2


def test_safe_http_read_timeout_success():
    settings = Settings(HTTP_READ_MAX_RETRIES=2, HTTP_READ_MAX_BACKOFF_S=5)
    sleep = MockSleep()

    attempts = 0

    def mock_get(url, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise TimeoutException("timeout")
        return Response(200, request=Request("GET", url))

    with httpx.Client() as client:
        client.get = mock_get
        response = safe_http_read(client, "GET", "http://test.com", settings, sleep_func=sleep)

    assert response.status_code == 200
    assert attempts == 3
    assert len(sleep.calls) == 2


def test_safe_http_read_400_no_retry():
    settings = Settings(HTTP_READ_MAX_RETRIES=3, HTTP_READ_MAX_BACKOFF_S=5)
    sleep = MockSleep()

    attempts = 0

    def mock_get(url, **kwargs):
        nonlocal attempts
        attempts += 1
        return Response(400, request=Request("GET", url))

    with httpx.Client() as client:
        client.get = mock_get
        response = safe_http_read(client, "GET", "http://test.com", settings, sleep_func=sleep)

    assert response.status_code == 400
    assert attempts == 1
    assert len(sleep.calls) == 0


def test_mutating_client_does_not_use_retry():
    # Verify that GammaPolymarketClient place_limit_order doesn't use the retry mechanism
    # We can inspect the source code to ensure 'safe_http_read' is not there
    from pathlib import Path

    client_path = Path("src/polymarket_weather_arb/adapters/polymarket/client.py")
    content = client_path.read_text()

    assert "safe_http_read" in content

    # Extract mutation methods to ensure safe_http_read is not used there
    import ast

    tree = ast.parse(content)

    mutation_methods = {"place_limit_order", "place_sell_limit_order", "cancel_order"}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in mutation_methods:
            found.add(node.name)
            body = ast.unparse(node)
            assert "safe_http_read" not in body

    assert mutation_methods.issubset(found)
