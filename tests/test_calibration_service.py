from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json

from polymarket_weather_arb.domain.global_temperature_bucket import (
    parse_global_temperature_bucket_rule,
)
from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.strategy_versions import GLOBAL_BUCKET_MODEL_VERSION
from polymarket_weather_arb.domain.weather import ForecastSnapshot
from polymarket_weather_arb.services.calibration_service import CalibrationService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def test_save_analysis_records_model_signal_with_latest_forecast_context(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        _seed_forecast(repo, provider="noaa-nws", source_grade="official_forecast")

        analysis_id = repo.save_analysis(
            Analysis(
                market_id="m1",
                model_version="weather-threshold-v1",
                fair_lower=Decimal("0.62"),
                fair_upper=Decimal("0.72"),
                reference_price=Decimal("0.48"),
                edge=Decimal("0.14"),
                side="buy_yes",
                decision="trade",
                reasons=["edge exists"],
            )
        )

        signal = repo.latest_model_signal("m1")
        assert signal is not None
        assert signal["analysis_id"] == analysis_id
        assert signal["market_id"] == "m1"
        assert signal["model_version"] == "weather-threshold-v1"
        assert signal["forecast_provider"] == "noaa-nws"
        assert signal["source_grade"] == "official_forecast"
        assert signal["yes_probability"] == 0.67
        assert signal["market_price"] == 0.48
        assert signal["outcome_status"] == "pending"
        payload = json.loads(signal["raw_payload"])
        assert payload["forecast_id"] is not None
        assert "forecast_raw_payload" not in payload
        assert "analysis_reasons" not in payload
    finally:
        connection.close()


def test_identical_cached_bucket_reprice_is_coalesced_for_one_minute(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        analysis = Analysis(
            market_id="m1",
            model_version="global-temp-bucket-multimodel-v6",
            fair_lower=Decimal("0.20"),
            fair_upper=Decimal("0.40"),
            reference_price=Decimal("0.10"),
            edge=Decimal("0.15"),
            side="buy_yes",
            decision="trade",
            reasons=["cached_event_group_reprice", "forecast_revision=same"],
        )

        first = repo.save_analysis(analysis)
        second = repo.save_analysis(analysis)

        assert second == first
        assert connection.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM model_signals").fetchone()[0] == 1
    finally:
        connection.close()


def test_model_signal_prefers_explicit_fair_probability_over_interval_midpoint(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        repo.save_analysis(
            Analysis(
                market_id="m1",
                model_version="global-temp-bucket-multimodel-v5",
                fair_lower=Decimal("0"),
                fair_upper=Decimal("0.80"),
                reference_price=Decimal("0.10"),
                edge=Decimal("0"),
                side=None,
                decision="watch",
                reasons=["clamped conservative interval"],
                fair_probability=Decimal("0.62"),
            )
        )

        signal = repo.latest_model_signal("m1")
        assert signal["yes_probability"] == 0.62
    finally:
        connection.close()


def test_operational_analysis_versions_do_not_create_or_pollute_calibration_signals(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        for version in (
            "global-temp-bucket-unavailable-v1",
            "global-temp-bucket-d0-guard-v1",
            "global-temp-bucket-multimodel-v6-entry-gated",
            "global-temp-bucket-multimodel-v6-switch",
            "settlement-route-v1",
        ):
            repo.save_analysis(
                Analysis(
                    market_id="m1",
                    model_version=version,
                    fair_lower=Decimal("0"),
                    fair_upper=Decimal("0"),
                    reference_price=None,
                    edge=Decimal("0"),
                    side=None,
                    decision="watch",
                    reasons=["operational state"],
                )
            )

        assert repo.list_model_signals(market_id="m1") == []
        assert CalibrationService(repo).report().groups == []
    finally:
        connection.close()


def test_quant_bucket_signals_share_one_calibration_event(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        for market_id, bucket in (("m1", "80°F or below"), ("m2", "81°F")):
            title = f"Will the highest temperature in New York City be {bucket} on July 16, 2026?"
            repo.upsert_market(
                Market(
                    id=market_id,
                    title=title,
                    description="Settlement source: Wunderground station KNYC.",
                    status="active",
                    is_weather=True,
                ),
                {"id": market_id},
            )
            rule = parse_global_temperature_bucket_rule(
                title,
                "Settlement source: Wunderground station KNYC.",
            )
            repo.save_temperature_bucket_rule(market_id, rule, module_id="global_temp_bucket")
            _seed_forecast(
                repo,
                market_id=market_id,
                provider="open-meteo-ensemble",
                source_grade="research_forecast",
            )
            repo.save_analysis(
                Analysis(
                    market_id=market_id,
                    model_version="global-temp-bucket-multimodel-v5",
                    fair_lower=Decimal("0.2"),
                    fair_upper=Decimal("0.4"),
                    reference_price=Decimal("0.1"),
                    edge=Decimal("0.1"),
                    side=None,
                    decision="watch",
                    reasons=["calibration event identity"],
                    fair_probability=Decimal("0.3"),
                )
            )
            repo.settle_model_signals_for_market(
                market_id,
                resolved_outcome="yes" if market_id == "m2" else "no",
                settlement_value=Decimal("85"),
                settlement_source="test",
            )

        signals = repo.list_model_signals(model_version="global-temp-bucket-multimodel-v5")
        identities = {json.loads(signal["raw_payload"])["event_identity"] for signal in signals}
        assert identities == {"New York City_2026-07-16_temperature_high_F"}

        group = next(
            group
            for group in CalibrationService(repo).report().groups
            if group.model_version == "global-temp-bucket-multimodel-v5"
        )
        assert group.resolved_signals == 2
        assert group.distinct_events == 1
        assert group.hit_rate == Decimal("0")
        assert group.brier_score == Decimal("0.37")
    finally:
        connection.close()


def test_settle_model_signals_and_report_scores_by_model_provider(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo, "m1")
        _seed_market(repo, "m2")
        _seed_forecast(
            repo, market_id="m1", provider="open-meteo-ensemble", source_grade="research_forecast"
        )
        repo.save_analysis(
            Analysis(
                market_id="m1",
                model_version="ensemble-threshold-v1",
                fair_lower=Decimal("0.70"),
                fair_upper=Decimal("0.80"),
                reference_price=Decimal("0.50"),
                edge=Decimal("0.20"),
                side="buy_yes",
                decision="trade",
                reasons=["ensemble"],
            )
        )
        _seed_forecast(
            repo, market_id="m2", provider="open-meteo-ensemble", source_grade="research_forecast"
        )
        repo.save_analysis(
            Analysis(
                market_id="m2",
                model_version="ensemble-threshold-v1",
                fair_lower=Decimal("0.20"),
                fair_upper=Decimal("0.30"),
                reference_price=Decimal("0.40"),
                edge=Decimal("0.10"),
                side="buy_no",
                decision="trade",
                reasons=["ensemble"],
            )
        )

        updated = repo.settle_model_signals_for_market(
            "m1",
            resolved_outcome="yes",
            settlement_value=Decimal("83"),
            settlement_source="nws-observation",
        )
        repo.settle_model_signals_for_market(
            "m2",
            resolved_outcome="no",
            settlement_value=Decimal("77"),
            settlement_source="nws-observation",
        )
        connection.commit()

        report = CalibrationService(repo).report()

        assert updated == 1
        assert len(report.groups) == 1
        group = report.groups[0]
        assert group.model_version == "ensemble-threshold-v1"
        assert group.forecast_provider == "open-meteo-ensemble"
        assert group.total_signals == 2
        assert group.resolved_signals == 2
        assert group.hit_rate == Decimal("1")
        assert group.brier_score == Decimal("0.0625")
        assert group.average_edge == Decimal("0.15")
        assert group.status == "collecting"
    finally:
        connection.close()


def test_calibration_trust_is_unknown_without_resolved_samples(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        _seed_forecast(repo)
        repo.save_analysis(
            Analysis(
                market_id="m1",
                model_version="weather-threshold-v1",
                fair_lower=Decimal("0.55"),
                fair_upper=Decimal("0.65"),
                reference_price=Decimal("0.50"),
                edge=Decimal("0.05"),
                side=None,
                decision="watch",
                reasons=["watch"],
            )
        )

        trust = CalibrationService(repo).trust_for_latest_signal("m1")

        assert trust.status == "unknown"
        assert trust.total_signals == 1
        assert trust.resolved_signals == 0
        assert trust.brier_score is None
    finally:
        connection.close()


def test_weather_source_weights_require_distinct_events_and_are_bounded(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        for index in range(20):
            _seed_market(repo, f"m-{index}")
            for provider, probability in (("good", 0.9), ("weak", 0.4)):
                connection.execute(
                    """
                    INSERT INTO model_signals (
                        market_id, model_version, forecast_provider, source_grade,
                        yes_probability, fair_lower, fair_upper, edge, decision,
                        outcome_status, resolved_outcome, raw_payload, created_at
                    ) VALUES (?, 'global-temp-source-v2', ?, 'research_forecast',
                              ?, ?, ?, 0, 'advisory', 'resolved', 'yes', ?, ?)
                    """,
                    (
                        f"m-{index}",
                        provider,
                        probability,
                        probability,
                        probability,
                        json.dumps(
                            {
                                "event_identity": f"New York_2026-08-{index + 1:02d}",
                                "forecast_revision": "r1",
                                "city": "New York",
                                "horizon": "D1",
                            }
                        ),
                        f"2026-07-{index + 1:02d}T00:00:00+00:00",
                    ),
                )

        calibrated = CalibrationService(repo).weather_source_weights(
            city="New York", horizon="D1", providers=["good", "weak", "new"]
        )

        assert calibrated.distinct_events == {"good": 20, "weak": 20, "new": 0}
        assert Decimal("1") < calibrated.weights["good"] <= Decimal("1.5")
        assert Decimal("0.5") <= calibrated.weights["weak"] < Decimal("1")
        assert calibrated.weights["new"] == Decimal("1")
    finally:
        connection.close()


def test_weather_source_weights_do_not_count_revisions_as_events(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo, "m1")
        for revision in range(25):
            connection.execute(
                """
                INSERT INTO model_signals (
                    market_id, model_version, forecast_provider, source_grade,
                    yes_probability, fair_lower, fair_upper, edge, decision,
                    outcome_status, resolved_outcome, raw_payload, created_at
                ) VALUES ('m1', 'global-temp-source-v2', 'source-a', 'research_forecast',
                          0.8, 0.8, 0.8, 0, 'advisory', 'resolved', 'yes', ?, ?)
                """,
                (
                    json.dumps(
                        {
                            "event_identity": "New York_2026-08-01",
                            "forecast_revision": f"r{revision}",
                            "city": "New York",
                            "horizon": "D1",
                        }
                    ),
                    f"2026-07-01T00:{revision:02d}:00+00:00",
                ),
            )

        calibrated = CalibrationService(repo).weather_source_weights(
            city="New York", horizon="D1", providers=["source-a", "source-b"]
        )

        assert calibrated.distinct_events["source-a"] == 1
        assert calibrated.weights == {"source-a": Decimal("1"), "source-b": Decimal("1")}
    finally:
        connection.close()


def test_weather_source_weights_fall_back_to_same_horizon_across_cities(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        for index in range(20):
            market_id = f"global-{index}"
            _seed_market(repo, market_id)
            for provider, probability in (("good", 0.9), ("weak", 0.4)):
                connection.execute(
                    """
                    INSERT INTO model_signals (
                        market_id, model_version, forecast_provider, source_grade,
                        yes_probability, fair_lower, fair_upper, edge, decision,
                        outcome_status, resolved_outcome, raw_payload, created_at
                    ) VALUES (?, 'global-temp-source-v2', ?, 'research_forecast',
                              ?, ?, ?, 0, 'advisory', 'resolved', 'yes', ?, ?)
                    """,
                    (
                        market_id,
                        provider,
                        probability,
                        probability,
                        probability,
                        json.dumps(
                            {
                                "event_identity": f"City {index}_2026-08-01",
                                "forecast_revision": "r1",
                                "city": f"City {index}",
                                "horizon": "D1",
                            }
                        ),
                        f"2026-07-{index + 1:02d}T00:00:00+00:00",
                    ),
                )

        calibrated = CalibrationService(repo).weather_source_weights(
            city="Singapore", horizon="D1", providers=["good", "weak", "new"]
        )

        assert calibrated.distinct_events == {"good": 20, "weak": 20, "new": 0}
        assert calibrated.weights["good"] > Decimal("1")
        assert calibrated.weights["weak"] < Decimal("1")
        assert calibrated.weights["new"] == Decimal("1")
        assert calibrated.reason == (
            "bounded inverse-Brier skill using all cities horizon=D1 phase=unknown"
        )
    finally:
        connection.close()


def test_pending_weather_source_signal_replaces_revision_within_horizon(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo, "m1")
        for revision, probability in (("r1", "0.30"), ("r2", "0.55"), ("r3", "0.70")):
            repo.save_weather_source_signal(
                market_id="m1",
                source="source-a",
                yes_probability=Decimal(probability),
                event_identity="New York_2026-08-01_temperature_high_F",
                forecast_revision=revision,
                city="New York",
                horizon="D1",
                target_date="2026-08-01",
                source_role="ensemble",
                now=datetime.now(timezone.utc),
            )

        rows = repo.list_model_signals(
            limit=None,
            market_id="m1",
            model_version="global-temp-source-v2",
            forecast_provider="source-a",
        )
        assert len(rows) == 1
        assert Decimal(str(rows[0]["yes_probability"])) == Decimal("0.70")
        assert json.loads(rows[0]["raw_payload"])["forecast_revision"] == "r3"
    finally:
        connection.close()


def test_weather_source_signal_preserves_one_revision_per_horizon(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo, "m1")
        for horizon in ("D2", "D1", "D0"):
            repo.save_weather_source_signal(
                market_id="m1",
                source="source-a",
                yes_probability=Decimal("0.60"),
                event_identity="New York_2026-08-01_temperature_high_F",
                forecast_revision=f"revision-{horizon}",
                city="New York",
                horizon=horizon,
                target_date="2026-08-01",
                source_role="ensemble",
                now=datetime.now(timezone.utc),
            )

        rows = repo.list_model_signals(
            limit=None,
            market_id="m1",
            model_version="global-temp-source-v2",
            forecast_provider="source-a",
        )
        assert len(rows) == 3
        assert {json.loads(row["raw_payload"])["horizon"] for row in rows} == {"D2", "D1", "D0"}
    finally:
        connection.close()


def test_weather_source_signal_preserves_distinct_d0_calibration_phases(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo, "m1")
        for phase, revision, probability in (
            ("D0_early", "early-r1", "0.30"),
            ("D0_early", "early-r2", "0.40"),
            ("D0_post_peak", "late-r1", "0.90"),
        ):
            repo.save_weather_source_signal(
                market_id="m1",
                source="reference_awc-taf",
                yes_probability=Decimal(probability),
                event_identity="Seoul_2026-08-01_temperature_high_C",
                forecast_revision=revision,
                city="Seoul",
                station="RKSI",
                horizon="D0",
                target_date="2026-08-01",
                source_role="reference",
                now=datetime.now(timezone.utc),
                calibration_phase=phase,
                lead_hours=Decimal("3.5"),
                market_probability=Decimal("0.25"),
                source_family="aviation-taf",
            )

        rows = repo.list_model_signals(
            limit=None,
            market_id="m1",
            model_version="global-temp-source-v2",
            forecast_provider="reference_awc-taf",
        )
        assert len(rows) == 2
        payloads = [json.loads(row["raw_payload"]) for row in rows]
        assert {payload["calibration_phase"] for payload in payloads} == {
            "D0_early",
            "D0_post_peak",
        }
        early = next(payload for payload in payloads if payload["calibration_phase"] == "D0_early")
        assert early["forecast_revision"] == "early-r2"
        assert early["source_family"] == "aviation-taf"
        assert {Decimal(str(row["market_price"])) for row in rows} == {Decimal("0.25")}
    finally:
        connection.close()


def test_uncalibrated_awc_taf_weight_is_advisory():
    class EmptyRepository:
        def list_resolved_weather_source_signals(self, **_kwargs):
            return []

    calibration = CalibrationService(EmptyRepository()).weather_source_weights(
        city="Seoul",
        horizon="D0",
        providers=["ecmwf", "icon", "reference_awc-taf"],
        calibration_phase="D0_pre_peak",
    )

    assert calibration.weights == {
        "ecmwf": Decimal("1"),
        "icon": Decimal("1"),
        "reference_awc-taf": Decimal("0.35"),
    }
    assert "AWC TAF remains advisory" in calibration.reason


def test_weather_source_bias_uses_unique_winner_and_shrinks_city_history():
    rows = []
    row_id = 0
    for event in range(20):
        for market_id, center, probability, outcome in (
            (f"e{event}-29", 29, 0.8, "no"),
            (f"e{event}-31", 31, 0.2, "yes"),
        ):
            row_id += 1
            rows.append(
                {
                    "id": row_id,
                    "market_id": market_id,
                    "forecast_provider": "ecmwf",
                    "yes_probability": probability,
                    "decision": "advisory",
                    "resolved_outcome": outcome,
                    "raw_payload": json.dumps(
                        {
                            "event_identity": f"event-{event}",
                            "horizon": "D1",
                            "raw_yes_probability": probability,
                        }
                    ),
                    "created_at": f"2026-07-01T{event:02d}:00:00+00:00",
                    "rule_bucket_center": center,
                }
            )

    class FakeRepository:
        def list_resolved_weather_source_signals(self, **_kwargs):
            return rows

    calibration = CalibrationService(FakeRepository()).weather_source_biases(
        city="Example City",
        station=None,
        horizon="D1",
        unit="C",
        providers=["ecmwf"],
    )

    # Raw expected center is 29.4C; winner is 31C. Twenty local samples shrink
    # +1.6C by 20/(20+20) to +0.8C.
    assert calibration.biases["ecmwf"] == Decimal("0.8")
    assert calibration.sigmas["ecmwf"] == Decimal("0.35")
    assert calibration.samples["ecmwf"] == 20
    assert calibration.scopes["ecmwf"] == "city=Example City horizon=D1 phase=unknown"


def test_weather_source_bias_rejects_small_local_samples_and_all_city_fallback():
    rows = []
    for event in range(6):
        rows.extend(
            [
                {
                    "id": event * 2 + 1,
                    "market_id": f"e{event}-29",
                    "forecast_provider": "ecmwf",
                    "yes_probability": 0.8,
                    "decision": "advisory",
                    "resolved_outcome": "no",
                    "raw_payload": json.dumps(
                        {
                            "event_identity": f"event-{event}",
                            "horizon": "D1",
                            "raw_yes_probability": 0.8,
                        }
                    ),
                    "created_at": f"2026-07-01T{event:02d}:00:00+00:00",
                    "rule_bucket_center": 29,
                },
                {
                    "id": event * 2 + 2,
                    "market_id": f"e{event}-31",
                    "forecast_provider": "ecmwf",
                    "yes_probability": 0.2,
                    "decision": "advisory",
                    "resolved_outcome": "yes",
                    "raw_payload": json.dumps(
                        {
                            "event_identity": f"event-{event}",
                            "horizon": "D1",
                            "raw_yes_probability": 0.2,
                        }
                    ),
                    "created_at": f"2026-07-01T{event:02d}:30:00+00:00",
                    "rule_bucket_center": 31,
                },
            ]
        )

    calls = []

    class FakeRepository:
        def list_resolved_weather_source_signals(self, **kwargs):
            calls.append(kwargs)
            return rows

    calibration = CalibrationService(FakeRepository()).weather_source_biases(
        city="Seoul",
        station="RKSI",
        horizon="D1",
        unit="C",
        providers=["ecmwf"],
    )

    assert calibration.biases == {"ecmwf": Decimal("0")}
    assert calibration.scopes == {"ecmwf": "uncalibrated"}
    assert all(call.get("city") is not None or call.get("station") is not None for call in calls)
    assert "all-city additive bias is disabled" in calibration.reason


def test_entry_performance_is_reduction_only_by_horizon_and_price_band():
    rows = [
        {
            "market_id": f"m-{index}",
            "entry_price": 0.20,
            "entered_at": f"2026-07-{index + 1:02d}T12:00:00+00:00",
            "target_date": f"2026-07-{index + 1:02d}",
            "settlement_timezone": "UTC",
            "resolved_outcome": "no",
        }
        for index in range(20)
    ]

    class FakeRepository:
        def list_resolved_live_bucket_entries(self, *, entry_policy_version):
            assert entry_policy_version == "weather-entry-v5"
            return rows

    calibration = CalibrationService(FakeRepository()).entry_performance(
        horizon="D0",
        reference_price=Decimal("0.20"),
    )

    assert calibration.policy_samples == 20
    assert calibration.horizon_samples == 20
    assert calibration.price_band_samples == 20
    assert calibration.multiplier == Decimal("0.50")
    assert "legacy policy versions excluded" in calibration.reason


def test_entry_performance_stays_neutral_until_policy_has_twenty_resolved_events():
    rows = [
        {
            "market_id": f"m-{index}",
            "entry_price": 0.20,
            "entered_at": f"2026-06-{index + 1:02d}T12:00:00+00:00",
            "target_date": f"2026-06-{index + 1:02d}",
            "settlement_timezone": "UTC",
            "resolved_outcome": "no",
        }
        for index in range(19)
    ]

    class FakeRepository:
        def list_resolved_live_bucket_entries(self, *, entry_policy_version):
            assert entry_policy_version == "weather-entry-v5"
            return rows

    calibration = CalibrationService(FakeRepository()).entry_performance(
        horizon="D0",
        reference_price=Decimal("0.20"),
    )

    assert calibration.policy_samples == 19
    assert calibration.multiplier == Decimal("1")
    assert "policy_samples=19/20" in calibration.reason


def test_d0_weak_event_brier_reduces_sizing_without_affecting_other_horizons():
    rows = []
    signal_queries = []
    for event in range(20):
        for bucket, probability, outcome in (
            ("winner", 0.45, "yes"),
            ("loser", 0.55, "no"),
        ):
            rows.append(
                {
                    "id": event * 2 + (1 if bucket == "winner" else 2),
                    "market_id": f"event-{event}-{bucket}",
                    "model_version": GLOBAL_BUCKET_MODEL_VERSION,
                    "forecast_provider": "global-weather",
                    "yes_probability": probability,
                    "edge": 0.1,
                    "decision": "trade",
                    "outcome_status": "resolved",
                    "resolved_outcome": outcome,
                    "raw_payload": json.dumps(
                        {
                            "event_identity": f"event-{event}",
                            "horizon": "D0",
                        }
                    ),
                    "created_at": f"2026-07-{event + 1:02d}T00:00:00+00:00",
                }
            )

    class FakeRepository:
        def latest_model_signal(self, market_id, model_version=None):
            assert market_id == "event-0-winner"
            assert model_version == GLOBAL_BUCKET_MODEL_VERSION
            return rows[0]

        def list_model_signals(self, **kwargs):
            assert kwargs["model_version"] == GLOBAL_BUCKET_MODEL_VERSION
            assert kwargs["forecast_provider"] == "global-weather"
            signal_queries.append(kwargs)
            return rows

    service = CalibrationService(FakeRepository())
    d0 = service.d0_model_sizing(
        market_id="event-0-winner",
        model_version=GLOBAL_BUCKET_MODEL_VERSION,
        horizon="D0",
    )
    d1 = service.d0_model_sizing(
        market_id="event-0-winner",
        model_version=GLOBAL_BUCKET_MODEL_VERSION,
        horizon="D1",
    )
    cached_d0 = service.d0_model_sizing(
        market_id="event-0-winner",
        model_version=GLOBAL_BUCKET_MODEL_VERSION,
        horizon="D0",
    )
    assert cached_d0 == d0
    assert len(signal_queries) == 1

    class SparseRepository(FakeRepository):
        def list_model_signals(self, **kwargs):
            return super().list_model_signals(**kwargs)[:38]

    sparse = CalibrationService(SparseRepository()).d0_model_sizing(
        market_id="event-0-winner",
        model_version=GLOBAL_BUCKET_MODEL_VERSION,
        horizon="D0",
    )

    assert d0.distinct_events == 20
    assert d0.brier_score == Decimal("0.3025")
    assert d0.multiplier == Decimal("0.50")
    assert "event-level Brier" in d0.reason
    assert d1.multiplier == Decimal("1")
    assert sparse.distinct_events == 19
    assert sparse.multiplier == Decimal("1")


def test_d1_weak_event_brier_also_reduces_sizing():
    rows = []
    for event in range(20):
        for bucket, probability, outcome in (
            ("winner", 0.35, "yes"),
            ("loser", 0.65, "no"),
        ):
            rows.append(
                {
                    "id": event * 2 + (1 if bucket == "winner" else 2),
                    "market_id": f"d1-event-{event}-{bucket}",
                    "model_version": GLOBAL_BUCKET_MODEL_VERSION,
                    "forecast_provider": "global-weather",
                    "yes_probability": probability,
                    "edge": 0.1,
                    "decision": "trade",
                    "outcome_status": "resolved",
                    "resolved_outcome": outcome,
                    "raw_payload": json.dumps(
                        {
                            "event_identity": f"d1-event-{event}",
                            "horizon": "D1",
                        }
                    ),
                    "created_at": f"2026-07-{event + 1:02d}T00:00:00+00:00",
                }
            )

    class FakeRepository:
        def latest_model_signal(self, market_id, model_version=None):
            assert market_id == "d1-event-0-winner"
            assert model_version == GLOBAL_BUCKET_MODEL_VERSION
            return rows[0]

        def list_model_signals(self, **kwargs):
            assert kwargs["model_version"] == GLOBAL_BUCKET_MODEL_VERSION
            assert kwargs["forecast_provider"] == "global-weather"
            return rows

    sizing = CalibrationService(FakeRepository()).weather_model_sizing(
        market_id="d1-event-0-winner",
        model_version=GLOBAL_BUCKET_MODEL_VERSION,
        horizon="D1",
    )

    assert sizing.distinct_events == 20
    assert sizing.brier_score == Decimal("0.4225")
    assert sizing.multiplier == Decimal("0.25")
    assert "D1 event-level Brier sizing" in sizing.reason


def test_resolved_live_bucket_entries_use_exact_partial_fills_only(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        title = "Will the highest temperature in New York City be 80°F on July 20, 2026?"
        repo.upsert_market(
            Market(
                id="m1",
                title=title,
                description="Settlement source: Wunderground station KNYC.",
                status="closed",
                is_weather=True,
            ),
            {"id": "m1"},
        )
        rule = parse_global_temperature_bucket_rule(
            title,
            "Settlement source: Wunderground station KNYC.",
        )
        repo.save_temperature_bucket_rule("m1", rule, module_id="global_temp_bucket")
        connection.execute(
            """
            INSERT INTO model_signals (
                market_id, model_version, source_grade, yes_probability,
                fair_lower, fair_upper, edge, decision, outcome_status,
                resolved_outcome, raw_payload
            ) VALUES (
                'm1', 'global-temp-bucket-multimodel-v6', 'research_forecast', 0.5,
                0.5, 0.5, 0, 'trade', 'resolved', 'yes', '{}'
            )
            """
        )
        cursor = connection.execute(
            """
            INSERT INTO order_intents (
                market_id, side, limit_price, size, notional, rationale, dry_run, status,
                entry_policy_version
            ) VALUES (
                'm1', 'buy_yes', 0.10, 100, 10, 'partial', 0,
                'partially_filled_closed', 'weather-entry-v2'
            )
            """
        )
        intent_id = int(cursor.lastrowid)
        connection.execute(
            """
            INSERT INTO order_attempts (
                intent_id, request_payload, response_payload, status
            ) VALUES (?, '{}', '{"order_id":"linked-order"}', 'submitted')
            """,
            (intent_id,),
        )
        connection.execute(
            """
            INSERT INTO fills (
                exchange_fill_id, order_id, market_id, side, price, size, fee, filled_at
            ) VALUES
                ('linked-fill', 'linked-order', 'm1', 'BUY', 0.08, 20, 0, '2026-07-19T12:00:00+00:00'),
                ('unlinked-fill', 'someone-else', 'm1', 'BUY', 0.90, 100, 0, '2026-07-19T12:01:00+00:00')
            """
        )

        entries = repo.list_resolved_live_bucket_entries(entry_policy_version="weather-entry-v2")

        assert len(entries) == 1
        assert entries[0]["entry_price"] == Decimal("0.08")
        assert entries[0]["entered_at"] == "2026-07-19T12:00:00+00:00"
        assert entries[0]["entry_policy_version"] == "weather-entry-v2"

        connection.execute(
            "UPDATE order_intents SET entry_policy_version = 'legacy-entry-v1' WHERE id = ?",
            (intent_id,),
        )
        assert repo.list_resolved_live_bucket_entries(entry_policy_version="weather-entry-v2") == []
    finally:
        connection.close()


def test_prune_superseded_pending_weather_source_signals_preserves_latest_and_resolved(
    tmp_path,
):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo, "m1")
        for revision, status, created_at in (
            ("old", "pending", "2026-07-01T00:00:00+00:00"),
            ("latest", "pending", "2026-07-01T02:00:00+00:00"),
            ("settled", "resolved", "2026-07-01T01:00:00+00:00"),
        ):
            connection.execute(
                """
                INSERT INTO model_signals (
                    market_id, model_version, forecast_provider, source_grade,
                    yes_probability, fair_lower, fair_upper, edge, decision,
                    outcome_status, resolved_outcome, raw_payload, created_at
                ) VALUES (
                    'm1', 'global-temp-source-v2', 'source-a', 'research_forecast',
                    0.6, 0.6, 0.6, 0, 'advisory', ?, ?, ?, ?
                )
                """,
                (
                    status,
                    "yes" if status == "resolved" else None,
                    json.dumps(
                        {
                            "event_identity": "New York_2026-08-01_temperature_high_F",
                            "forecast_revision": revision,
                            "horizon": "D1",
                        }
                    ),
                    created_at,
                ),
            )

        pruned = repo.prune_superseded_pending_weather_source_signals(batch_size=10)
        rows = repo.list_model_signals(
            limit=None,
            market_id="m1",
            model_version="global-temp-source-v2",
            forecast_provider="source-a",
        )

        assert pruned == 1
        assert len(rows) == 2
        assert {json.loads(row["raw_payload"])["forecast_revision"] for row in rows} == {
            "latest",
            "settled",
        }
    finally:
        connection.close()


def _repo(tmp_path):
    database = Database(tmp_path / "calibration.db")
    database.init_schema()
    connection = database.connect()
    return connection, Repository(connection)


def _seed_market(repo: Repository, market_id: str = "m1") -> None:
    repo.upsert_market(
        Market(
            id=market_id,
            slug=market_id,
            title=f"Weather market {market_id}",
            description="NOAA station KNYC",
            yes_token_id=f"yes-{market_id}",
            no_token_id=f"no-{market_id}",
            status="active",
            is_weather=True,
        ),
        {"id": market_id},
    )


def _seed_forecast(
    repo: Repository,
    market_id: str = "m1",
    *,
    provider: str = "noaa-nws",
    source_grade: str = "official_forecast",
) -> None:
    now = datetime.now(timezone.utc)
    repo.save_forecast(
        ForecastSnapshot(
            provider=provider,
            variable="temperature_high",
            value=Decimal("82"),
            unit="F",
            issue_time=now,
            valid_time=now,
            market_id=market_id,
            location="New York",
            station="KNYC",
            fetched_at=now,
        ),
        {"source_grade": source_grade, "provider": provider},
    )


def test_llm_weight_distinct_events(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo, "m1")
        _seed_market(repo, "m2")
        from polymarket_weather_arb.services.calibration_service import _calculate_weight

        # Test distinct events
        assert _calculate_weight(19, Decimal("0.24"), Decimal("0.52"), Decimal("0")) == Decimal("0")
        assert _calculate_weight(20, Decimal("0.24"), Decimal("0.52"), Decimal("0")) == Decimal(
            "0.10"
        )
        assert _calculate_weight(50, Decimal("0.22"), Decimal("0.55"), Decimal("0")) == Decimal(
            "0.25"
        )
        assert _calculate_weight(100, Decimal("0.20"), Decimal("0.58"), Decimal("0")) == Decimal(
            "0.50"
        )

        # Test reset conditions
        assert _calculate_weight(100, Decimal("0.28"), Decimal("0.58"), Decimal("0")) == Decimal(
            "0"
        )
        assert _calculate_weight(100, Decimal("0.20"), Decimal("0.58"), Decimal("0.11")) == Decimal(
            "0"
        )

    finally:
        connection.close()


def test_repo_save_llm_model_signal(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo, "m1")
        now = datetime.now(timezone.utc)
        repo.save_llm_model_signal(
            market_id="m1",
            provider="fake",
            model="model",
            yes_probability=Decimal("0.4"),
            confidence=Decimal("0.8"),
            reason="test",
            event_identity="nyc_2026-05-08",
            forecast_revision="rev1",
            now=now,
        )

        signal = repo.latest_model_signal("m1")
        assert signal is not None
        assert signal["model_version"] == "llm-weather-vote-v1"
        assert signal["forecast_provider"] == "llm:fake:model"
        assert signal["yes_probability"] == 0.4
        assert signal["decision"] == "advisory"

        # Duplicate should be ignored
        repo.save_llm_model_signal(
            market_id="m1",
            provider="fake",
            model="model",
            yes_probability=Decimal("0.9"),
            confidence=Decimal("0.8"),
            reason="test",
            event_identity="nyc_2026-05-08",
            forecast_revision="rev1",
            now=now,
        )

        signal2 = repo.latest_model_signal("m1")
        assert signal2["yes_probability"] == 0.4
    finally:
        connection.close()


def test_invalid_llm_review_is_counted_as_malformed_not_as_resolved_forecast(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo, "valid")
        _seed_market(repo, "invalid")
        now = datetime.now(timezone.utc)
        repo.save_llm_model_signal(
            market_id="valid",
            provider="fake",
            model="model",
            yes_probability=Decimal("0.8"),
            confidence=Decimal("0.8"),
            reason="valid",
            event_identity="event-valid",
            forecast_revision="rev-valid",
            now=now,
        )
        repo.save_llm_model_signal(
            market_id="invalid",
            provider="fake",
            model="model",
            yes_probability=Decimal("0"),
            confidence=Decimal("0"),
            reason="invalid",
            event_identity="event-invalid",
            forecast_revision="rev-invalid",
            now=now,
            decision="invalid",
        )
        repo.settle_model_signals_for_market(
            "valid",
            resolved_outcome="yes",
            settlement_value=Decimal("80"),
            settlement_source="test",
        )
        repo.settle_model_signals_for_market(
            "invalid",
            resolved_outcome="yes",
            settlement_value=Decimal("80"),
            settlement_source="test",
        )

        group = next(
            group
            for group in CalibrationService(repo).report().groups
            if group.model_version == "llm-weather-vote-v1"
        )

        assert group.total_signals == 2
        assert group.resolved_signals == 1
        assert group.distinct_events == 1
        assert group.malformed_rate == Decimal("0.5")
        assert group.effective_weight == 0
    finally:
        connection.close()
