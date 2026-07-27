import logging
import math
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from polymarket_weather_arb.config import Settings

logger = logging.getLogger(__name__)

_HOST_LOCKS_GUARD = threading.Lock()
_HOST_LOCKS: dict[str, threading.Lock] = {}
_HOST_RATE_LIMIT_UNTIL: dict[str, float] = {}
_DEFAULT_RATE_LIMIT_COOLDOWN_S = 15 * 60
_DAILY_LIMIT_RESET_BUFFER_S = 5 * 60
_JSON_CACHE_GUARD = threading.Lock()
_JSON_CACHE_LOCKS: dict[str, threading.Lock] = {}
_JSON_CACHE: dict[str, tuple[float, datetime, Any]] = {}
_OPEN_METEO_USAGE: dict[str, dict[str, int]] = {}


def _host_key(url: str) -> str:
    parsed = httpx.URL(url)
    return f"{parsed.scheme}://{parsed.host}:{parsed.port or ''}"


def _rate_limit_key(url: str) -> str:
    """Share free-tier quota cooldowns across Open-Meteo API subdomains."""
    parsed = httpx.URL(url)
    host = str(parsed.host or "").lower()
    if host.endswith("open-meteo.com") and not host.startswith("customer-"):
        return "open-meteo-free-tier"
    return _host_key(url)


def _host_read_lock(host_key: str) -> threading.Lock:
    """Coordinate reads and retry cooldowns across workers hitting one API host."""
    with _HOST_LOCKS_GUARD:
        return _HOST_LOCKS.setdefault(host_key, threading.Lock())


def _rate_limit_cooldown_seconds(response: httpx.Response) -> int:
    retry_after = response.headers.get("Retry-After", "").strip()
    try:
        parsed = math.ceil(float(retry_after))
    except ValueError:
        parsed = 0
    return parsed if parsed > 0 else _DEFAULT_RATE_LIMIT_COOLDOWN_S


def _daily_limit_cooldown_seconds(now: datetime) -> int:
    now = now.astimezone(timezone.utc)
    tomorrow = (now + timedelta(days=1)).date()
    reset = datetime.combine(tomorrow, datetime.min.time(), tzinfo=timezone.utc)
    return max(1, math.ceil((reset - now).total_seconds()) + _DAILY_LIMIT_RESET_BUFFER_S)


def _is_daily_limit_response(response: httpx.Response) -> bool:
    if response.status_code != 429:
        return False
    try:
        body = response.text.lower()
    except Exception:
        return False
    return "daily api request limit exceeded" in body


def _open_meteo_call_weight(url: str) -> int:
    host = str(httpx.URL(url).host or "").lower()
    return 4 if host == "ensemble-api.open-meteo.com" else 1


def _record_open_meteo_request(url: str, *, status_code: int | None = None) -> None:
    host = str(httpx.URL(url).host or "").lower()
    if not host.endswith("open-meteo.com"):
        return
    day = datetime.now(timezone.utc).date().isoformat()
    with _JSON_CACHE_GUARD:
        stats = _OPEN_METEO_USAGE.setdefault(
            day,
            {
                "network_requests": 0,
                "estimated_units": 0,
                "responses_429": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "stale_cache_hits": 0,
                "cooldown_skips": 0,
            },
        )
        stats["network_requests"] += 1
        stats["estimated_units"] += _open_meteo_call_weight(url)
        if status_code == 429:
            stats["responses_429"] += 1


def _record_open_meteo_cache(url: str, status: str) -> None:
    host = str(httpx.URL(url).host or "").lower()
    if not host.endswith("open-meteo.com"):
        return
    day = datetime.now(timezone.utc).date().isoformat()
    with _JSON_CACHE_GUARD:
        stats = _OPEN_METEO_USAGE.setdefault(
            day,
            {
                "network_requests": 0,
                "estimated_units": 0,
                "responses_429": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "stale_cache_hits": 0,
                "cooldown_skips": 0,
            },
        )
        field = {
            "fresh_cache": "cache_hits",
            "network_fresh": "cache_misses",
            "stale_if_error": "stale_cache_hits",
            "cooldown_skip": "cooldown_skips",
        }.get(status)
        if field:
            stats[field] += 1


def open_meteo_usage_snapshot(*, now: datetime | None = None) -> dict[str, int | str]:
    """Return process-local UTC-day request accounting for operator visibility."""
    day = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()
    with _JSON_CACHE_GUARD:
        stats = dict(_OPEN_METEO_USAGE.get(day, {}))
    return {
        "utc_day": day,
        "network_requests": int(stats.get("network_requests", 0)),
        "estimated_units": int(stats.get("estimated_units", 0)),
        "responses_429": int(stats.get("responses_429", 0)),
        "cache_hits": int(stats.get("cache_hits", 0)),
        "cache_misses": int(stats.get("cache_misses", 0)),
        "stale_cache_hits": int(stats.get("stale_cache_hits", 0)),
        "cooldown_skips": int(stats.get("cooldown_skips", 0)),
    }


def open_meteo_cooldown_remaining(
    *,
    monotonic_func: Callable[[], float] = time.monotonic,
) -> int:
    """Return the shared free-tier cooldown without performing a network read."""
    with _HOST_LOCKS_GUARD:
        cooldown_until = _HOST_RATE_LIMIT_UNTIL.get("open-meteo-free-tier", 0)
    return max(0, math.ceil(cooldown_until - monotonic_func()))


def _json_cache_key(url: str, namespace: str, kwargs: dict[str, Any]) -> str:
    params = kwargs.get("params") or {}
    encoded = json.dumps(params, sort_keys=True, default=str, separators=(",", ":"))
    return f"{namespace}|{url}|{encoded}"


def _json_cache_lock(key: str) -> threading.Lock:
    with _JSON_CACHE_GUARD:
        return _JSON_CACHE_LOCKS.setdefault(key, threading.Lock())


def cached_json_read(
    client: httpx.Client,
    url: str,
    *,
    cache_namespace: str,
    ttl_seconds: float,
    stale_if_error_seconds: float,
    monotonic_func: Callable[[], float] = time.monotonic,
    **kwargs: Any,
) -> tuple[Any, datetime, str]:
    """Coalesce identical GETs and retain bounded stale data for read failures."""
    key = _json_cache_key(url, cache_namespace, kwargs)
    with _json_cache_lock(key):
        now_m = monotonic_func()
        with _JSON_CACHE_GUARD:
            cached = _JSON_CACHE.get(key)
        if cached is not None and 0 <= now_m - cached[0] <= ttl_seconds:
            _record_open_meteo_cache(url, "fresh_cache")
            return cached[2], cached[1], "fresh_cache"

        try:
            response = safe_http_read(client, "GET", url, **kwargs)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            if cached is not None and 0 <= now_m - cached[0] <= stale_if_error_seconds:
                _record_open_meteo_cache(url, "stale_if_error")
                return cached[2], cached[1], "stale_if_error"
            raise

        fetched_at = datetime.now(timezone.utc)
        with _JSON_CACHE_GUARD:
            _JSON_CACHE[key] = (now_m, fetched_at, payload)
            if len(_JSON_CACHE) > 1024:
                oldest = min(_JSON_CACHE, key=lambda item: _JSON_CACHE[item][0])
                _JSON_CACHE.pop(oldest, None)
                _JSON_CACHE_LOCKS.pop(oldest, None)
        _record_open_meteo_cache(url, "network_fresh")
        return payload, fetched_at, "network_fresh"


def reset_http_reader_state() -> None:
    """Clear process-local cooldown, usage, and response caches for isolated tests."""
    with _HOST_LOCKS_GUARD:
        _HOST_RATE_LIMIT_UNTIL.clear()
    with _JSON_CACHE_GUARD:
        _JSON_CACHE.clear()
        _JSON_CACHE_LOCKS.clear()
        _OPEN_METEO_USAGE.clear()


def safe_http_read(
    client: httpx.Client,
    method: str,
    url: str,
    settings: Settings | None = None,
    sleep_func: Callable[[float], None] = time.sleep,
    monotonic_func: Callable[[], float] = time.monotonic,
    utcnow_func: Callable[[], datetime] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """
    Perform an idempotent HTTP read (GET, etc) with retry and backoff logic.
    Do NOT use this for mutating operations (POST, PUT, DELETE, etc.)
    unless the specific API endpoint is explicitly safe and idempotent.

    Retries on:
    - httpx.RequestError (Network, Timeout, etc.)
    - HTTP 429 Too Many Requests
    - HTTP 500, 502, 503, 504 Server Errors
    If the request fails with a 429, 500, 502, 503, or 504 error, or a timeout/network error,
    it will back off and retry up to settings.HTTP_READ_MAX_RETRIES times.
    Only idempotent GET requests are supported.
    """
    if method.upper() != "GET":
        raise ValueError(f"safe_http_read only supports GET, but {method} was provided.")

    if settings is None:
        settings = Settings()
    max_retries = settings.http_read_max_retries
    max_backoff = settings.http_read_max_backoff_s

    # Calls to the same upstream host share one retry lane. In particular, a
    # 429 backoff sleeps while holding this lock, so concurrent weather groups
    # cannot independently hammer the provider during its cooldown window.
    host_key = _host_key(url)
    rate_limit_key = _rate_limit_key(url)
    with _host_read_lock(host_key):
        now = monotonic_func()
        with _HOST_LOCKS_GUARD:
            cooldown_until = _HOST_RATE_LIMIT_UNTIL.get(rate_limit_key, 0)
        if cooldown_until > now:
            remaining = max(1, math.ceil(cooldown_until - now))
            safe_url = str(httpx.URL(url).copy_with(query=None))
            if rate_limit_key == "open-meteo-free-tier":
                _record_open_meteo_cache(url, "cooldown_skip")
            logger.debug(
                "HTTP 429 cooldown active on GET %s: skipping network read for %ss",
                safe_url,
                remaining,
            )
            return httpx.Response(
                429,
                headers={"Retry-After": str(remaining)},
                request=httpx.Request("GET", url),
            )
        with _HOST_LOCKS_GUARD:
            _HOST_RATE_LIMIT_UNTIL.pop(rate_limit_key, None)

        attempt = 0
        while True:
            attempt += 1
            safe_url = str(httpx.URL(url).copy_with(query=None))
            try:
                req_func = getattr(client, method.lower())
                response = req_func(url, **kwargs)
                _record_open_meteo_request(url, status_code=response.status_code)
                if response.status_code not in {429, 500, 502, 503, 504}:
                    return response

                if _is_daily_limit_response(response):
                    current_utc = (
                        utcnow_func() if utcnow_func is not None else datetime.now(timezone.utc)
                    )
                    cooldown = _daily_limit_cooldown_seconds(current_utc)
                    with _HOST_LOCKS_GUARD:
                        _HOST_RATE_LIMIT_UNTIL[rate_limit_key] = monotonic_func() + cooldown
                    logger.warning(
                        "Open-Meteo daily quota exhausted on %s; network reads paused for %ss",
                        safe_url,
                        cooldown,
                    )
                    return response

                if attempt > max_retries:
                    if response.status_code == 429:
                        cooldown = _rate_limit_cooldown_seconds(response)
                        with _HOST_LOCKS_GUARD:
                            _HOST_RATE_LIMIT_UNTIL[rate_limit_key] = monotonic_func() + cooldown
                        logger.warning(
                            "HTTP 429 cooldown opened on %s for %ss",
                            safe_url,
                            cooldown,
                        )
                    logger.warning(
                        f"HTTP {response.status_code} on {method} {safe_url}: "
                        f"Max retries ({max_retries}) exceeded."
                    )
                    return response

                # Handle backoff calculation
                backoff = min(2 ** (attempt - 1), max_backoff)
                if response.status_code == 429:
                    retry_after_str = response.headers.get("Retry-After")
                    if retry_after_str and retry_after_str.isdigit():
                        retry_after = int(retry_after_str)
                        backoff = min(retry_after, max_backoff)

                logger.warning(
                    f"HTTP {response.status_code} on {method} {safe_url}: "
                    f"Attempt {attempt}/{max_retries}. Retrying in {backoff}s..."
                )
                sleep_func(backoff)

            except httpx.RequestError as exc:
                if attempt > max_retries:
                    logger.warning(
                        f"HTTP RequestError ({type(exc).__name__}) on {method} {safe_url}: "
                        f"Max retries ({max_retries}) exceeded."
                    )
                    raise

                backoff = min(2 ** (attempt - 1), max_backoff)
                logger.warning(
                    f"HTTP RequestError ({type(exc).__name__}) on {method} {safe_url}: "
                    f"Attempt {attempt}/{max_retries}. Retrying in {backoff}s..."
                )
                sleep_func(backoff)
