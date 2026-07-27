from polymarket_weather_arb.services.module_credibility_service import build_module_credibility


def test_credibility_marks_storm_research_only():
    snapshot = build_module_credibility(
        module_id="hurricane_storm",
        rule_confidence=None,
        source="NHC",
        source_grade="research_forecast",
        forecast_age_seconds=None,
        analysis_model=None,
    )

    assert snapshot.live_eligibility == "research_only"
    assert snapshot.rule_status == "needs_review"
    assert "module is research-only" in snapshot.reasons


def test_credibility_marks_global_bucket_micro_live_ready():
    snapshot = build_module_credibility(
        module_id="global_temp_bucket",
        rule_confidence=0.92,
        source="NOAA",
        source_grade="official_forecast",
        forecast_age_seconds=100,
        analysis_model="global-temp-bucket-normal-v1",
    )

    assert snapshot.live_eligibility == "micro_live_ready"
    assert snapshot.rule_status == "clear"
    assert snapshot.data_source == "NOAA"
    assert snapshot.reasons == ["module credibility checks passed"]


def test_credibility_accepts_research_forecast_for_global_bucket():
    snapshot = build_module_credibility(
        module_id="global_temp_bucket",
        rule_confidence=0.92,
        source="open-meteo",
        source_grade="research_forecast",
        forecast_age_seconds=100,
        analysis_model="global-temp-bucket-normal-v1",
        location="London",
    )

    assert snapshot.live_eligibility == "micro_live_ready"
    assert snapshot.blockers == []


def test_credibility_marks_existing_weather_as_gate_required():
    snapshot = build_module_credibility(
        module_id="weather",
        rule_confidence=0.8,
        source="NOAA",
        source_grade="official_forecast",
        forecast_age_seconds=60,
        analysis_model="threshold-interval-v1",
    )

    assert snapshot.live_eligibility == "candidate_gate_required"
    assert snapshot.rule_status == "needs_review"
    assert "rule confidence below 0.85" in snapshot.reasons
