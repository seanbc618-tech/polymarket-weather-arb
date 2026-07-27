"""Read-only ExitGuardian settlement-core tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from polymarket_weather_arb.domain.global_temperature_bucket import (
    parse_global_temperature_bucket_rule,
)
from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.strategy_versions import GLOBAL_BUCKET_MODEL_VERSION
from polymarket_weather_arb.domain.weather import WeatherObservation
from polymarket_weather_arb.services.exit_guardian_service import ExitGuardianService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def _repo(tmp_path):
    db = Database(tmp_path / "pp.db")
    db.init_schema()
    conn = db.connect()
    return conn, Repository(conn)


def _seed_market(repo: Repository, market_id: str = "m1", *, closed: bool = False):
    repo.upsert_market(
        Market(
            id=market_id,
            title="Will the highest temperature in Chicago be 80F or higher on December 31, 2099?",
            description="NOAA station KORD",
            yes_token_id="yes-token",
            no_token_id="no-token",
            is_weather=True,
            close_time="2099-12-31T23:59:59+00:00",
            status="closed" if closed else "active",
        ),
        {
            "id": market_id,
            "closed": closed,
            "acceptingOrders": not closed,
            "feesEnabled": True,
            "feeType": "weather_fees",
            "orderMinSize": "1",
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["yes-token", "no-token"],
            "endDate": "2099-12-31T23:59:59+00:00",
        },
    )


def _seed_exact_airport_market(
    repo: Repository,
    *,
    market_id: str,
    city: str,
    station: str,
    bucket: str,
) -> None:
    title = f"Will the highest temperature in {city} be {bucket} on December 31, 2099?"
    repo.upsert_market(
        Market(
            id=market_id,
            title=title,
            description=(
                f"Settlement source: Wunderground station {station}. "
                f"https://www.wunderground.com/history/daily/test/{station}"
            ),
            yes_token_id=f"yes-{market_id}",
            no_token_id=f"no-{market_id}",
            is_weather=True,
            close_time="2099-12-31T23:59:59+00:00",
            status="active",
        ),
        {
            "id": market_id,
            "closed": False,
            "acceptingOrders": True,
            "feesEnabled": True,
            "feeType": "weather_fees",
            "orderMinSize": "1",
            "outcomes": ["Yes", "No"],
            "clobTokenIds": [f"yes-{market_id}", f"no-{market_id}"],
            "endDate": "2099-12-31T23:59:59+00:00",
        },
    )


def _fill(repo, *, fid, market_id, side, price, size, fee=0, outcome="YES", filled_at=None):
    ts = filled_at or datetime.now(timezone.utc).isoformat()
    repo.connection.execute(
        """
        INSERT INTO fills (exchange_fill_id, market_id, order_id, side, price, size, fee, filled_at, raw_payload)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fid,
            market_id,
            f"ord-{fid}",
            side,
            float(price),
            float(size),
            float(fee),
            ts,
            "{}",
        ),
    )
    # Ensure outcome matching via raw_payload outcome
    repo.connection.execute(
        "UPDATE fills SET raw_payload = ? WHERE exchange_fill_id = ?",
        (f'{{"outcome": "{outcome}"}}', fid),
    )


def _analysis(
    repo,
    market_id,
    *,
    decision="trade",
    edge="0.20",
    side="buy_yes",
    model=GLOBAL_BUCKET_MODEL_VERSION,
    reasons=None,
    fair_lower="0.70",
    fair_upper="0.85",
    revision=None,
):
    now = datetime.now(timezone.utc)
    analysis_id = repo.save_analysis(
        Analysis(
            market_id=market_id,
            model_version=model,
            fair_lower=Decimal(fair_lower),
            fair_upper=Decimal(fair_upper),
            reference_price=Decimal("0.50"),
            edge=Decimal(edge),
            side=side if decision == "trade" else None,
            decision=decision,
            reasons=reasons or ["fixture"],
            created_at=now,
        )
    )
    if revision is not None:
        repo.connection.execute(
            """
            UPDATE model_signals
            SET raw_payload = json_set(raw_payload, '$.forecast_revision', ?)
            WHERE analysis_id = ?
            """,
            (revision, analysis_id),
        )


def test_price_double_keeps_full_settlement_core(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_market(repo)
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES ('m1','YES',100,1.0)"
    )
    _fill(repo, fid="b1", market_id="m1", side="BUY", price="0.01", size="100", fee=0)
    _analysis(repo, "m1", decision="trade", edge="0.25", side="buy_yes")
    recs = ExitGuardianService(repo).evaluate(
        min_edge=Decimal("0.05"),
        best_bids={("m1", "YES"): Decimal("0.02")},
    )
    pos = [r for r in recs if r.kind == "position"][0]
    assert pos.action == "hold_for_resolution"
    assert pos.policy_stage == "settlement_core"
    assert pos.recommended_size is None
    assert pos.runner_size_after == Decimal("100")
    assert pos.accounting_verified is True
    conn.close()


def test_price_double_never_triggers_profit_exit(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_market(repo)
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES ('m1','YES',100,1.0)"
    )
    _fill(repo, fid="b1", market_id="m1", side="BUY", price="0.01", size="100")
    _analysis(repo, "m1", decision="trade", edge="0.30", side="buy_yes")
    pos = [
        r
        for r in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("m1", "YES"): Decimal("0.02")},
        )
        if r.kind == "position"
    ][0]
    assert pos.action == "hold_for_resolution"
    assert pos.action != "exit_full"
    conn.close()


def test_profitable_dust_position_stays_in_settlement_core(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_market(repo)
    repo.connection.execute(
        "UPDATE markets SET raw_payload = json_set(raw_payload, '$.orderMinSize', '5') "
        "WHERE id = 'm1'"
    )
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES ('m1','YES',6.25,1.0)"
    )
    _fill(repo, fid="b1", market_id="m1", side="BUY", price="0.16", size="6.25")
    _analysis(repo, "m1", decision="trade", edge="0.20", side="buy_yes")

    pos = [
        rec
        for rec in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("m1", "YES"): Decimal("0.25")},
            bid_depths={("m1", "YES"): Decimal("20")},
        )
        if rec.kind == "position"
    ][0]

    assert pos.action == "hold_for_resolution"
    assert pos.policy_stage == "settlement_core"
    assert pos.recommended_size is None
    assert pos.runner_size_after == Decimal("6.25")
    conn.close()


def test_settlement_core_does_not_depend_on_bid_depth(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_market(repo)
    repo.connection.execute(
        "UPDATE markets SET raw_payload = json_set(raw_payload, '$.orderMinSize', '5') "
        "WHERE id = 'm1'"
    )
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES ('m1','YES',6.25,1.0)"
    )
    _fill(repo, fid="b1", market_id="m1", side="BUY", price="0.16", size="6.25")
    _analysis(repo, "m1", decision="trade", edge="0.20", side="buy_yes")

    pos = [
        rec
        for rec in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("m1", "YES"): Decimal("0.25")},
            bid_depths={("m1", "YES"): Decimal("2")},
        )
        if rec.kind == "position"
    ][0]

    assert pos.action == "hold_for_resolution"
    assert pos.policy_stage == "settlement_core"
    conn.close()


def test_prior_sales_do_not_shrink_remaining_settlement_core(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_market(repo)
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES ('m1','YES',40,0.8)"
    )
    _fill(repo, fid="b1", market_id="m1", side="BUY", price="0.01", size="100", fee=0)
    _fill(
        repo,
        fid="s1",
        market_id="m1",
        side="SELL",
        price="0.02",
        size="60",
        fee=0,
        filled_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
    )
    _analysis(repo, "m1", decision="trade", edge="0.25", side="buy_yes")
    pos = [
        r
        for r in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("m1", "YES"): Decimal("0.03")},
        )
        if r.kind == "position"
    ][0]
    # 60*0.02 = 1.20 proceeds >= 1.00 cost
    assert pos.action == "hold_for_resolution"
    assert pos.runner_size_after == Decimal("40")
    assert pos.unrecovered_cash is not None and pos.unrecovered_cash <= Decimal("0.00001")
    conn.close()


def test_model_reversal_alone_does_not_exit(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_market(repo)
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES ('m1','YES',100,1.0)"
    )
    _fill(repo, fid="b1", market_id="m1", side="BUY", price="0.01", size="100")
    _analysis(repo, "m1", decision="trade", edge="0.25", side="buy_no")  # reversed
    pos = [
        r
        for r in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("m1", "YES"): Decimal("0.02")},
        )
        if r.kind == "position"
    ][0]
    assert pos.action == "hold_for_resolution"
    assert pos.recommended_size is None
    assert "model direction reversed" in pos.reason
    conn.close()


def test_watch_with_positive_edge_does_not_exit_full(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_market(repo)
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES ('m1','YES',20,1.4)"
    )
    _fill(repo, fid="b1", market_id="m1", side="BUY", price="0.10", size="20", fee="0.09")
    _analysis(repo, "m1", decision="watch", edge="0.1634", side=None)

    pos = [
        rec
        for rec in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("m1", "YES"): Decimal("0.07")},
        )
        if rec.kind == "position"
    ][0]

    assert pos.action == "hold_for_resolution"
    assert pos.latest_edge == Decimal("0.1634")
    assert pos.recommended_size is None
    conn.close()


def test_watch_with_small_negative_edge_stays_in_no_trade_band(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_market(repo)
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES ('m1','YES',20,1.4)"
    )
    _fill(repo, fid="b1", market_id="m1", side="BUY", price="0.10", size="20", fee="0.09")
    _analysis(repo, "m1", decision="watch", edge="-0.021", side=None)

    pos = [
        rec
        for rec in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("m1", "YES"): Decimal("0.07")},
        )
        if rec.kind == "position"
    ][0]

    assert pos.action == "hold_for_resolution"
    assert pos.policy_stage == "settlement_core"
    conn.close()


def test_large_negative_edge_never_authorizes_model_only_sell(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_market(repo)
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES ('m1','YES',20,1.4)"
    )
    _analysis(
        repo,
        "m1",
        decision="watch",
        edge="-0.08",
        side=None,
        fair_lower="0.00",
        fair_upper="0.02",
        revision="forecast-r1",
    )

    first = [
        rec
        for rec in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("m1", "YES"): Decimal("0.07")},
        )
        if rec.kind == "position"
    ][0]
    assert first.action == "hold_for_resolution"
    assert first.policy_stage == "settlement_core"
    assert "forbids model-only SELL" in first.reason

    _analysis(
        repo,
        "m1",
        decision="watch",
        edge="-0.09",
        side=None,
        fair_lower="0.00",
        fair_upper="0.02",
        revision="forecast-r2",
    )
    second = [
        rec
        for rec in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("m1", "YES"): Decimal("0.07")},
        )
        if rec.kind == "position"
    ][0]
    assert second.action == "hold_for_resolution"
    assert second.policy_stage == "settlement_core"
    assert second.policy_version == "weather-exit-v3-settlement-only"
    assert "forbids model-only SELL" in second.reason
    conn.close()


def test_d0_hourly_strong_contradiction_requires_two_fresh_confirmations(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_market(repo)
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES ('m1','YES',20,1.4)"
    )
    common_reasons = [
        "consensus_probability_median=0.22",
        "model_disagreement=0.47",
        "D0 hourly conditioning probability=0.0041 weight=0.7500 blend_ratio=0.1111",
        (
            "D0 hourly context station=KATL observed_max=78.98F current=75.02F "
            "remaining_peak=94.7F trend_per_hour=0 post_peak=False "
            "trajectory_upper_bound=99.93"
        ),
    ]
    _analysis(
        repo,
        "m1",
        decision="watch",
        edge="-0.04",
        reasons=common_reasons,
        fair_lower="0.00",
        fair_upper="0.44",
    )

    first = [
        rec
        for rec in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("m1", "YES"): Decimal("0.21")},
        )
        if rec.kind == "position"
    ][0]
    assert first.action == "hold_for_resolution"
    assert first.policy_stage == "settlement_core"

    _analysis(
        repo,
        "m1",
        decision="watch",
        edge="-0.04",
        reasons=common_reasons,
        fair_lower="0.00",
        fair_upper="0.44",
    )
    second = [
        rec
        for rec in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("m1", "YES"): Decimal("0.21")},
        )
        if rec.kind == "position"
    ][0]
    assert second.action == "hold_for_resolution"
    assert second.policy_stage == "settlement_core"
    assert second.recommended_size is None
    conn.close()


def test_d0_low_yes_probability_does_not_contradict_held_no(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_market(repo)
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES ('m1','NO',20,1.4)"
    )
    reasons = [
        "consensus_probability_median=0.02",
        "D0 hourly conditioning probability=0.0041 weight=0.7500 blend_ratio=0.1111",
    ]
    _analysis(repo, "m1", decision="watch", edge="0.20", reasons=reasons)
    _analysis(repo, "m1", decision="watch", edge="0.20", reasons=reasons)

    position = [
        rec
        for rec in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("m1", "NO"): Decimal("0.80")},
        )
        if rec.kind == "position"
    ][0]
    assert position.policy_stage != "d0_strong_contradiction"
    conn.close()


def test_station_taf_conflict_alone_cannot_liquidate_settlement_core(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_exact_airport_market(
        repo,
        market_id="seoul-26",
        city="Seoul",
        station="RKSI",
        bucket="26C",
    )
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) "
        "VALUES ('seoul-26','YES',20,1.4)"
    )

    def reasons(issue_time: str) -> list[str]:
        return [
            "consensus_probability_median=0.08",
            "decision_probability_conservative=0.04",
            "model_probabilities=ecmwf:0.0800,icon:0.0700,reference_awc-taf:0.0100",
            "source_weights=ecmwf:1.0000,icon:1.0000,reference_awc-taf:0.8000",
            (
                f"awc_taf_target=27.0C station=RKSI issue_time={issue_time} "
                "valid_time=2026-07-23T04:00:00+00:00 cache_status=network_fresh"
            ),
            "top_candidate_family_supporters=2/6",
            "event_bucket_context=complete siblings=9 expected=9 source=gamma_event",
        ]

    _analysis(
        repo,
        "seoul-26",
        decision="watch",
        edge="-0.02",
        reasons=reasons("2026-07-22T01:09:00+00:00"),
        fair_lower="0.04",
        fair_upper="0.12",
    )

    first = [
        rec
        for rec in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("seoul-26", "YES"): Decimal("0.08")},
        )
        if rec.kind == "position"
    ][0]
    assert first.action == "hold_for_resolution"
    assert first.policy_stage == "settlement_core"

    _analysis(
        repo,
        "seoul-26",
        decision="watch",
        edge="-0.02",
        reasons=reasons("2026-07-22T01:09:00+00:00"),
        fair_lower="0.04",
        fair_upper="0.12",
    )
    duplicate = [
        rec
        for rec in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("seoul-26", "YES"): Decimal("0.08")},
        )
        if rec.kind == "position"
    ][0]
    assert duplicate.action == "hold_for_resolution"
    assert duplicate.policy_stage == "settlement_core"

    _analysis(
        repo,
        "seoul-26",
        decision="watch",
        edge="-0.02",
        reasons=reasons("2026-07-22T03:09:00+00:00"),
        fair_lower="0.04",
        fair_upper="0.12",
    )
    distinct = [
        rec
        for rec in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("seoul-26", "YES"): Decimal("0.08")},
        )
        if rec.kind == "position"
    ][0]
    assert distinct.action == "hold_for_resolution"
    assert distinct.policy_stage == "settlement_core"
    assert distinct.recommended_size is None
    conn.close()


def test_uncalibrated_station_taf_cannot_force_exit(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_exact_airport_market(
        repo,
        market_id="wuhan-33",
        city="Wuhan",
        station="ZHHH",
        bucket="33C",
    )
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) "
        "VALUES ('wuhan-33','YES',20,1.0)"
    )
    reasons = [
        "consensus_probability_median=0.2250",
        "decision_probability_conservative=0.1429",
        "model_probabilities=ecmwf:0.4118,icon:0.2250,reference_awc-taf:0.2297",
        "source_weights=ecmwf:1.0000,icon:1.0000,reference_awc-taf:0.3500",
        "advisory_families_excluded_from_pricing_quorum=aviation-taf",
        (
            "awc_taf_target=34.0C station=ZHHH issue_time=2026-07-23T09:03:00+00:00 "
            "valid_time=2026-07-24T08:00:00+00:00 cache_status=network_fresh"
        ),
        "top_candidate_family_supporters=1/5",
        "event_bucket_context=complete siblings=11 expected=11 source=gamma_event",
    ]
    for _ in range(2):
        _analysis(
            repo,
            "wuhan-33",
            decision="watch",
            edge="0.09",
            reasons=reasons,
            fair_lower="0.0048",
            fair_upper="0.50",
        )

    position = [
        rec
        for rec in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("wuhan-33", "YES"): Decimal("0.04")},
        )
        if rec.kind == "position"
    ][0]

    assert position.policy_stage != "station_forecast_contradiction"
    assert position.policy_stage != "station_forecast_exit_confirmation"
    conn.close()


def test_station_taf_alone_cannot_force_exit_against_model_majority(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_exact_airport_market(
        repo,
        market_id="guangzhou-34",
        city="Guangzhou",
        station="ZGGG",
        bucket="34C",
    )
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) "
        "VALUES ('guangzhou-34','YES',20,1.4)"
    )
    reasons = [
        "consensus_probability_median=0.40",
        (
            "awc_taf_target=35.0C station=ZGGG issue_time=2026-07-21T21:11:00+00:00 "
            "valid_time=2026-07-23T06:00:00+00:00 cache_status=fresh_cache"
        ),
        "top_candidate_supporters=4/6",
    ]
    _analysis(
        repo,
        "guangzhou-34",
        decision="watch",
        edge="0.20",
        reasons=reasons,
        fair_lower="0.20",
        fair_upper="0.55",
    )
    _analysis(
        repo,
        "guangzhou-34",
        decision="watch",
        edge="0.20",
        reasons=reasons,
        fair_lower="0.20",
        fair_upper="0.55",
    )

    position = [
        rec
        for rec in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("guangzhou-34", "YES"): Decimal("0.16")},
        )
        if rec.kind == "position"
    ][0]
    assert position.action == "hold_for_resolution"
    assert position.policy_stage != "station_forecast_contradiction"
    conn.close()


def test_stale_station_taf_cannot_force_exit(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_exact_airport_market(
        repo,
        market_id="seoul-stale-taf",
        city="Seoul",
        station="RKSI",
        bucket="26C",
    )
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) "
        "VALUES ('seoul-stale-taf','YES',20,1.4)"
    )
    reasons = [
        "consensus_probability_median=0.08",
        (
            "awc_taf_target=27.0C station=RKSI issue_time=2026-07-22T01:09:00+00:00 "
            "valid_time=2026-07-23T04:00:00+00:00 cache_status=stale_if_error"
        ),
        "top_candidate_supporters=2/6",
    ]
    for _ in range(2):
        _analysis(
            repo,
            "seoul-stale-taf",
            decision="watch",
            edge="-0.02",
            reasons=reasons,
            fair_lower="0.04",
            fair_upper="0.12",
        )

    position = [
        rec
        for rec in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("seoul-stale-taf", "YES"): Decimal("0.08")},
        )
        if rec.kind == "position"
    ][0]
    assert position.policy_stage != "station_forecast_contradiction"
    conn.close()


def test_bucket_rebalance_marker_never_sells_without_coordinated_target_entry(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_market(repo)
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES ('m1','YES',20,1.4)"
    )
    _analysis(
        repo,
        "m1",
        decision="watch",
        edge="0.04",
        side=None,
        reasons=["rebalance_target=m2", "switch_score_advantage=0.20"],
    )

    pos = [
        rec
        for rec in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("m1", "YES"): Decimal("0.07")},
        )
        if rec.kind == "position"
    ][0]

    assert pos.action == "hold_for_resolution"
    assert pos.policy_stage == "settlement_core"
    assert "rebalance candidate m2" in pos.reason
    conn.close()


def _seed_exact_bucket(
    repo: Repository,
    *,
    market_id: str,
    city: str,
    station: str,
    bucket: str,
    now: datetime,
) -> None:
    title = f"Will the highest temperature in {city} be {bucket}C on July 18, 2026?"
    description = f"Settlement source: Wunderground station {station}."
    market = Market(
        id=market_id,
        title=title,
        description=description,
        yes_token_id=f"yes-{market_id}",
        no_token_id=f"no-{market_id}",
        is_weather=True,
        close_time="2026-07-18T15:00:00+00:00",
        status="active",
    )
    repo.upsert_market(
        type("ModuleMarket", (), {**market.__dict__, "module_id": "global_temp_bucket"})(),
        {
            "id": market_id,
            "closed": False,
            "acceptingOrders": True,
            "feesEnabled": True,
            "feeType": "weather_fees",
            "orderMinSize": "1",
            "outcomes": ["Yes", "No"],
            "clobTokenIds": [f"yes-{market_id}", f"no-{market_id}"],
            "endDate": "2026-07-18T15:00:00+00:00",
        },
    )
    repo.save_temperature_bucket_rule(
        market_id,
        parse_global_temperature_bucket_rule(title, description),
        module_id="global_temp_bucket",
    )
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES (?, 'YES', 6.25, 1)",
        (market_id,),
    )
    _fill(
        repo,
        fid=f"buy-{market_id}",
        market_id=market_id,
        side="BUY",
        price="0.16",
        size="6.25",
        fee="0.042",
        filled_at=now.isoformat(),
    )


def _save_d0_bucket_analysis(
    repo: Repository,
    *,
    market_id: str,
    station: str,
    observed_max: str,
    current: str,
    trajectory_upper: str,
    post_peak: bool,
    now: datetime,
) -> None:
    repo.save_analysis(
        Analysis(
            market_id=market_id,
            model_version="global-temp-bucket-multimodel-v6",
            fair_lower=Decimal("0.75"),
            fair_upper=Decimal("0.90"),
            reference_price=Decimal("0.60"),
            edge=Decimal("0.20"),
            side=None,
            decision="watch",
            reasons=[
                "consensus_probability_median=0.82",
                "model_disagreement=0.15",
                (
                    f"D0 hourly context station={station} observed_max={observed_max}C "
                    f"current={current}C remaining_peak={trajectory_upper}C "
                    f"trend_per_hour=0 post_peak={post_peak} "
                    f"trajectory_upper_bound={trajectory_upper}"
                ),
            ],
            created_at=now,
        )
    )


def _save_awc_observation(
    repo: Repository,
    *,
    market_id: str,
    station: str,
    value: str,
    fetched_at: datetime,
    warnings: list[str] | None = None,
    observation_count: int = 12,
) -> None:
    repo.save_observation(
        WeatherObservation(
            market_id=market_id,
            provider="official-station-observations",
            station=station,
            variable="temperature_high",
            value=Decimal(value),
            unit="C",
            observed_at=fetched_at,
            fetched_at=fetched_at,
            quality_status="AWC",
        ),
        {
            "quality_status": "AWC",
            "latest_observation_at": fetched_at.isoformat(),
            "observations": [
                {"timestamp": fetched_at.isoformat()} for _ in range(observation_count)
            ],
            "warnings": warnings or [],
        },
    )


def test_seoul_d0_winner_lock_beats_principal_recovery_and_rebalance(tmp_path):
    conn, repo = _repo(tmp_path)
    now = datetime(2026, 7, 18, 8, 30, tzinfo=timezone.utc)
    _seed_exact_bucket(
        repo,
        market_id="seoul-26",
        city="Seoul",
        station="RKSI",
        bucket="26",
        now=now,
    )
    _save_d0_bucket_analysis(
        repo,
        market_id="seoul-26",
        station="RKSI",
        observed_max="26",
        current="25",
        trajectory_upper="26.4",
        post_peak=True,
        now=now,
    )
    latest = repo.latest_analysis("seoul-26")
    reasons = list(json.loads(latest["reasons"]))
    reasons.append("rebalance_target=seoul-27")
    repo.connection.execute(
        "UPDATE analyses SET reasons = ? WHERE id = ?",
        (json.dumps(reasons), latest["id"]),
    )
    _save_awc_observation(
        repo,
        market_id="seoul-26",
        station="RKSI",
        value="26",
        fetched_at=now,
    )

    rec = next(
        item
        for item in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("seoul-26", "YES"): Decimal("0.70")},
            now=now,
        )
        if item.kind == "position"
    )

    assert rec.action == "hold_for_resolution"
    assert rec.policy_stage == "d0_winner_lock"
    assert "post-peak conditioned peak 26.4C" in rec.reason
    conn.close()


def test_rising_qingdao_bucket_is_not_winner_locked(tmp_path):
    conn, repo = _repo(tmp_path)
    now = datetime(2026, 7, 18, 5, 30, tzinfo=timezone.utc)
    _seed_exact_bucket(
        repo,
        market_id="qingdao-29",
        city="Qingdao",
        station="ZSQD",
        bucket="29",
        now=now,
    )
    _save_d0_bucket_analysis(
        repo,
        market_id="qingdao-29",
        station="ZSQD",
        observed_max="29",
        current="29",
        trajectory_upper="30.2",
        post_peak=False,
        now=now,
    )
    _save_awc_observation(
        repo,
        market_id="qingdao-29",
        station="ZSQD",
        value="29",
        fetched_at=now,
    )

    rec = next(
        item
        for item in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("qingdao-29", "YES"): Decimal("0.30")},
            now=now,
        )
        if item.kind == "position"
    )

    assert rec.policy_stage != "d0_winner_lock"
    assert rec.action == "hold_for_resolution"
    conn.close()


def test_stale_d0_observation_cannot_lock_winner(tmp_path):
    conn, repo = _repo(tmp_path)
    now = datetime(2026, 7, 18, 8, 30, tzinfo=timezone.utc)
    _seed_exact_bucket(
        repo,
        market_id="seoul-stale",
        city="Seoul",
        station="RKSI",
        bucket="26",
        now=now,
    )
    _save_d0_bucket_analysis(
        repo,
        market_id="seoul-stale",
        station="RKSI",
        observed_max="26",
        current="25",
        trajectory_upper="26.4",
        post_peak=True,
        now=now,
    )
    _save_awc_observation(
        repo,
        market_id="seoul-stale",
        station="RKSI",
        value="26",
        fetched_at=now - timedelta(hours=2),
    )

    rec = next(
        item
        for item in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("seoul-stale", "YES"): Decimal("0.70")},
            now=now,
        )
        if item.kind == "position"
    )

    assert rec.policy_stage != "d0_winner_lock"
    conn.close()


def test_low_coverage_d0_observation_cannot_lock_winner(tmp_path):
    conn, repo = _repo(tmp_path)
    now = datetime(2026, 7, 18, 8, 30, tzinfo=timezone.utc)
    _seed_exact_bucket(
        repo,
        market_id="seoul-low-coverage",
        city="Seoul",
        station="RKSI",
        bucket="26",
        now=now,
    )
    _save_d0_bucket_analysis(
        repo,
        market_id="seoul-low-coverage",
        station="RKSI",
        observed_max="26",
        current="25",
        trajectory_upper="26.4",
        post_peak=True,
        now=now,
    )
    _save_awc_observation(
        repo,
        market_id="seoul-low-coverage",
        station="RKSI",
        value="26",
        fetched_at=now,
        warnings=["low observation coverage: 8 usable records"],
        observation_count=8,
    )

    rec = next(
        item
        for item in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("seoul-low-coverage", "YES"): Decimal("0.70")},
            now=now,
        )
        if item.kind == "position"
    )

    assert rec.policy_stage != "d0_winner_lock"
    conn.close()


def test_insufficient_model_evidence_never_becomes_full_exit(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_market(repo)
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES ('m1','YES',100,1.0)"
    )
    _fill(repo, fid="b1", market_id="m1", side="BUY", price="0.01", size="100")
    _analysis(
        repo,
        "m1",
        decision="watch",
        edge="0.25",
        side=None,
        reasons=[
            "supporting_models=0/0 required=0",
            "evidence_status=insufficient_models",
            "requires at least 3 models",
        ],
    )

    pos = [
        rec
        for rec in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("m1", "YES"): Decimal("0.02")},
        )
        if rec.kind == "position"
    ][0]

    assert pos.action == "review_no_analysis"
    assert pos.policy_stage == "evidence"
    assert "auto-exit blocked" in pos.reason
    conn.close()


def test_unavailable_analysis_model_never_becomes_full_exit(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_market(repo)
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES ('m1','YES',10,1.0)"
    )
    _analysis(
        repo,
        "m1",
        decision="reject",
        edge="0",
        side=None,
        model="global-temp-bucket-unavailable-v1",
        reasons=["forecast/analysis failed: HTTP 429"],
    )

    pos = [rec for rec in ExitGuardianService(repo).evaluate() if rec.kind == "position"][0]

    assert pos.action == "review_no_analysis"
    assert "model_version=global-temp-bucket-unavailable-v1" in pos.reason
    conn.close()


def test_stale_analysis_no_sell_recommendation(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_market(repo)
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES ('m1','YES',10,1)"
    )
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    repo.save_analysis(
        Analysis(
            market_id="m1",
            model_version="t",
            fair_lower=Decimal("0.2"),
            fair_upper=Decimal("0.3"),
            reference_price=Decimal("0.5"),
            edge=Decimal("0.01"),
            side=None,
            decision="skip",
            reasons=["old"],
            created_at=stale,
        )
    )
    pos = [
        r
        for r in ExitGuardianService(repo).evaluate(min_edge=Decimal("0.05"))
        if r.kind == "position"
    ][0]
    assert pos.action == "review_no_analysis"
    conn.close()


def test_settlement_route_never_sell(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_market(repo, closed=True)
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES ('m1','YES',10,1)"
    )
    repo.save_analysis(
        Analysis(
            market_id="m1",
            model_version="settlement-route-v1",
            fair_lower=Decimal("0"),
            fair_upper=Decimal("0"),
            reference_price=None,
            edge=Decimal("0"),
            side=None,
            decision="skip",
            reasons=["Position expired/closed; settlement state: awaiting observation"],
        )
    )
    pos = [r for r in ExitGuardianService(repo).evaluate() if r.kind == "position"][0]
    assert pos.action == "settlement_pending"
    conn.close()


def test_unverified_ledger_no_principal_recovery(tmp_path):
    conn, repo = _repo(tmp_path)
    _seed_market(repo)
    # Position without fills
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES ('m1','YES',50,1)"
    )
    _analysis(repo, "m1", decision="trade", edge="0.20", side="buy_yes")
    pos = [
        r
        for r in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("m1", "YES"): Decimal("0.40")},
        )
        if r.kind == "position"
    ][0]
    assert pos.action != "recover_principal"
    assert pos.accounting_verified is False
    conn.close()
