from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.rules import ResolutionRule
from polymarket_weather_arb.domain.weather import ForecastSnapshot, WeatherObservation
from polymarket_weather_arb.services.settlement_service import SettlementService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


class FakeObservationProvider:
    def __init__(
        self,
        value: Decimal = Decimal("83"),
        unit: str = "F",
        warnings: list[str] | None = None,
    ) -> None:
        self.value = value
        self.unit = unit
        self.warnings = warnings or []
        self.seen_rule: ResolutionRule | None = None

    def fetch_observation(self, market_id: str, rule: ResolutionRule):
        self.seen_rule = rule
        now = datetime.now(timezone.utc)
        return (
            WeatherObservation(
                provider="fake-nws",
                market_id=market_id,
                station=rule.station,
                observed_at=now,
                variable=rule.variable or "temperature_high",
                value=self.value,
                unit=self.unit,
                quality_status="V",
                fetched_at=now,
            ),
            {
                "source": "nws-observation",
                "source_grade": "settlement_observation",
                "official_signal": True,
                "warnings": list(self.warnings),
            },
        )


def test_backfill_market_saves_observation_and_settles_pending_signals(tmp_path):
    connection, repo = _repo(tmp_path)
    provider = FakeObservationProvider(value=Decimal("83"))
    try:
        _seed_market(repo)
        _seed_rule(repo, operator=">=", threshold=Decimal("80"))
        _seed_signal(repo)

        result = SettlementService(repo, provider).backfill_market("m1")

        signal = repo.latest_model_signal("m1")
        observation = repo.latest_observation("m1")

        assert result.market_id == "m1"
        assert result.resolved_outcome == "yes"
        assert result.observation_value == Decimal("83")
        assert result.observation_unit == "F"
        assert result.settlement_source == "nws-observation"
        assert result.updated_signals == 1
        assert provider.seen_rule is not None
        assert provider.seen_rule.station == "KNYC"
        assert signal["outcome_status"] == "resolved"
        assert signal["resolved_outcome"] == "yes"
        assert signal["settlement_value"] == 83
        assert signal["settlement_source"] == "nws-observation"
        assert observation is not None
        assert observation["value"] == 83
        assert observation["quality_status"] == "V"
    finally:
        connection.close()


def test_backfill_market_handles_under_threshold_as_yes_when_observation_is_below(tmp_path):
    connection, repo = _repo(tmp_path)
    provider = FakeObservationProvider(value=Decimal("77"))
    try:
        _seed_market(repo)
        _seed_rule(repo, operator="<=", threshold=Decimal("80"))
        _seed_signal(repo)

        result = SettlementService(repo, provider).backfill_market("m1")

        assert result.resolved_outcome == "yes"
        assert repo.latest_model_signal("m1")["resolved_outcome"] == "yes"
    finally:
        connection.close()


def test_preview_market_does_not_save_observation(tmp_path):
    connection, repo = _repo(tmp_path)
    provider = FakeObservationProvider(value=Decimal("83"))
    try:
        _seed_market(repo)
        _seed_rule(repo, operator=">=", threshold=Decimal("80"))

        result = SettlementService(repo, provider).preview_market("m1")

        assert result.market_id == "m1"
        assert result.observed_value == Decimal("83")
        assert result.unit == "F"
        assert result.station == "KNYC"
        assert result.variable == "temperature_high"
        assert result.quality_status == "V"
        assert result.settlement_source == "nws-observation"
        assert result.rule_operator == ">="
        assert result.rule_threshold == Decimal("80")
        # observation must not be persisted
        assert repo.latest_observation("m1") is None
    finally:
        connection.close()


def test_preview_market_does_not_settle_model_signals(tmp_path):
    connection, repo = _repo(tmp_path)
    provider = FakeObservationProvider(value=Decimal("83"))
    try:
        _seed_market(repo)
        _seed_rule(repo, operator=">=", threshold=Decimal("80"))
        _seed_signal(repo)

        SettlementService(repo, provider).preview_market("m1")

        signal = repo.latest_model_signal("m1")
        assert signal["outcome_status"] != "resolved"
        assert signal["resolved_outcome"] is None
    finally:
        connection.close()


def test_preview_market_computes_yes_outcome_correctly(tmp_path):
    connection, repo = _repo(tmp_path)
    provider = FakeObservationProvider(value=Decimal("83"))
    try:
        _seed_market(repo)
        _seed_rule(repo, operator=">=", threshold=Decimal("80"))

        result = SettlementService(repo, provider).preview_market("m1")
        assert result.would_resolve_outcome == "yes"
    finally:
        connection.close()


def test_preview_market_computes_no_outcome_correctly(tmp_path):
    connection, repo = _repo(tmp_path)
    provider = FakeObservationProvider(value=Decimal("75"))
    try:
        _seed_market(repo)
        _seed_rule(repo, operator=">=", threshold=Decimal("80"))

        result = SettlementService(repo, provider).preview_market("m1")
        assert result.would_resolve_outcome == "no"
    finally:
        connection.close()


def test_preview_market_does_not_persist_parsed_rule(tmp_path):
    """When no resolution_rule row exists, preview must parse but not save the rule."""
    connection, repo = _repo(tmp_path)
    provider = FakeObservationProvider(value=Decimal("83"))
    try:
        _seed_market(repo)
        _seed_signal(repo)
        # no _seed_rule — rule must be parsed from market title on the fly

        result = SettlementService(repo, provider).preview_market("m1")

        assert result.would_resolve_outcome == "yes"
        assert result.observed_value == Decimal("83")
        # rule must NOT have been persisted
        assert repo.get_resolution_rule("m1") is None
        # observation must NOT have been persisted
        assert repo.latest_observation("m1") is None
        # signal must still be pending
        signal = repo.latest_model_signal("m1")
        assert signal["outcome_status"] != "resolved"
        assert signal["resolved_outcome"] is None
    finally:
        connection.close()


def test_backfill_market_persists_parsed_rule_when_missing(tmp_path):
    """backfill_market must save the parsed rule when none exists."""
    connection, repo = _repo(tmp_path)
    provider = FakeObservationProvider(value=Decimal("83"))
    try:
        _seed_market(repo)
        _seed_signal(repo)
        # no _seed_rule

        SettlementService(repo, provider).backfill_market("m1")

        # backfill should have persisted the rule
        assert repo.get_resolution_rule("m1") is not None
    finally:
        connection.close()


def test_preview_result_includes_warnings(tmp_path):
    connection, repo = _repo(tmp_path)
    provider = FakeObservationProvider(
        value=Decimal("83"),
        warnings=[
            "low observation coverage: 2 usable records",
            "selected observation quality is X",
        ],
    )
    try:
        _seed_market(repo)
        _seed_rule(repo, operator=">=", threshold=Decimal("80"))

        result = SettlementService(repo, provider).preview_market("m1")

        assert len(result.warnings) == 2
        assert "low observation coverage" in result.warnings[0]
        assert "quality is X" in result.warnings[1]
    finally:
        connection.close()


def test_backfill_result_includes_warnings(tmp_path):
    connection, repo = _repo(tmp_path)
    provider = FakeObservationProvider(
        value=Decimal("83"),
        warnings=["low observation coverage: 1 usable records"],
    )
    try:
        _seed_market(repo)
        _seed_rule(repo, operator=">=", threshold=Decimal("80"))
        _seed_signal(repo)

        result = SettlementService(repo, provider).backfill_market("m1")

        assert len(result.warnings) == 1
        assert "low observation coverage" in result.warnings[0]
    finally:
        connection.close()


def _repo(tmp_path):
    database = Database(tmp_path / "settlement.db")
    database.init_schema()
    connection = database.connect()
    return connection, Repository(connection)


def _seed_market(repo: Repository) -> None:
    repo.upsert_market(
        Market(
            id="m1",
            slug="m1",
            title="Will NYC high temperature be at least 80F?",
            description="According to NOAA station KNYC on 2026-06-03",
            yes_token_id="yes-m1",
            no_token_id="no-m1",
            status="active",
            is_weather=True,
        ),
        {"id": "m1"},
    )


def _seed_rule(repo: Repository, *, operator: str, threshold: Decimal) -> None:
    repo.save_resolution_rule(
        "m1",
        ResolutionRule(
            raw_text="Will NYC high temperature be at least 80F?",
            location="New York",
            station="KNYC",
            source="NOAA",
            variable="temperature_high",
            operator=operator,
            threshold=threshold,
            unit="F",
            window_start="2026-06-03",
            window_end=None,
            confidence=0.9,
            tradable=True,
            rejection_reason=None,
        ),
    )


def _seed_signal(repo: Repository) -> None:
    now = datetime.now(timezone.utc)
    repo.save_forecast(
        ForecastSnapshot(
            provider="noaa-nws",
            variable="temperature_high",
            value=Decimal("82"),
            unit="F",
            issue_time=now,
            valid_time=now,
            market_id="m1",
            location="New York",
            station="KNYC",
            fetched_at=now,
        ),
        {"source_grade": "official_forecast", "provider": "noaa-nws"},
    )
    repo.save_analysis(
        Analysis(
            market_id="m1",
            model_version="weather-threshold-v1",
            fair_lower=Decimal("0.60"),
            fair_upper=Decimal("0.70"),
            reference_price=Decimal("0.50"),
            edge=Decimal("0.10"),
            side="buy_yes",
            decision="trade",
            reasons=["edge"],
        )
    )
    repo.connection.commit()
