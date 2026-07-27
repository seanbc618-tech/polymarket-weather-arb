"""Shared httpx client factory with optional temporary proxy support."""

from __future__ import annotations

import os
from typing import Any

import httpx

from polymarket_weather_arb.config import Settings


def apply_proxy_environment(settings: Settings | None = None) -> dict[str, str]:
    """Export proxy settings into process env for httpx/urllib/SDK trust_env.

    Returns the env keys that were set (for tests/logging).
    """
    settings = settings or Settings()
    applied: dict[str, str] = {}
    # PROXY_URL is the temporary app-level catch-all and wins when set.
    http_proxy = settings.proxy_url or settings.http_proxy or settings.all_proxy
    https_proxy = settings.proxy_url or settings.https_proxy or settings.all_proxy or http_proxy
    all_proxy = settings.proxy_url or settings.all_proxy
    no_proxy = settings.no_proxy

    def _set(key: str, value: str | None) -> None:
        if value:
            os.environ[key] = value
            applied[key] = value

    _set("HTTP_PROXY", http_proxy)
    _set("http_proxy", http_proxy)
    _set("HTTPS_PROXY", https_proxy)
    _set("https_proxy", https_proxy)
    _set("ALL_PROXY", all_proxy)
    _set("all_proxy", all_proxy)
    _set("NO_PROXY", no_proxy)
    _set("no_proxy", no_proxy)
    return applied


def effective_proxy_url(settings: Settings | None = None) -> str | None:
    settings = settings or Settings()
    return settings.proxy_url or settings.https_proxy or settings.http_proxy or settings.all_proxy


def build_httpx_client(
    timeout: float = 20,
    *,
    settings: Settings | None = None,
    **kwargs: Any,
) -> httpx.Client:
    """Create an httpx.Client that honors optional temporary proxy settings."""
    settings = settings or Settings()
    apply_proxy_environment(settings)
    proxy = effective_proxy_url(settings)
    client_kwargs: dict[str, Any] = {"timeout": timeout, "trust_env": True, **kwargs}
    if proxy and "proxy" not in client_kwargs:
        client_kwargs["proxy"] = proxy
    return httpx.Client(**client_kwargs)
