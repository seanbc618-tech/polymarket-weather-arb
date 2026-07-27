"""Proxy helper tests (offline). Isolate from ambient HTTP_PROXY env."""

from __future__ import annotations

import os

from polymarket_weather_arb.adapters.http_client import (
    apply_proxy_environment,
    build_httpx_client,
    effective_proxy_url,
)
from polymarket_weather_arb.config import Settings


def _settings(**kwargs) -> Settings:
    # Explicitly clear proxy fields so ambient shell env does not leak in.
    base = {
        "PROXY_URL": None,
        "HTTP_PROXY": None,
        "HTTPS_PROXY": None,
        "ALL_PROXY": None,
        "NO_PROXY": None,
    }
    base.update(kwargs)
    return Settings(**base)


def test_effective_proxy_url_prefers_proxy_url():
    s = _settings(HTTP_PROXY="http://a:1", HTTPS_PROXY="http://b:2", PROXY_URL="http://c:3")
    assert effective_proxy_url(s) == "http://c:3"
    s2 = _settings(HTTP_PROXY="http://a:1")
    assert effective_proxy_url(s2) == "http://a:1"
    s3 = _settings(PROXY_URL="http://c:3")
    assert effective_proxy_url(s3) == "http://c:3"


def test_apply_proxy_environment_exports_keys(monkeypatch):
    for key in (
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = _settings(PROXY_URL="http://127.0.0.1:7890")
    applied = apply_proxy_environment(settings)
    assert applied["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert applied["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7890"


def test_build_httpx_client_sets_proxy():
    settings = _settings(PROXY_URL="http://127.0.0.1:7890")
    client = build_httpx_client(timeout=5, settings=settings)
    try:
        assert client is not None
        assert effective_proxy_url(settings) == "http://127.0.0.1:7890"
    finally:
        client.close()
