"""Desktop packaging helpers for the macOS beginner app.

The launcher is a composition root only: process lifecycle, data paths, and
settings loading. Strategy, pricing, risk, reconciliation, BUY, and SELL stay
in existing services.
"""

from polymarket_weather_arb.desktop.paths import DesktopPaths, resolve_desktop_paths

__all__ = ["DesktopPaths", "resolve_desktop_paths"]
