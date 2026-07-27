"""Version identifiers shared by the weather strategy owners."""

from decimal import Decimal

GLOBAL_BUCKET_MODEL_VERSION = "global-temp-bucket-multimodel-v8"
WEATHER_SOURCE_MODEL_VERSION = "global-temp-source-v2"
WEATHER_ENTRY_POLICY_VERSION = "weather-entry-v5"
WEATHER_EXIT_POLICY_VERSION = "weather-exit-v3-settlement-only"

# V5 keeps the research threshold at 0.08 so near-miss opportunities remain
# measurable, while the final live boundary requires a wider margin.
WEATHER_V5_LIVE_MIN_EDGE = Decimal("0.10")
WEATHER_V5_LIVE_MIN_PRICE = Decimal("0.05")
