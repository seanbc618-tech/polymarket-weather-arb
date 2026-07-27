from __future__ import annotations

from polymarket_weather_arb.modules.base import MarketModule
from polymarket_weather_arb.modules.china_temp_bucket import CHINA_TEMP_BUCKET_MODULE
from polymarket_weather_arb.modules.global_temp_bucket import GLOBAL_TEMP_BUCKET_MODULE
from polymarket_weather_arb.modules.hurricane_storm import HURRICANE_STORM_MODULE
from polymarket_weather_arb.modules.precip_snow import PRECIP_SNOW_MODULE
from polymarket_weather_arb.modules.weather import WEATHER_MODULE

MODULES = {
    module.id: module
    for module in (
        CHINA_TEMP_BUCKET_MODULE,
        GLOBAL_TEMP_BUCKET_MODULE,
        HURRICANE_STORM_MODULE,
        PRECIP_SNOW_MODULE,
        WEATHER_MODULE,
    )
}


def list_modules() -> list[MarketModule]:
    return [MODULES[module_id] for module_id in sorted(MODULES)]


def get_module(module_id: str) -> MarketModule:
    try:
        return MODULES[module_id]
    except KeyError as exc:
        raise ValueError(f"unknown module: {module_id}") from exc


def default_module() -> MarketModule:
    return WEATHER_MODULE
