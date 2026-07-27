from decimal import Decimal

import pytest

from polymarket_weather_arb.domain.china_temperature_bucket import (
    parse_china_temperature_bucket_rule,
)
from polymarket_weather_arb.modules.registry import get_module, list_modules


def test_china_temp_bucket_module_is_registered():
    module = get_module("china_temp_bucket")

    assert module.id == "china_temp_bucket"
    assert module.supports_discovery is True
    assert module.supports_analysis is True
    assert module.supports_dry_run is True
    assert "china_temp_bucket" in {item.id for item in list_modules()}


def test_parse_exact_china_temperature_bucket_rule():
    rule = parse_china_temperature_bucket_rule(
        "Will the high temperature in Qingdao be 21°C on May 10, 2026?",
        "Resolved according to China Meteorological Administration observations.",
    )

    assert rule.tradable is True
    assert rule.city == "Qingdao"
    assert rule.city_cn == "青岛"
    assert rule.station_id == "ZSQD"
    assert rule.source == "CMA"
    assert rule.variable == "temperature_high"
    assert rule.bucket_center_c == Decimal("21")
    assert rule.bucket_lower_c == Decimal("20.5")
    assert rule.bucket_upper_c == Decimal("21.5")
    assert rule.target_date == "2026-05-10"
    assert rule.settlement_timezone == "Asia/Shanghai"


def test_parse_range_china_temperature_bucket_rule():
    rule = parse_china_temperature_bucket_rule(
        "Will Chengdu maximum temperature be between 30-31°C for 2026-05-10?",
        "Source: National Meteorological Center.",
    )

    assert rule.tradable is True
    assert rule.city == "Chengdu"
    assert rule.source == "NMC"
    assert rule.variable == "temperature_high"
    assert rule.bucket_center_c == Decimal("30.5")
    assert rule.bucket_lower_c == Decimal("30")
    assert rule.bucket_upper_c == Decimal("31")
    assert rule.target_date == "2026-05-10"


def test_parse_chinese_alias_and_date():
    rule = parse_china_temperature_bucket_rule(
        "上海最高气温会是19℃吗？",
        "根据中国天气网2026年5月10日官方观测结算。",
    )

    assert rule.tradable is True
    assert rule.city == "Shanghai"
    assert rule.city_cn == "上海"
    assert rule.source == "weather.com.cn"
    assert rule.variable == "temperature_high"
    assert rule.bucket_center_c == Decimal("19")
    assert rule.target_date == "2026-05-10"


def test_parse_polymarket_wunderground_bucket_event_pattern():
    rule = parse_china_temperature_bucket_rule(
        "Highest temperature in Shanghai on May 10?",
        "Outcome: 18°C or below. This market resolves to the bucket containing the highest temperature recorded at Shanghai Pudong International Airport Station in degrees Celsius on 10 May '26, according to wunderground.com/history/daily/cn/shanghai/ZSPD. Slug: highest-temperature-in-shanghai-on-may-10-2026",
    )

    assert rule.tradable is True
    assert rule.city == "Shanghai"
    assert rule.station_id == "ZSPD"
    assert rule.source == "Wunderground"
    assert rule.variable == "temperature_high"
    assert rule.bucket_center_c == Decimal("18")
    assert rule.bucket_lower_c == Decimal("17.5")
    assert rule.bucket_upper_c == Decimal("18.5")
    assert rule.target_date == "2026-05-10"


@pytest.mark.parametrize(
    ("title", "description", "reason"),
    [
        (
            "Lowest temperature in Shanghai on May 10, 2026?",
            "Outcome: 18°C. Resolved according to Wunderground.",
            "unsupported or unclear temperature variable",
        ),
        (
            "Will Beijing high temperature be 21°C on May 10, 2026?",
            "Resolved according to China Meteorological Administration.",
            "unsupported or unclear China city",
        ),
        (
            "Will Wuhan high temperature be 21°F on May 10, 2026?",
            "Resolved according to China Meteorological Administration.",
            "temperature bucket must be Celsius",
        ),
        (
            "Will Wuhan high temperature be 20-23°C on May 10, 2026?",
            "Resolved according to China Meteorological Administration.",
            "unclear 1C Celsius temperature bucket",
        ),
        (
            "Will Wuhan temperature be 21°C on May 10, 2026?",
            "Resolved according to China Meteorological Administration.",
            "unsupported or unclear temperature variable",
        ),
        (
            "Will Wuhan high temperature be 21°C on May 10, 2026?",
            "Resolved according to Open-Meteo.",
            "unclear official China weather source",
        ),
        (
            "Will Wuhan high temperature be 21°C?",
            "Resolved according to China Meteorological Administration.",
            "unclear target date",
        ),
    ],
)
def test_china_temperature_bucket_rule_rejections(title, description, reason):
    rule = parse_china_temperature_bucket_rule(title, description)

    assert rule.tradable is False
    assert rule.rejection_reason is not None
    assert reason in rule.rejection_reason
