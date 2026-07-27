from polymarket_weather_arb.modules.registry import get_module, list_modules


def test_weather_roadmap_modules_are_registered():
    modules = {module.id: module for module in list_modules()}

    assert get_module("global_temp_bucket").supports_discovery is True
    assert get_module("global_temp_bucket").supports_analysis is True
    assert get_module("global_temp_bucket").supports_dry_run is True
    assert get_module("global_temp_bucket").live_eligibility == "micro_live_ready"
    assert get_module("precip_snow").supports_discovery is True
    assert get_module("precip_snow").supports_analysis is True
    assert get_module("precip_snow").supports_dry_run is True
    assert get_module("hurricane_storm").supports_discovery is True
    assert get_module("hurricane_storm").supports_analysis is False
    assert get_module("hurricane_storm").supports_dry_run is False
    assert {"global_temp_bucket", "precip_snow", "hurricane_storm"} <= set(modules)
