import pytest

from polymarket_weather_arb.adapters.http_reader import reset_http_reader_state


@pytest.fixture(autouse=True)
def _disable_cli_color(monkeypatch):
    reset_http_reader_state()
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
